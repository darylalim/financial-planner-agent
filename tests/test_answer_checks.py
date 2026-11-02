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

from financial_planner.prompts import PROMPT_DEFAULT_PERCENTS, SYSTEM_PROMPT
from financial_planner.tools import ALL_TOOLS, READER_TOOLS

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "answer_checks.py"
_spec = importlib.util.spec_from_file_location("answer_checks", _PATH)
assert _spec is not None and _spec.loader is not None
checks = importlib.util.module_from_spec(_spec)
# Registered before exec so the frozen dataclass can resolve its own module.
sys.modules["answer_checks"] = checks
_spec.loader.exec_module(checks)


def _unsourced(
    answer: str,
    *tool_payloads: object,
    user: str = "",
    earlier: tuple[str, ...] = (),
    defaults: tuple[Decimal, ...] = (),
) -> list[str]:
    outputs = [p if isinstance(p, str) else json.dumps(p) for p in tool_payloads]
    said = [*earlier, user] if user else list(earlier)
    return checks.unsourced_figures(answer, outputs, said, defaults)


# Recorded scenario wording (scripts/live_check.py SCENARIOS), so these examples
# are the ones Phase 1's batches actually produced.
RETIREMENT_TURN_1 = (
    "I'm 41, want to retire at 65. I have $310,000 invested and add $1,900/month. "
    "I'll need about $6,500/month in today's dollars. Am I on track?"
)
RETIREMENT_TURN_2 = "What happens if I retire at 62 instead?"
MARKET = (
    "What's VTI trading at, and what has its actual annualized return and "
    "volatility been over the last 10 years?"
)


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

    def test_a_figure_from_no_turn_at_all_is_not_sourced(self):
        assert _unsourced("As before, $2,390.80.") == ["$2,390.80"]

    def test_one_string_for_the_user_messages_is_refused(self):
        """A str is an iterable of characters, so every digit would be a source."""
        with pytest.raises(TypeError):
            checks.unsourced_figures("$9", [], "I have $9")


class TestEarlierTurns:
    """Plan decision 3. In Phase 1's after-fix batch, 13 of retirement's 20
    unsourced figures were turn 2 restating turn 1."""

    def test_a_figure_from_an_earlier_user_message_is_sourced(self):
        answer = "Assumptions: same as before, a flat $1,900/month."
        assert _unsourced(answer, user=RETIREMENT_TURN_2, earlier=(RETIREMENT_TURN_1,)) == []

    def test_a_figure_from_an_earlier_tool_output_is_sourced(self):
        """Tool outputs are passed for the whole conversation, so turn 1's
        project_savings result sources turn 2's restatement of it."""
        turn_1_output = {"ending_balance": 1625950.0, "monthly_contribution": 1900}
        answer = "At 65 you'd have had $1,625,950 ($1.63M)."
        assert _unsourced(answer, turn_1_output, user=RETIREMENT_TURN_2) == []

    def test_a_figure_in_no_turn_is_still_unsourced(self):
        answer = "You'd need $3,500/month to close the gap."
        assert _unsourced(answer, user=RETIREMENT_TURN_2, earlier=(RETIREMENT_TURN_1,)) == [
            "$3,500"
        ]


class TestPromptDefaults:
    """Plan decision 3: the rates SYSTEM_PROMPT tells the model to assume."""

    def test_the_equity_default_is_sourced(self):
        answer = "I'll keep using a 7% nominal return (your current assumption)."
        assert _unsourced(answer, user=MARKET) == ["7%"]
        assert _unsourced(answer, user=MARKET, defaults=PROMPT_DEFAULT_PERCENTS) == []

    def test_the_inflation_default_is_sourced(self):
        answer = "Assumptions: 7% return, 2.5% inflation."
        assert _unsourced(answer, user=RETIREMENT_TURN_1, defaults=PROMPT_DEFAULT_PERCENTS) == []

    def test_a_default_sources_only_a_percentage(self):
        assert _unsourced("about $7", defaults=PROMPT_DEFAULT_PERCENTS) == ["$7"]

    def test_the_constants_are_what_the_prompt_says(self):
        for percent in PROMPT_DEFAULT_PERCENTS:
            assert f"{percent}%" in SYSTEM_PROMPT


