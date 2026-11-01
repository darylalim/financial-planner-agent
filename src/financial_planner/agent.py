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

Middleware -- three deliberate departures from what ``create_deep_agent``
assembles on its own. All three are passed through ``middleware=``, which is
*additive*: it splices into the default stack rather than replacing it, so the
skills and memory middleware configured below survive untouched. The one rule
is that a custom middleware whose ``.name`` matches a default replaces that
default in place -- which is exactly how the filesystem narrowing and the
finance summary prompt work, and exactly why ``test_agent.py`` pins the
resulting tool set and summarizer.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from deepagents import FilesystemMiddleware, create_deep_agent
from deepagents._models import resolve_model
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.summarization import create_summarization_middleware
from langchain.agents.middleware import TodoListMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite import SqliteSaver

from financial_planner.config import (
    AGENT_HOME,
    ANTHROPIC_EFFORT,
    CHECKPOINT_DB,
    DEFAULT_MODEL,
    THINKING_BINDING_BETA,
    THINKING_BINDING_MODELS,
    VIRTUAL_MEMORY,
    VIRTUAL_SKILLS,
    ensure_directories,
)
from financial_planner.prompts import FINANCE_SUMMARY_PROMPT, SYSTEM_PROMPT
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


# Every PDF, at any depth and in any letter case. deepagents lowercases an
# extension to decide a file is a PDF, but its permission matcher is
# case-sensitive, so `*.pdf` alone would let `Statement.PDF` through as a whole
# document block. The second pattern exists because the matcher's `*` skips a
# leading dot: `.statement.pdf` matches only it. Its `**` also skips dot
# *directories*, which no pattern list can cover at every depth -- and nothing
# needs it to: an untrusted PDF reaches disk only through `uploads.py`, which
# saves it flat into /workspace/ under its final name component.
_PDF = "[pP][dD][fF]"
PDF_READ_DENY = FilesystemPermission(
    operations=["read"], paths=[f"/**/*.{_PDF}", f"/**/.*.{_PDF}"], mode="deny"
)


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


def _anthropic_model_name(spec: str) -> str | None:
    """The bare Claude model name in ``spec``, or ``None`` if it is not Anthropic's.

    Mirrors init_chat_model's inference: an unprefixed ``claude-...`` is Anthropic.
    """
    provider, _, name = spec.rpartition(":")
    if provider == "anthropic" or (not provider and name.startswith("claude")):
        return name
    return None


def build_model(spec: str) -> BaseChatModel:
    """Turn a provider-prefixed model string into the chat model the agent runs on.

    Anthropic models get an explicit effort, and the ones that run preserved
    thinking's history check also get ``drop_block`` (see
    ``config.THINKING_BINDING_MODELS``). The whole ``thinking`` dict is sent, not
    just ``block_binding``: ChatAnthropic adds its adaptive-thinking default only
    when ``thinking`` is unset, so a dict holding only the binding would silently
    turn adaptive thinking off. ``display: "omitted"`` because the app never
    renders thinking, so a summary of it would only make saved conversations
    bigger.

    Every other provider is resolved as deepagents would resolve the string,
    since these kwargs are Anthropic's and would break, say, an ``openai:`` model.
    """
    name = _anthropic_model_name(spec)
    if name is None:
        return resolve_model(spec)
    kwargs: dict[str, Any] = {"effort": ANTHROPIC_EFFORT}
    if name in THINKING_BINDING_MODELS:
        kwargs["thinking"] = {
            "type": "adaptive",
            "display": "omitted",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        }
        kwargs["betas"] = [THINKING_BINDING_BETA]
    return init_chat_model(spec, **kwargs)


def build_agent(
    *,
    model: str | BaseChatModel = DEFAULT_MODEL,
    checkpointer: Any | None = None,
) -> Any:
    """Construct the financial planning deep agent.

    Args:
        model: A provider-prefixed model string (default: ``config.DEFAULT_MODEL``),
            built by ``build_model``, or an instantiated chat model used as is.
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

    # Resolved once, here, so the agent and its summarizer share one instance --
    # and with it the effort and thinking settings. The summarizer factory also
    # refuses a string. An injected test model passes straight through, so no
    # real chat model is ever built behind a test's back.
    if isinstance(model, str):
        model = build_model(model)

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
            #
            # PDF_READ_DENY keeps PDFs away from the built-in tools: `read_file`
            # on one hands the model the whole document, scanned pages
            # included, past every check the PDF tools make. "read" covers
            # `ls`, `glob` and `grep` too, and listings filter a denied path out
            # rather than failing, so the agent does not see PDFs there at all
            # -- app.py names the files on file in the user's message instead.
            # The `general-purpose` subagent inherits this instance by name.
            FilesystemMiddleware(
                backend=backend,
                tools=["ls", "read_file", "write_file", "edit_file", "glob", "grep"],
                permissions=[PDF_READ_DENY],
            ),
            # deepagents 0.7 dropped todo planning from its default stack, so
            # `write_todos` has to be asked for explicitly. The system prompt
            # tells the agent to plan multi-step work visibly and the UI has a
            # status label for it, neither of which meant anything while the
            # tool went unbound. Brings its own `todos` state channel, which
            # the SQLite checkpointer then persists per thread.
            TodoListMiddleware(),
            # Replaces the default summarizer in place, by name, to change only
            # its prompt: the default is written for coding sessions and says
            # nothing about keeping figures exact, and a figure misstated in a
            # summary re-enters the conversation with no tool behind it. The
            # `general-purpose` subagent inherits it through the same name
            # match. Matching is by class name, so a rename -- or a subclass,
            # whose name differs -- fails open to the default prompt;
            # TestSummarizerIsFinanceAware pins both graphs.
            create_summarization_middleware(model, backend, summary_prompt=FINANCE_SUMMARY_PROMPT),
        ],
        checkpointer=checkpointer if checkpointer is not None else build_checkpointer(),
        name="financial-planner",
    )
