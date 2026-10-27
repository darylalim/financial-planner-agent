"""Live end-to-end check against the real model and real APIs.

The pytest suite runs offline and cannot see the failures that only appear with
a real provider: tool output leaking into the answer, assistant messages running
together, or the agent claiming it saved a file it never wrote. This script
exercises each tool family against the live API and asserts on the *rendered
answer*, which is the thing the user actually reads.

Run::

    uv run python scripts/live_check.py           # every scenario
    uv run python scripts/live_check.py budget    # just one

Needs ANTHROPIC_API_KEY. The `search` scenario is skipped without TAVILY_API_KEY.
Costs real tokens -- a full run is roughly five minutes of model time.

The agent is pointed at a throwaway home directory, so your own AGENTS.md and
workspace are never touched.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Redirect the agent's filesystem root BEFORE financial_planner.config is
# imported and freezes the path. Skills are copied in so the agent can still
# load them; nothing is written back to the real agent_home.
# Nested one level down: CHECKPOINT_DB is a sibling of AGENT_HOME (outside it,
# so the agent cannot read its own transcript), so a home with no parent of its
# own would drop the database into the shared system temp root, outliving the
# run. It is named after the home, so concurrent runs no longer collide there --
# the leak is what nesting prevents. The atexit below removes the parent.
_LIVE_ROOT = Path(tempfile.mkdtemp(prefix="planner-live-"))
_LIVE_HOME = _LIVE_ROOT / "home"
_LIVE_HOME.mkdir()
shutil.copytree(PROJECT_ROOT / "agent_home" / "skills", _LIVE_HOME / "skills")
(_LIVE_HOME / "workspace").mkdir()
shutil.copy(
    PROJECT_ROOT / "examples" / "sample-transactions.csv",
    _LIVE_HOME / "workspace" / "sample-transactions.csv",
)
os.environ["FINANCIAL_PLANNER_HOME"] = str(_LIVE_HOME)
# atexit rather than a try/finally, so the temp home is removed on every exit
# path -- including the early returns for a missing key or a bad scenario name.
atexit.register(shutil.rmtree, _LIVE_ROOT, ignore_errors=True)

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from answer_checks import missing_tools, unsourced_figures  # noqa: E402
from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from financial_planner.agent import build_agent  # noqa: E402
from financial_planner.config import TAVILY_API_KEY, missing_required_keys  # noqa: E402
from financial_planner.streaming import (  # noqa: E402
    Token,
    ToolEnd,
    ToolStart,
    stream_agent_events,
)

# Text that can only have come from a tool result, never from the assistant's
# own prose. Any of these in the answer means the stream filter regressed.
LEAK_PATTERNS = [
    (r"Successfully (replaced|wrote|edited|created)", "file-tool confirmation"),
    (r'\{"[a-z_]+":', "raw tool JSON"),
    (r'"final_balance_nominal"', "calculator JSON key"),
    (r'\{\s*"error"', "error envelope"),
]

# A sentence ending immediately before a capital, or before a bold span opening
# on a capital, means two assistant messages were joined with no break.
# Requiring the capital matters: "**Savings rate 43%.**" is a period closing a
# bold span, which is ordinary markdown.
RUN_ON = re.compile(r"[a-z0-9]\.(?=[A-Z]|\*\*[A-Z])")

# Claims that a file was written. If no write tool ran, the agent described an
# intention as a completed action.
SAVE_CLAIM = re.compile(
    r"(I'(ve|ll) (saved|added|updated|recorded|written)|"
    r"(saved|written|added) (it |this )?to [`/]|"
    r"updated your profile|added .{0,40} to your profile)",
    re.IGNORECASE,
)
WRITE_TOOLS = {"write_file", "edit_file"}

# The Deep Agents read-only built-ins. `streaming._tool_message_failed` now sees
# these fail -- they report `status="error"` on the ToolMessage rather than our
# error envelope, so they used to be invisible here -- and a failure in one is
# routine: read_file on a path not written yet, grep with no hits, ls of a
# directory the agent is about to create. The agent recovers in the next step.
# Failing the scenario on those buries the signal this script exists to give
# under benign probes, so they are reported and not counted. Everything else
# stays fatal, including every tool of our own: an error envelope from
# `summarize_spending` is a real failure whatever the agent did next.
RECOVERABLE_TOOLS = {"ls", "read_file", "glob", "grep"}

SCENARIOS: dict[str, list[str]] = {
    "budget": [
        "I attached my transactions at /workspace/sample-transactions.csv. "
        "What am I spending per month, and what's my savings rate?",
    ],
    "debt": [
        "I have three debts: a credit card at $8,400 with 22.9% APR, minimum $210; "
        "a car loan at $14,200 at 6.4% APR, minimum $340; and a student loan at "
        "$21,000 at 4.5% APR, minimum $230. I can put $1,200/month total toward them. "
        "Avalanche or snowball -- and how much does the choice actually cost me?",
    ],
    "market": [
        "What's VTI trading at, and what has its actual annualized return and "
        "volatility been over the last 10 years?",
    ],
    "search": [
        "What's the 2026 401(k) employee contribution limit, and the catch-up "
        "amount if I'm over 50? Cite where you got it.",
    ],
    "retirement": [
        "I'm 41, want to retire at 65. I have $310,000 invested and add $1,900/month. "
        "I'll need about $6,500/month in today's dollars. Am I on track?",
        "What happens if I retire at 62 instead?",
    ],
}


@dataclass
class ToolCall:
    name: str
    agent: str
    output: str


def _output_text(output: Any) -> str:
    """A tool's output as text. Built-ins return a ToolMessage; ``task`` and
    ``write_todos`` return a Command whose update carries the ToolMessage."""
    update = getattr(output, "update", None)
    if isinstance(update, dict):
        return "\n".join(_output_text(m) for m in update.get("messages") or [])
    content = getattr(output, "content", output)
    return content if isinstance(content, str) else json.dumps(content, default=str)


class ToolRecorder(BaseCallbackHandler):
    """Records every tool call in every agent, with its output.

    ``stream_agent_events`` cannot supply this. Its ToolEnd carries only a name
    and a verdict, and it streams without ``subgraphs=True``, so a tool the
    ``task`` subagent runs never reaches it. Callbacks propagate into
    subgraphs, which is why the harness collects here instead of widening
    ``streaming.py``.
    """

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self._started: dict[Any, tuple[str, str]] = {}

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, **kwargs):  # noqa: ARG002
        # langchain stamps each agent's own name here: the main graph's
        # ("financial-planner") for its tools, the subagent's ("general-purpose")
        # for a delegated call.
        agent = (metadata or {}).get("lc_agent_name") or "?"
        self._started[run_id] = ((serialized or {}).get("name") or "tool", agent)

    def on_tool_end(self, output, *, run_id, **kwargs):  # noqa: ARG002
        name, agent = self._started.pop(run_id, ("tool", "?"))
        self.calls.append(ToolCall(name, agent, _output_text(output)))

    def on_tool_error(self, error, *, run_id, **kwargs):  # noqa: ARG002
        name, agent = self._started.pop(run_id, ("tool", "?"))
        self.calls.append(ToolCall(name, agent, str(error)))


def check_answer(answer: str, tools: list[str]) -> list[str]:
    """Return a problem description for each defect found in a rendered answer."""
    problems = [
        f"leaked into answer: {label}"
        for pattern, label in LEAK_PATTERNS
        if re.search(pattern, answer)
    ]
    for match in RUN_ON.finditer(answer):
        context = answer[max(0, match.start() - 25) : match.start() + 25]
        problems.append(f"missing paragraph break: ...{context}...")
    claim = SAVE_CLAIM.search(answer)
    if claim and not WRITE_TOOLS.intersection(tools):
        problems.append(f"claimed a save with no write tool: {claim.group(0)!r}")
    return problems


def run_scenario(name: str, turns: list[str]) -> bool:
    print(f"\n{'=' * 78}\nSCENARIO: {name}\n{'=' * 78}")
    agent = build_agent(checkpointer=InMemorySaver())
    recorder = ToolRecorder()
    config = {
        "configurable": {"thread_id": f"live-{name}-{uuid.uuid4()}"},
        # A graph step per node, and a tool round now crosses four (model,
        # TodoListMiddleware.after_model, tools, SkillsMiddleware.before_model --
        # the last added in deepagents 0.7.32), so this allows ~20 rounds.
        "recursion_limit": 84,
        "callbacks": [recorder],
    }

    ok = True
    ran_anywhere: set[str] = set()
    for i, prompt in enumerate(turns, 1):
        print(f"\n--- turn {i} ---\n> {prompt}\n")
        started = time.time()
        parts: list[str] = []
        tools: list[str] = []
        failed_tools: list[str] = []
        recorder.calls.clear()

        for event in stream_agent_events(agent, [{"role": "user", "content": prompt}], config):
            if isinstance(event, Token):
                parts.append(event.text)
            elif isinstance(event, ToolStart):
                tools.append(event.name)
                print(f"  [tool] {event.name}")
            elif isinstance(event, ToolEnd) and not event.ok:
                failed_tools.append(event.name)

        answer = "".join(parts)
        problems = check_answer(answer, tools)
        fatal = [name for name in failed_tools if name not in RECOVERABLE_TOOLS]
        recovered = [name for name in failed_tools if name in RECOVERABLE_TOOLS]
        if fatal:
            problems.append(f"tools returned errors: {fatal}")

        ran_anywhere.update(call.name for call in recorder.calls)
        delegated = [f"{c.name} ({c.agent})" for c in recorder.calls if c.agent != agent.name]
        # Reported, not failed: the Phase 0 run calibrates the false-positive
        # rate (a figure fairly restated from an earlier turn is flagged), and
        # whether this becomes a failure is decided once that rate is known.
        unsourced = unsourced_figures(answer, [c.output for c in recorder.calls], prompt)

        print(f"\n  tools: {tools}")
        if delegated:
            print(f"  delegated: {delegated}")
        print(f"  elapsed: {time.time() - started:.1f}s   answer chars: {len(answer)}")
        print(f"  unsourced figures: {len(unsourced)} {unsourced if unsourced else ''}")
        for name in recovered:
            print(f"  ~~ {name} returned an error the agent could recover from")
        for problem in problems:
            print(f"  !! {problem}")
        if not problems:
            print("  checks: clean")
        ok = ok and not problems
        print(f"\n--- answer ---\n{answer}\n")

    missing = missing_tools(name, ran_anywhere)
    if missing:
        print(f"  !! expected tools never ran, in any agent: {missing}")
        ok = False
    return ok


def main() -> int:
    missing = missing_required_keys()
    if missing:
        print(f"Cannot run: {', '.join(missing)} is not set.")
        return 2

    requested = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in requested if n not in SCENARIOS]
    if unknown:
        print(f"Unknown scenario(s): {unknown}. Available: {list(SCENARIOS)}")
        return 2

    if "search" in requested and not TAVILY_API_KEY:
        print("Skipping 'search': TAVILY_API_KEY is not set.")
        requested = [n for n in requested if n != "search"]

    # Skipping can empty the set -- `live_check.py search` with no Tavily key.
    # Without this the summary below reduces to `all({})`, which is True, and
    # the script exits 0: a wrapper or CI step reads a run that verified
    # nothing as a pass. Nothing ran, so this is a "cannot run" (2), not a
    # "found problems" (1).
    if not requested:
        print("Cannot run: every requested scenario was skipped, so nothing was checked.")
        return 2

    results = {name: run_scenario(name, SCENARIOS[name]) for name in requested}

    print(f"\n{'=' * 78}\nSUMMARY")
    for name, passed in results.items():
        print(f"  {name:12s} {'clean' if passed else 'PROBLEM'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
