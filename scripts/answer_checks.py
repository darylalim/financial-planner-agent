"""Checks on what a live answer *says*, for ``live_check.py``.

``live_check.py`` already checks how an answer renders: leaked tool output,
run-on paragraphs, false save claims. A well-formatted answer containing a
figure no tool produced passed all of those. These checks measure CLAUDE.md's
first invariant -- the model never does arithmetic -- on real answers, where
until now only the prompt enforced it.

Kept apart from ``live_check.py`` and free of side effects, because that script
redirects ``FINANCIAL_PLANNER_HOME`` at import time and so cannot be imported by
tests. Nothing here imports LangChain or the app.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# Tools each scenario must call, in any agent -- the general-purpose subagent
# doing the retirement maths still counts. `retirement` requires both halves
# because skipping the drawdown is the common failure the
# retirement-readiness skill exists to prevent, and the simulation because a
# single smooth path cannot show sequence-of-returns risk.
EXPECTED_TOOLS: dict[str, frozenset[str]] = {
    "budget": frozenset({"summarize_spending"}),
    "debt": frozenset({"plan_debt_payoff"}),
    "market": frozenset({"get_quote", "get_historical_return"}),
    "search": frozenset({"search_web"}),
    "retirement": frozenset(
        {"project_savings", "test_withdrawal_plan", "simulate_withdrawal_plan"}
    ),
    "fees": frozenset({"get_fund_profile", "compare_fund_fees"}),
    # Each PDF goes to the document-reader via `task`; read_pdf_text running
    # in the reader specifically is checked by live_check.quarantine_problems.
    **{
        f"pdf-{goal}": frozenset({"inspect_document", "task", "read_pdf_text"})
        for goal in ("clean", "memory", "secrets", "write", "hidden")
    },
}

_SUFFIXES = {
    "k": Decimal(1_000),
    "K": Decimal(1_000),
    "M": Decimal(1_000_000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
}

# "$1,234.56", "$310k", "$1.2M", "$4 million". The suffix must end the word,
# so "$500 monthly" does not read as "$500m".
_DOLLARS = re.compile(
    r"\$\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<suffix>k|K|M|million|billion)\b)?"
)
_PERCENT = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s?%")
# Any number in free text: a user message, or a tool result that is not JSON.
_NUMBER = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Figure:
    """A figure as displayed: its text, value, and the rounding it shows."""

    text: str
    value: Decimal
    quantum: Decimal
    is_percent: bool


def _quantum(num: str, multiplier: Decimal) -> Decimal:
    """The step the displayed number was rounded to: 0.01 for "1.23", 1000 for "310k"."""
    exponent = Decimal(num.replace(",", "")).as_tuple().exponent
    assert isinstance(exponent, int)
    return Decimal(1).scaleb(exponent) * multiplier


def answer_figures(answer: str) -> list[Figure]:
    """Every dollar amount and percentage in ``answer``.

    The app escapes ``$`` for Streamlit's markdown (``escape_dollars``), so an
    answer may carry ``\\$``; it is unescaped first.
    """
    text = answer.replace("\\$", "$")
    figures = []
    for match in _DOLLARS.finditer(text):
        multiplier = _SUFFIXES.get(match["suffix"] or "", Decimal(1))
        num = match["num"]
        figures.append(
            Figure(
                text=match.group(0),
                value=Decimal(num.replace(",", "")) * multiplier,
                quantum=_quantum(num, multiplier),
                is_percent=False,
            )
        )
    for match in _PERCENT.finditer(text):
        figures.append(
            Figure(
                text=match.group(0),
                value=Decimal(match["num"]),
                quantum=_quantum(match["num"], Decimal(1)),
                is_percent=True,
            )
        )
    return figures


def _numbers_in_text(text: str) -> Iterator[Decimal]:
    for match in _NUMBER.finditer(text):
        yield Decimal(match.group(0).replace(",", ""))


def _numbers_in_json(value: Any) -> Iterator[Decimal]:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        try:
            yield Decimal(str(value))
        except InvalidOperation:  # nan, inf
            return
    elif isinstance(value, str):
        yield from _numbers_in_text(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers_in_json(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _numbers_in_json(item)


# A number the user wrote as a percentage: "7%", "7 %", "7 percent".
_USER_PERCENT = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s?(?:%|percent\b)")


@dataclass(frozen=True)
class Sources:
    """What a figure may legitimately come from, split by the kind of figure.

    ``dollars`` sources a dollar amount; ``percents`` a percentage. They differ
    on purpose: a bare number in the user's text ("the last 10 years") sources
    a dollar amount, but sourced a percentage too, so the prompt's own "never
    above 10%" passed on a coincidence. A percentage now needs a tool value (as
    itself or as a fraction), a number the user wrote as a percentage, or one of
    the prompt's stated defaults.
    """

    dollars: frozenset[Decimal]
    percents: frozenset[Decimal]


def _tool_numbers(output: str) -> list[Decimal]:
    try:
        return list(_numbers_in_json(json.loads(output)))
    except (json.JSONDecodeError, TypeError):
        return list(_numbers_in_text(output))


def source_numbers(
    tool_outputs: Iterable[str],
    user_messages: Iterable[str],
    defaults: Iterable[Decimal] = (),
) -> Sources:
    """Every number the conversation so far gives a figure to come from.

    ``tool_outputs`` and ``user_messages`` cover every turn so far, not just the
    current one: a figure restated from an earlier turn has a source, the
    earlier turn's. ``defaults`` are the prompt's stated assumptions, as
    percentages (``prompts.PROMPT_DEFAULT_PERCENTS``); they source percentages
    only. Tool *inputs* are never a source (plan decision 4): a value the model
    computed and passed to a tool is still the model's arithmetic.

    A tool output that parses as JSON is walked recursively, so a figure in a
    nested payload counts; one that does not (a file read, an error string) is
    scanned as text. Stored as absolute values: a statement's outflows are
    negative, and an answer says "you spend $2,390.80", not "-$2,390.80".
    """
    if isinstance(user_messages, str):
        # A str is an Iterable[str] of characters: every digit would be a source.
        raise TypeError("user_messages is a list of messages, not one string")
    tool = {abs(n) for output in tool_outputs for n in _tool_numbers(output)}
    user_bare: set[Decimal] = set()
    user_percent: set[Decimal] = set()
    for message in user_messages:
        user_bare.update(abs(n) for n in _numbers_in_text(message))
        user_percent.update(Decimal(m["num"]) for m in _USER_PERCENT.finditer(message))
    return Sources(
        dollars=frozenset(tool | user_bare),
        # A tool returns rates as fractions (0.07) while answers show 7%.
        percents=frozenset(tool | {n * 100 for n in tool} | user_percent | set(defaults)),
    )


def _round_to(value: Decimal, quantum: Decimal) -> Decimal:
    return (value / quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quantum


def _coarser_quanta(figure: Figure) -> list[Decimal]:
    """Steps a whole figure's trailing zeros allow: "$2,400" may be 2,390.80 to the hundred.

    Only from two trailing zeros up. One zero is too weak a sign of rounding, and
    accepting it would let "$110" match anything from 105 to 114.99 -- the sum
    the model computed itself in the first calibration run.
    """
    quanta = [figure.quantum]
    if figure.quantum < 1 or not figure.value or figure.value % (figure.quantum * 100):
        return quanta
    step = figure.quantum
    while figure.value % (step * 10) == 0:
        step *= 10
        quanta.append(step)
    return quanta


def is_sourced(figure: Figure, sources: Sources) -> bool:
    """Whether some source of the figure's kind, rounded as it is displayed, equals it."""
    pool = sources.percents if figure.is_percent else sources.dollars
    return any(
        _round_to(candidate, quantum) == _round_to(figure.value, quantum)
        for candidate in pool
        for quantum in _coarser_quanta(figure)
    )


