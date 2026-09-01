"""Streamlit chat UI for the financial planning agent.

Run with::

    uv run streamlit run app.py

Two pieces of state matter here and they live in different places:

* ``st.session_state.messages`` is only for *redrawing* the transcript after a
  rerun. It is not the agent's memory.
* The agent's real memory is the SQLite checkpointer keyed by ``thread_id``.
  That is why each turn sends only the new user message -- resending the whole
  transcript would duplicate every prior turn in the checkpointed thread.
"""

from __future__ import annotations

import streamlit as st

from financial_planner.agent import build_agent, build_checkpointer
from financial_planner.config import (
    DEFAULT_MODEL,
    WORKSPACE_DIR,
    ensure_directories,
    missing_required_keys,
)
from financial_planner.envelope import redact
from financial_planner.rendering import escape_dollars, escape_markdown
from financial_planner.streaming import Token, ToolEnd, ToolStart, stream_agent_events
from financial_planner.uploads import save_uploads

# No "xls": reading legacy BIFF needs xlrd, which is not a dependency, so
# accepting one only produced a file the sidebar listed and every tool call
# then failed on. Better to reject it at the picker.
UPLOAD_TYPES = ["csv", "xlsx", "pdf"]

# How many new characters to accumulate before repainting a streaming answer.
# Each repaint re-escapes the whole answer so far, so painting once per token
# makes the render cost grow with the square of the answer length -- a long
# reply spends seconds re-scanning text the user has already read. At model
# speed this is still several repaints a second, so the text keeps flowing.
STREAM_REDRAW_CHARS = 32

# What a turn leaves behind when it never reaches an answer at all. The reserved
# slot in the turn block below explains what can end one that way.
INTERRUPTED = "_This turn was interrupted before the agent replied. Ask again to retry._"

TOOL_LABELS = {
    "project_savings": "Projecting savings",
    "required_savings_rate": "Solving for a savings rate",
    "loan_payment": "Calculating loan payment",
    "plan_debt_payoff": "Simulating debt payoff",
    "test_withdrawal_plan": "Testing withdrawal plan",
    "inspect_document": "Inspecting document",
    "summarize_spending": "Summarizing spending",
    "read_pdf_text": "Reading PDF",
    "get_quote": "Fetching quotes",
    "get_fund_profile": "Looking up fund",
    "get_historical_return": "Fetching historical returns",
    "search_web": "Searching the web",
    "write_todos": "Planning",
    "read_file": "Reading file",
    "write_file": "Writing file",
    "edit_file": "Updating file",
    "ls": "Listing files",
    "glob": "Finding files",
    "grep": "Searching files",
    "task": "Delegating to a subagent",
}

SUGGESTIONS = {
    ":material/savings: Am I on track to retire?": (
        "I'd like a retirement readiness check. Ask me for whatever you need."
    ),
    ":material/receipt_long: Build me a budget": (
        "Help me build a budget from the statements in my workspace."
    ),
    ":material/trending_down: Which debt first?": (
        "I have multiple debts and want to know which to pay off first."
    ),
}

st.set_page_config(
    page_title="Financial planner",
    page_icon=":material/account_balance:",
    layout="centered",
)


@st.cache_resource(show_spinner="Starting the planner...")
def load_agent(model_name: str):
    """Build the agent once and reuse it across reruns.

    Cached as a *resource* rather than data: it owns a SQLite connection and a
    model client, neither of which should be rebuilt on every keystroke.

    The spinner text is overridden because the default names the *function* --
    "Running `load_agent(...)`." -- and this is the one place an internal
    identifier would reach a UI that maintains TOOL_LABELS precisely so the user
    never sees one.
    """
    ensure_directories()
    return build_agent(model=model_name, checkpointer=build_checkpointer())


def new_thread_id() -> str:
    """Mint a thread id. Time-prefixed so threads sort chronologically.

    The random suffix is not decoration. Seconds-only ids collide when "New
    conversation" is clicked twice quickly, and a collision silently resumes
    the previous thread: the transcript looks empty because that lives in
    session_state, while the checkpointer -- keyed by thread id -- hands the
    agent the whole prior conversation back.
    """
    import datetime as _dt
    import uuid as _uuid

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"session-{stamp}-{_uuid.uuid4().hex[:8]}"


# --- Fast UI first ----------------------------------------------------------

ensure_directories()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "thread_id" not in st.session_state:
    st.session_state.thread_id = new_thread_id()
if "unsaved_uploads" not in st.session_state:
    st.session_state.unsaved_uploads = []

