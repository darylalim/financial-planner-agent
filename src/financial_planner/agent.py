"""Agent construction.

Assembles the Deep Agent from the four pieces the harness does not supply:
the model, the custom tools, the system prompt, and the storage backend.

Storage design -- two kinds of persistence, deliberately separated:

* **Files** live on real disk via ``FilesystemBackend``, rooted at
  ``agent_home/``. That is what makes the household's profile and generated
  plans survive a restart. The default ``StateBackend`` would keep them only
  for the life of a thread, which fails the "tracks progress across sessions"
  requirement outright.
* **Conversation and todo state** live in a SQLite checkpointer keyed by
  ``thread_id``, so an interrupted planning session resumes where it stopped.

Middleware -- two deliberate departures from what ``create_deep_agent``
assembles on its own. Both are passed through ``middleware=``, which is
*additive*: it splices into the default stack rather than replacing it, so the
skills and memory middleware configured below survive untouched. The one rule
is that a custom middleware whose ``.name`` matches a default replaces that
default in place -- which is exactly how the filesystem narrowing works, and
exactly why ``test_agent.py`` pins the resulting tool set.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from deepagents import FilesystemMiddleware, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import TodoListMiddleware
from langgraph.checkpoint.sqlite import SqliteSaver

from financial_planner.config import (
    AGENT_HOME,
    CHECKPOINT_DB,
    DEFAULT_MODEL,
    VIRTUAL_MEMORY,
    VIRTUAL_SKILLS,
    ensure_directories,
)
from financial_planner.prompts import SYSTEM_PROMPT
from financial_planner.tools import ALL_TOOLS

DEFAULT_PROFILE = """\
# Household profile

The planner keeps this file current. It loads at the start of every session, so
anything recorded here carries across conversations.

## Household
_Not yet recorded._

## Goals
_Not yet recorded._

## Accounts
_Not yet recorded._

## Assumptions in use
- Long-run equity return: 7% nominal
- Inflation: 2.5%

## Decisions already made
_Nothing yet._
"""


def _ensure_profile() -> None:
    """Create AGENTS.md if absent.

    The memory middleware reads this path at startup; a missing file means the
    agent silently starts every session with no household context.
    """
    ensure_directories()
    profile = AGENT_HOME / "AGENTS.md"
    if not profile.exists():
        profile.write_text(DEFAULT_PROFILE, encoding="utf-8")


def build_checkpointer() -> SqliteSaver:
    """Open the SQLite checkpointer used for conversation/todo state.

    ``check_same_thread=False`` is required because Streamlit serves each rerun
    from a worker thread, while the connection is cached across reruns.
    """
    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CHECKPOINT_DB), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def build_agent(
    *,
    model: str | Any = DEFAULT_MODEL,
    checkpointer: Any | None = None,
) -> Any:
    """Construct the financial planning deep agent.

    Args:
        model: Provider-prefixed model string (``"anthropic:claude-opus-5"``) or
            an instantiated chat model.
        checkpointer: Optional checkpointer. One is created if not supplied;
            pass a shared instance from the UI layer so it survives reruns.

    Returns:
        A compiled agent. Invoke with a ``thread_id`` in the config:
        ``agent.invoke({"messages": [...]}, {"configurable": {"thread_id": "..."}})``
    """
    _ensure_profile()

    # virtual_mode=True re-roots the agent at AGENT_HOME and blocks traversal
    # above it, so the repository's .env and source are outside its reach.
    backend = FilesystemBackend(root_dir=AGENT_HOME, virtual_mode=True)

    return create_deep_agent(
        model=model,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        skills=[VIRTUAL_SKILLS],
        memory=[VIRTUAL_MEMORY],
        middleware=[
            # Narrows the built-in filesystem tools. Matched by `.name`, this
            # *replaces* the FilesystemMiddleware create_deep_agent would have
            # built, so it must receive the same backend instance.
            #
            # `delete` is dropped because nothing in this product needs it: the
            # agent appends to /workspace/ and edits /AGENTS.md, never removes.
            # Leaving it bound would hand recursive deletion over the user's
            # bank statements to an agent that also ingests prompt-injectable
            # PDFs -- and there is no approval gate in the UI to catch it.
            # `execute` is dropped because FilesystemBackend is not a sandbox
            # backend, so it could only ever return an error; the exclusion
            # keeps it out of the tool node instead of relying on the runtime
            # capability filter to hide it from each request.
            #
            # Omitted tools never reach the dispatchable tool node, so this is
            # a real removal rather than a hidden-from-the-model schema filter.
            FilesystemMiddleware(
                backend=backend,
                tools=["ls", "read_file", "write_file", "edit_file", "glob", "grep"],
            ),
            # deepagents 0.7 dropped todo planning from its default stack, so
            # `write_todos` has to be asked for explicitly. The system prompt
            # tells the agent to plan multi-step work visibly and the UI has a
            # status label for it, neither of which meant anything while the
            # tool went unbound. Brings its own `todos` state channel, which
            # the SQLite checkpointer then persists per thread.
            TodoListMiddleware(),
        ],
        checkpointer=checkpointer if checkpointer is not None else build_checkpointer(),
        name="financial-planner",
    )