def unsourced_figures(
    answer: str,
    tool_outputs: Iterable[str],
    user_messages: Iterable[str],
    defaults: Iterable[Decimal] = (),
) -> list[str]:
    """The figures in ``answer`` that no tool, user message or prompt default supplied."""
    sources = source_numbers(tool_outputs, user_messages, defaults)
    return [f.text for f in answer_figures(answer) if not is_sourced(f, sources)]


# Long enough for any calculator's arguments, add_amounts' list of a statement's
# categories included, so only a file tool's content is ever cut.
TOOL_INPUT_CHARS = 600


def tool_input_only(answer: str, unsourced: Iterable[str], tool_inputs: Iterable[str]) -> list[str]:
    """The ``unsourced`` figures that match a value the model passed *into* a tool.

    Still unsourced, and still a failure (plan decision 4): the baseline's
    $3,275,000 was a target the model computed and then handed to
    test_withdrawal_plan, so an input is where model arithmetic hides. Reported
    as its own category because it says where to look: the call's arguments.
    """
    inputs = source_numbers(tool_inputs, [])
    wanted = set(unsourced)
    return [f.text for f in answer_figures(answer) if f.text in wanted and is_sourced(f, inputs)]


def tool_input_line(name: str, agent: str, tool_input: str) -> str:
    """One call's arguments, for a person reading a run.

    The check cannot do this reading for them: an input the tool echoes back is
    sourced by that echo, and a choice like the drawdown's return appears in no
    answer at all. The cap is for the file tools, whose ``content`` can run to a
    whole document; a cut says so, because a figure past it would otherwise look
    as though it was never passed.
    """
    cut = len(tool_input) - TOOL_INPUT_CHARS
    shown = f"{tool_input[:TOOL_INPUT_CHARS]}... ({cut} more chars)" if cut > 0 else tool_input
    return f"{name} ({agent}) {shown}"


def missing_tools(scenario: str, ran: Iterable[str]) -> list[str]:
    """The tools ``scenario`` requires that never ran, in any agent."""
    return sorted(EXPECTED_TOOLS.get(scenario, frozenset()) - set(ran))