with st.sidebar:
    st.subheader("Financial planner")
    st.caption(f"Model: `{DEFAULT_MODEL}`")

    if st.button("New conversation", icon=":material/add_comment:", width="stretch"):
        st.session_state.messages = []
        st.session_state.thread_id = new_thread_id()
        st.session_state.unsaved_uploads = []
        st.rerun()

    st.divider()
    st.markdown("**Your documents**")
    documents = sorted(
        p.name for p in WORKSPACE_DIR.glob("*") if p.is_file() and p.name != ".gitkeep"
    )
    if documents:
        for name in documents:
            # escape_markdown, for the reason the upload warning below gives:
            # the name reached disk from the browser and st.caption renders
            # markdown, so "a`b.csv" opens a code span that swallows every
            # filename listed after it. Only the name is escaped -- the icon
            # directive around it is ours and has to keep rendering.
            st.caption(f":material/description: {escape_markdown(name)}")
    else:
        st.caption("No documents yet. Attach one in the chat box below.")

    st.divider()
    st.caption(
        "Educational planning support, not licensed financial advice. "
        "Your documents stay on this machine; excerpts are sent to the model "
        "when the agent reads them."
    )

st.title("Financial planner")

missing = missing_required_keys()
if missing:
    st.error(
        f"Missing required environment variable(s): {', '.join(missing)}. "
        "Copy `.env.example` to `.env` and add your key, then restart the app.",
        icon=":material/key_off:",
    )
    st.stop()

# Redraw the transcript. This is display only -- the agent's history lives in
# the checkpointer, not here.
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        # escape_dollars everywhere text reaches st.markdown: "$2.2M at 7% and
        # $1.5M at 5%" would otherwise render the middle as LaTeX.
        st.markdown(escape_dollars(message["content"]))
        # The tool failures of that turn. They are also written live into an
        # st.status, but that is collapsed by default and the post-turn rerun
        # destroys it, so this is the only place they outlive the turn --
        # and _tool_message_failed exists precisely so that a tool which failed
        # is not read as one that worked.
        for failed in message.get("tool_errors", ()):
            # escape_markdown, not escape_dollars: a tool name arrives on a
            # ToolMessage, so it is model output rather than prose of ours.
            st.caption(f":material/error: {escape_markdown(failed)} returned an error")

# Onboarding chips, shown only on an empty conversation.
#
# The selection is used directly rather than stashed in session_state and
# replayed after a st.rerun(). That round trip deadlocks: st.rerun() aborts the
# script before the stash can be read, and on the next run the pills widget
# still holds the same selection, so it stashes and reruns again forever.
picked: str | None = None
suggestions_slot = st.empty()
if not st.session_state.messages:
    picked = suggestions_slot.pills("Try asking:", list(SUGGESTIONS), label_visibility="collapsed")

prompt = st.chat_input(
    "Ask about your finances, or attach a statement",
    accept_file="multiple",
    file_type=UPLOAD_TYPES,
    submit_mode="disable",
)

user_text: str | None = None
uploaded_names: list[str] = []

# Cleared on every submission so the notice below never outlives the upload it
# describes. It lives in session_state because the st.rerun() at the end of a
# successful turn repaints the page: a warning drawn during the turn would
# otherwise flash past unread, which is the silent loss it exists to prevent.
if picked or prompt:
    st.session_state.unsaved_uploads = []

# Saved here rather than inside the branch that resolves the text: the files
# ride on the chat input, so an attachment is persisted whichever source ends
# up supplying the message.
if prompt and prompt.files:
    uploaded_names, st.session_state.unsaved_uploads = save_uploads(prompt.files, WORKSPACE_DIR)

if picked:
    user_text = SUGGESTIONS[picked]
elif prompt:
    user_text = (prompt.text or "").strip() or None

if st.session_state.unsaved_uploads:
    # escape_markdown, not escape_dollars: the name is browser-supplied and
    # st.warning renders markdown, so "$100 to $200.csv" arrives as LaTeX -- but
    # so does "a`b.csv", which opens a code span that swallows the rest of the
    # sentence telling the user how to recover the file. A filename is data, not
    # prose, so nothing in it should render.
    unsaved = ", ".join(escape_markdown(n) for n in st.session_state.unsaved_uploads)
    st.warning(
        f"Could not save: {unsaved}. The name is unusable or the write failed; "
        "rename the file and attach it again.",
        icon=":material/warning:",
    )

