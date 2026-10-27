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
# retirement-readiness skill exists to prevent.
EXPECTED_TOOLS: dict[str, frozenset[str]] = {
    "budget": frozenset({"summarize_spending"}),
    "debt": frozenset({"plan_debt_payoff"}),
    "market": frozenset({"get_quote", "get_historical_return"}),
    "search": frozenset({"search_web"}),
    "retirement": frozenset({"project_savings", "test_withdrawal_plan"}),
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


def source_numbers(tool_outputs: Iterable[str], user_message: str) -> set[Decimal]:
    """Every number a turn's figures may legitimately come from.

    A tool output that parses as JSON is walked recursively, so a figure in a
    nested payload counts; one that does not (a file read, an error string) is
    scanned as text. Stored as absolute values: a statement's outflows are
    negative, and an answer says "you spend $2,390.80", not "-$2,390.80".
    """
    numbers: set[Decimal] = set()
    for output in tool_outputs:
        try:
            found = list(_numbers_in_json(json.loads(output)))
        except (json.JSONDecodeError, TypeError):
            found = list(_numbers_in_text(output))
        numbers.update(abs(n) for n in found)
    numbers.update(abs(n) for n in _numbers_in_text(user_message))
    return numbers


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


def is_sourced(figure: Figure, sources: set[Decimal]) -> bool:
    """Whether some source, rounded as the figure is displayed, equals it.

    A percentage also matches a fraction (0.07 is 7%), since tools return rates
    as fractions while answers show percentages.
    """
    for source in sources:
        candidates = [source, source * 100] if figure.is_percent else [source]
        for candidate in candidates:
            for quantum in _coarser_quanta(figure):
                if _round_to(candidate, quantum) == _round_to(figure.value, quantum):
                    return True
    return False


def unsourced_figures(answer: str, tool_outputs: Iterable[str], user_message: str) -> list[str]:
    """The figures in ``answer`` that neither a tool result nor the user supplied."""
    sources = source_numbers(tool_outputs, user_message)
    return [f.text for f in answer_figures(answer) if not is_sourced(f, sources)]


def missing_tools(scenario: str, ran: Iterable[str]) -> list[str]:
    """The tools ``scenario`` requires that never ran, in any agent."""
    return sorted(EXPECTED_TOOLS.get(scenario, frozenset()) - set(ran))
