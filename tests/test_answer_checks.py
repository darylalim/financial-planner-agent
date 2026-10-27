"""Tests for ``scripts/answer_checks.py``, the live check's content checks.

The live check itself spends real money and is not in the gate, so the matching
it relies on is pinned here, offline. Loaded by path: ``scripts/`` is not a
package, and its sibling ``live_check.py`` must never be imported (it redirects
``FINANCIAL_PLANNER_HOME`` at import time).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from financial_planner.tools import ALL_TOOLS

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "answer_checks.py"
_spec = importlib.util.spec_from_file_location("answer_checks", _PATH)
assert _spec is not None and _spec.loader is not None
checks = importlib.util.module_from_spec(_spec)
# Registered before exec so the frozen dataclass can resolve its own module.
sys.modules["answer_checks"] = checks
_spec.loader.exec_module(checks)


def _unsourced(answer: str, *tool_payloads: object, user: str = "") -> list[str]:
    outputs = [p if isinstance(p, str) else json.dumps(p) for p in tool_payloads]
    return checks.unsourced_figures(answer, outputs, user)


class TestFigureExtraction:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("$2,390.80", Decimal("2390.80")),
            ("$1,234,567", Decimal("1234567")),
            ("$310k", Decimal("310000")),
            ("$1.2M", Decimal("1200000")),
            ("$4 million", Decimal("4000000")),
            ("\\$4,200", Decimal("4200")),
        ],
    )
    def test_dollar_amounts_are_read_with_separators_and_suffixes(self, text, value):
        [figure] = checks.answer_figures(f"You have {text} left.")
        assert figure.value == value
        assert not figure.is_percent

    def test_a_suffix_must_end_the_word(self):
        """ "$500 monthly" is five hundred dollars, not five hundred million."""
        [figure] = checks.answer_figures("about $500 monthly")
        assert figure.value == Decimal(500)

    def test_percentages_are_read(self):
        [figure] = checks.answer_figures("a savings rate of 43.08%")
        assert figure.is_percent
        assert figure.value == Decimal("43.08")

    def test_the_displayed_precision_is_kept(self):
        [figure] = checks.answer_figures("$1.2M")
        assert figure.quantum == Decimal(100_000)


class TestProvenance:
    def test_an_exact_figure_from_a_tool_is_sourced(self):
        assert _unsourced("You spend $2,390.80 a month.", {"monthly_spend": 2390.8}) == []

    def test_a_figure_rounded_from_a_tool_value_is_sourced(self):
        assert _unsourced("About $1,809 a month.", {"saved": 1809.2}) == []

    def test_a_nested_value_is_found(self):
        payload = {"categories": [{"name": "Housing", "monthly": -1850.0}]}
        assert _unsourced("Housing is $1,850.", payload) == []

    def test_a_negative_outflow_matches_a_positive_figure(self):
        assert _unsourced("Interest costs $44.61.", {"interest": -44.61}) == []

    def test_a_fraction_matches_its_percentage(self):
        assert _unsourced("a 7% return", {"annualized_return": 0.07}) == []
        assert _unsourced("a savings rate of 43.08%", {"savings_rate": 0.4308}) == []

    def test_trailing_zeros_allow_the_rounding_they_show(self):
        assert _unsourced("roughly $2,400 a month", {"monthly_spend": 2390.8}) == []

    def test_one_trailing_zero_is_not_read_as_rounding(self):
        """The first calibration run's own case: the model added 94.35 + 15.99
        itself and wrote "about $110". Rounding to the ten would hide it."""
        assert _unsourced("about $110", {"dining": 94.35, "subs": 15.99}) == ["$110"]

    def test_a_figure_only_in_the_user_message_is_sourced(self):
        assert _unsourced("With $310k invested", user="I have $310,000 invested") == []

    def test_an_unsourced_figure_is_reported(self):
        assert _unsourced("That is $9,999 a year.", {"monthly_spend": 2390.8}) == ["$9,999"]

    def test_a_tool_result_that_is_not_json_is_scanned_as_text(self):
        assert _unsourced("Rent was $1,850.", "2026-01-03,Rent,-1850.00") == []

    def test_a_figure_from_another_turn_is_not_a_source(self):
        """Provenance is per turn; a restated earlier figure is flagged. That is
        a known false positive the Phase 0 run exists to measure."""
        assert _unsourced("As before, $2,390.80.") == ["$2,390.80"]


class TestExpectedTools:
    def test_every_expected_tool_exists(self):
        """A misspelled name would fail the scenario on every run, forever."""
        names = {tool.name for tool in ALL_TOOLS}
        for scenario, expected in checks.EXPECTED_TOOLS.items():
            assert expected <= names, scenario

    def test_a_tool_that_ran_in_any_agent_counts(self):
        assert checks.missing_tools("retirement", ["project_savings", "test_withdrawal_plan"]) == []

    def test_a_missing_tool_is_reported(self):
        assert checks.missing_tools("retirement", ["project_savings"]) == ["test_withdrawal_plan"]

    def test_a_scenario_with_no_expectation_requires_nothing(self):
        assert checks.missing_tools("not-a-scenario", []) == []