if user_text or uploaded_names:
    # Unmount the chips before the turn starts. They are the one input left live
    # while the agent streams -- st.chat_input carries submit_mode="disable" for
    # exactly this reason -- and a click on one is a rerun request that lands at
    # the next st.* call inside the streaming loop. The reserved reply below
    # stops that orphaning the question, but the turn is still thrown away along
    # with everything it spent, so the better fix is not to offer the click.
    suggestions_slot.empty()

    # Tell the agent where the files landed; it cannot see the upload widget.
    if uploaded_names:
        listing = ", ".join(f"/workspace/{n}" for n in uploaded_names)
        note = f"I've uploaded these files: {listing}"
        user_text = f"{user_text}\n\n{note}" if user_text else note

    assert user_text is not None
    st.session_state.messages.append({"role": "user", "content": user_text})

    # The assistant's half of the exchange is appended now, before a single
    # token exists, and filled in as the turn goes. Every write below mutates
    # this dict rather than appending a second one.
    #
    # It is the reservation that matters, not the placeholder text. Streamlit
    # delivers rerun and stop requests as ScriptControlException, which
    # subclasses BaseException specifically so that user code cannot catch it,
    # and every st.* call in the streaming loop is a delivery point -- the Stop
    # button, the toolbar's Rerun, runOnSave, and a click on any widget still
    # mounted all arrive that way. An append at the *end* of the turn therefore
    # loses that race, leaving the question in the transcript with no reply,
    # which the next redraw shows as an agent that ignored it. A slot claimed
    # up front cannot be lost: at worst it still says the turn was interrupted.
    tool_errors: list[str] = []
    reply = {"role": "assistant", "content": INTERRUPTED, "tool_errors": tool_errors}
    st.session_state.messages.append(reply)

    with st.chat_message("user"):
        st.markdown(escape_dollars(user_text))

    completed = False
    with st.chat_message("assistant"):
        activity = st.status("Thinking", expanded=False)
        answer_slot = st.empty()
        streamed = ""
        painted = 0

        try:
            agent = load_agent(DEFAULT_MODEL)
            config = {
                "configurable": {"thread_id": st.session_state.thread_id},
                "recursion_limit": 100,
            }
            events = stream_agent_events(agent, [{"role": "user", "content": user_text}], config)

            for event in events:
                if isinstance(event, Token):
                    streamed += event.text
                    # Repaint on a line break -- where a half-drawn list or
                    # heading looks worst -- or once enough new text has piled
                    # up to be worth another full-answer escape pass.
                    if "\n" in event.text or len(streamed) - painted >= STREAM_REDRAW_CHARS:
                        answer_slot.markdown(escape_dollars(streamed))
                        painted = len(streamed)
                elif isinstance(event, ToolStart):
                    label = TOOL_LABELS.get(event.name, event.name)
                    activity.update(label=label)
                    activity.write(f":material/play_arrow: {label}")
                elif isinstance(event, ToolEnd):
                    if not event.ok:
                        tool_errors.append(event.name)
                        # Open the status. Everything else in it is progress
                        # chatter; this is the one line worth interrupting the
                        # answer for, and it is invisible while collapsed.
                        activity.update(expanded=True)
                        activity.write(
                            f":material/error: {event.name} returned an error; "
                            "the agent will retry or work around it."
                        )

            answer = streamed.strip()
            activity.update(label="Done", state="complete")

            if not answer:
                answer = (
                    "_The agent finished without producing a reply. "
                    "Try rephrasing, or start a new conversation._"
                )

            # Unconditional final paint: the throttle above leaves the last few
            # tokens undrawn, so the completed answer is only exact once this
            # has run.
            answer_slot.markdown(escape_dollars(answer))

            reply["content"] = answer
            completed = True

        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            activity.update(label="Failed", state="error")
            # Redacted for the same reason the tools redact: this is the one
            # place a model-client error surfaces, and those quote the failing
            # request. Streamlit renders it and it may be screenshotted.
            detail = redact(f"{type(exc).__name__}: {exc}")
            # escape_markdown for the same reason as the filename above: this is
            # a raw exception string, not prose, and st.error renders markdown.
            # An error quoting two dollar amounts renders the span between them
            # as LaTeX, and one quoting a backtick or a bracket -- which HTTP
            # clients do when they echo a request back -- mangles or truncates
            # the only description of the failure the user gets.
            safe_detail = escape_markdown(detail)
            st.error(safe_detail, icon=":material/error:")
            # Nothing is sent to the *checkpointer* -- it already holds whatever
            # partial state the graph committed. But session_state.messages is
            # display only (see the module docstring), and leaving it without a
            # reply means the next rerun redraws a question the agent appears to
            # have ignored, the st.error having gone with the old page.
            #
            # It stores the *escaped* string, not the raw one. This copy is the
            # durable half of the pair -- the st.error goes with the old page,
            # this is redrawn on every later run -- and that redraw runs
            # escape_dollars, which substitutes only "$", so a raw exception's
            # backticks, brackets and dunders would all render. Escaping once is
            # stable under the redraw: escape_dollars skips a dollar that is
            # already backslashed.
            partial = streamed.strip()
            if partial:
                # Whatever the user already watched appear is kept. It is still
                # painted in answer_slot, so dropping it means the next redraw
                # silently deletes text they read -- and a half-answer plus a
                # reason is far more use than a reason alone.
                reply["content"] = (
                    f"{partial}\n\n_The turn failed part-way through this reply: {safe_detail}_"
                )
            else:
                reply["content"] = f"_This turn failed and was not answered: {safe_detail}_"

    if completed:
        # Redraw once the turn is done. The sidebar's document list and the
        # onboarding chips were both rendered from pre-turn state, so without
        # this a statement uploaded this turn stays invisible in the sidebar
        # even after the agent has read it. Safe from looping: on the redraw
        # the chat input is empty and the transcript is non-empty, so nothing
        # re-enters this block.
        st.rerun()
