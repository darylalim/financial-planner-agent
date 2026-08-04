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

from pathlib import Path

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


def save_uploads(files) -> list[str]:
    """Write uploaded files into the agent's workspace.

    Filenames come from the browser and are untrusted, so only the final path
    component is used -- an upload named ``../../.env`` must not escape.
    """
    saved: list[str] = []
    for item in files:
        safe_name = Path(item.name).name
        if not safe_name or safe_name in (".", ".."):
            continue
        destination = WORKSPACE_DIR / safe_name
        destination.write_bytes(item.getvalue())
        saved.append(safe_name)
    return saved


def new_thread_id() -> str:
    """Mint a thread id. Time-based so threads sort chronologically."""
    import datetime as _dt

    return f"session-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"


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
if not st.session_state.messages:
    picked = st.pills("Try asking:", list(SUGGESTIONS), label_visibility="collapsed")
    if picked:
        st.session_state.pending_prompt = SUGGESTIONS[picked]
        st.rerun()

prompt = st.chat_input(
    "Ask about your finances, or attach a statement",
    accept_file="multiple",
    file_type=UPLOAD_TYPES,
    submit_mode="disable",
)

# A suggestion chip queues its prompt for the next run.
pending = st.session_state.pop("pending_prompt", None)

user_text: str | None = None
uploaded_names: list[str] = []

if pending:
    user_text = pending
elif prompt:
    user_text = (prompt.text or "").strip() or None
    if prompt.files:
        uploaded_names = save_uploads(prompt.files)

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

        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            activity.update(label="Failed", state="error")
            st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")
            # Do not persist a failed turn to the transcript; the checkpointer
            # already holds whatever partial state the graph committed.