class TestAPercentageNeedsAPercentageSource:
    """The baseline's coincidental match: "never go above 10%" (the prompt's
    cap) passed because the question said "the last 10 years"."""

    def test_a_bare_number_in_the_question_does_not_source_a_percentage(self):
        assert _unsourced("I'd never go above 10%.", user=MARKET) == ["10%"]

    def test_the_cap_is_sourced_by_the_prompt_default_instead(self):
        answer = "I'd never go above 10%."
        assert _unsourced(answer, user=MARKET, defaults=PROMPT_DEFAULT_PERCENTS) == []

    @pytest.mark.parametrize("said", ["assume 6% a year", "assume 6 % a year", "6 percent"])
    def test_a_percentage_the_user_wrote_is_sourced(self, said):
        assert _unsourced("At 6%, you'd have more.", user=said) == []

    def test_a_bare_number_still_sources_a_dollar_amount(self):
        assert _unsourced("a $500 contribution", user="I add 500 a month") == []


class TestToolInputsAreNotASource:
    """Plan decision 4. The baseline's case: the model computed a $3,275,000
    target and passed it to test_withdrawal_plan. The checker is never given
    tool inputs, so a figure that is only an input stays unsourced."""

    def test_a_figure_only_in_a_tool_input_is_unsourced(self):
        output = {"success_rate_note": "lasts to age 95", "ending_balance": 412000.0}
        answer = "With a $3,275,000 target, the money lasts to 95."
        assert _unsourced(answer, output, user=RETIREMENT_TURN_1) == ["$3,275,000"]

    def test_it_is_reported_as_a_tool_input(self):
        """Still unsourced, but named, so the failure points at the call's arguments.
        The recorder keeps a call's input as LangChain's str() of the args."""
        answer = "With a $3,275,000 target and $1,900 saved monthly, it lasts to 95."
        unsourced = _unsourced(answer)
        inputs = [str({"portfolio_value": 3275000.0, "years": 30, "annual_return": 0.05})]
        assert unsourced == ["$3,275,000", "$1,900"]
        assert checks.tool_input_only(answer, unsourced, inputs) == ["$3,275,000"]

    def test_a_sourced_figure_is_never_reported_as_a_tool_input(self):
        """Only figures already unsourced are classified; the user's own $310,000,
        passed into a tool, is sourced by the user and stays out of the list."""
        answer = "Starting from $310,000."
        inputs = [str({"starting_balance": 310000})]
        unsourced = _unsourced(answer, user=RETIREMENT_TURN_1)
        assert unsourced == []
        assert checks.tool_input_only(answer, unsourced, inputs) == []


class TestExpectedTools:
    def test_every_expected_tool_exists(self):
        """A misspelled name would fail the scenario on every run, forever.

        Any agent's tools count -- the reader's included -- plus `task`, the
        harness's delegation tool, which the pdf scenarios require.
        """
        names = {tool.name for tool in [*ALL_TOOLS, *READER_TOOLS]} | {"task"}
        for scenario, expected in checks.EXPECTED_TOOLS.items():
            assert expected <= names, scenario

    def test_a_tool_that_ran_in_any_agent_counts(self):
        ran = ["project_savings", "test_withdrawal_plan", "simulate_withdrawal_plan"]
        assert checks.missing_tools("retirement", ran) == []

    def test_a_missing_tool_is_reported(self):
        ran = ["project_savings", "simulate_withdrawal_plan"]
        assert checks.missing_tools("retirement", ran) == ["test_withdrawal_plan"]

    def test_a_retirement_answer_without_a_simulation_fails(self):
        """The one-path answer the retirement skill used to give, before it
        reported a probability."""
        ran = ["project_savings", "test_withdrawal_plan"]
        assert checks.missing_tools("retirement", ran) == ["simulate_withdrawal_plan"]

    def test_a_scenario_with_no_expectation_requires_nothing(self):
        assert checks.missing_tools("not-a-scenario", []) == []
