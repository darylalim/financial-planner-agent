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
from financial_planner.rendering import escape_dollars
from financial_planner.streaming import Token, ToolEnd, ToolStart, stream_agent_events
from financial_planner.uploads import save_uploads

UPLOAD_TYPES = ["csv", "xlsx", "xls", "pdf"]

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


@st.cache_resource
def load_agent(model_name: str):
    """Build the agent once per session and reuse it across reruns.

    Cached as a *resource* rather than data: it owns a SQLite connection and a
    model client, neither of which should be rebuilt on every keystroke.
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

with st.sidebar:
    st.subheader("Financial planner")
    st.caption(f"Model: `{DEFAULT_MODEL}`")

    if st.button("New conversation", icon=":material/add_comment:", width="stretch"):
        st.session_state.messages = []
        st.session_state.thread_id = new_thread_id()
        st.rerun()

    st.divider()
    st.markdown("**Your documents**")
    documents = sorted(
        p.name for p in WORKSPACE_DIR.glob("*") if p.is_file() and p.name != ".gitkeep"
    )
    if documents:
        for name in documents:
            st.caption(f":material/description: {name}")
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

# Onboarding chips, shown only on an empty conversation.
#
# The selection is used directly rather than stashed in session_state and
# replayed after a st.rerun(). That round trip deadlocks: st.rerun() aborts the
# script before the stash can be read, and on the next run the pills widget
# still holds the same selection, so it stashes and reruns again forever.
picked: str | None = None
if not st.session_state.messages:
    picked = st.pills("Try asking:", list(SUGGESTIONS), label_visibility="collapsed")

prompt = st.chat_input(
    "Ask about your finances, or attach a statement",
    accept_file="multiple",
    file_type=UPLOAD_TYPES,
    submit_mode="disable",
)

user_text: str | None = None
uploaded_names: list[str] = []

if picked:
    user_text = SUGGESTIONS[picked]
elif prompt:
    user_text = (prompt.text or "").strip() or None
    if prompt.files:
        uploaded_names = save_uploads(prompt.files, WORKSPACE_DIR)

if user_text or uploaded_names:
    # Tell the agent where the files landed; it cannot see the upload widget.
    if uploaded_names:
        listing = ", ".join(f"/workspace/{n}" for n in uploaded_names)
        note = f"I've uploaded these files: {listing}"
        user_text = f"{user_text}\n\n{note}" if user_text else note

    assert user_text is not None
    st.session_state.messages.append({"role": "user", "content": user_text})
    with st.chat_message("user"):
        st.markdown(escape_dollars(user_text))

    completed = False
    with st.chat_message("assistant"):
        activity = st.status("Thinking", expanded=False)
        answer_slot = st.empty()
        parts: list[str] = []

        try:
            agent = load_agent(DEFAULT_MODEL)
            config = {
                "configurable": {"thread_id": st.session_state.thread_id},
                "recursion_limit": 100,
            }
            events = stream_agent_events(agent, [{"role": "user", "content": user_text}], config)

            for event in events:
                if isinstance(event, Token):
                    parts.append(event.text)
                    answer_slot.markdown(escape_dollars("".join(parts)))
                elif isinstance(event, ToolStart):
                    label = TOOL_LABELS.get(event.name, event.name)
                    activity.update(label=label)
                    activity.write(f":material/play_arrow: {label}")
                elif isinstance(event, ToolEnd):
                    if not event.ok:
                        activity.write(
                            f":material/error: {event.name} returned an error; "
                            "the agent will retry or work around it."
                        )

            answer = "".join(parts).strip()
            activity.update(label="Done", state="complete")

            if not answer:
                answer = (
                    "_The agent finished without producing a reply. "
                    "Try rephrasing, or start a new conversation._"
                )
                answer_slot.markdown(answer)

            st.session_state.messages.append({"role": "assistant", "content": answer})
            completed = True

        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            activity.update(label="Failed", state="error")
            st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")
            # Do not persist a failed turn to the transcript; the checkpointer
            # already holds whatever partial state the graph committed.

    if completed:
        # Redraw once the turn is done. The sidebar's document list and the
        # onboarding chips were both rendered from pre-turn state, so without
        # this a statement uploaded this turn stays invisible in the sidebar
        # even after the agent has read it. Safe from looping: on the redraw
        # the chat input is empty and the transcript is non-empty, so nothing
        # re-enters this block.
        st.rerun()
