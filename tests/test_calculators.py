"""Tests for the calculator tool wrappers.

`test_finance.py` already pins the math. What is untested there is the layer the
model actually touches: argument coercion, the JSON envelope, and the promise
that a bad argument comes back as a readable message instead of ending the turn.

The envelope tests are the important ones. `streaming._is_error_result` decides
whether the UI reports a tool call as failed, and it does so by string-matching
the serialized payload -- so a change to `_err`'s shape here would silently stop
failures being surfaced. Those tests assert against the real detector rather
than a copy of what its input is assumed to look like.
"""

from __future__ import annotations

import json

import pytest

from financial_planner.streaming import _is_error_result
from financial_planner.tools.calculators import (
    CALCULATOR_TOOLS,
    loan_payment,
    plan_debt_payoff,
    project_savings,
    required_savings_rate,
    test_withdrawal_plan,
)

DEBTS = [
    {"name": "Card A", "balance": 6000.0, "apr": 0.22, "minimum_payment": 120.0},
    {"name": "Card B", "balance": 2000.0, "apr": 0.09, "minimum_payment": 50.0},
]

# One valid call per tool, for the checks that should hold across all of them.
VALID_CALLS = [
    (
        project_savings,
        {
            "starting_balance": 50_000,
            "monthly_contribution": 1_500,
            "years": 20,
            "annual_return": 0.07,
        },
    ),
    (
        required_savings_rate,
        {
            "target_amount": 1_000_000,
            "starting_balance": 50_000,
            "years": 30,
            "annual_return": 0.07,
        },
    ),
    (loan_payment, {"principal": 300_000, "apr": 0.06, "years": 30}),
    (plan_debt_payoff, {"debts": DEBTS, "monthly_budget": 500.0}),
    (
        test_withdrawal_plan,
        {
            "portfolio_value": 1_000_000,
            "annual_withdrawal": 40_000,
            "years": 30,
            "annual_return": 0.05,
        },
    ),
]

# Each tool paired with arguments its underlying finance function rejects.
INVALID_CALLS = [
    # 7 rather than 0.07 -- the unit error the rate guards exist to catch.
    (
        project_savings,
        {"starting_balance": 1_000, "monthly_contribution": 100, "years": 20, "annual_return": 7.0},
    ),
    (
        required_savings_rate,
        {"target_amount": 1_000, "starting_balance": 0, "years": 0, "annual_return": 0.07},
    ),
    (loan_payment, {"principal": -1.0, "apr": 0.06, "years": 30}),
    # Budget below the sum of the minimum payments: the debts never amortize.
    (plan_debt_payoff, {"debts": DEBTS, "monthly_budget": 10.0}),
    (
        test_withdrawal_plan,
        {"portfolio_value": 1_000, "annual_withdrawal": 40, "years": 0, "annual_return": 0.05},
    ),
]


def _ids(calls):
    return [tool.name for tool, _ in calls]


def call(tool, **kwargs) -> dict:
    """Invoke a tool the way the agent does and parse its reply."""
    raw = tool.invoke(kwargs)
    assert isinstance(raw, str), "tools must return a string; the model never sees a dict"
    return json.loads(raw)


class TestEveryToolIsWellFormed:
    @pytest.mark.parametrize(("tool", "kwargs"), VALID_CALLS, ids=_ids(VALID_CALLS))
    def test_a_valid_call_returns_a_success_payload(self, tool, kwargs):
        assert "error" not in call(tool, **kwargs)

    @pytest.mark.parametrize(("tool", "kwargs"), INVALID_CALLS, ids=_ids(INVALID_CALLS))
    def test_a_rejected_call_returns_an_error_instead_of_raising(self, tool, kwargs):
        """A raise ends the agent's turn; a returned message lets it retry."""
        assert "error" in call(tool, **kwargs)

    @pytest.mark.parametrize(("tool", "kwargs"), INVALID_CALLS, ids=_ids(INVALID_CALLS))
    def test_the_error_envelope_is_what_the_ui_detects(self, tool, kwargs):
        """Pins `_err`'s serialized shape against the real consumer.

        `_is_error_result` string-matches the payload's opening characters, so
        adding a key before "error" -- or switching to indented JSON -- would
        make every tool failure render in the UI as a success.
        """
        assert _is_error_result(tool.invoke(kwargs))

    @pytest.mark.parametrize(("tool", "kwargs"), VALID_CALLS, ids=_ids(VALID_CALLS))
    def test_a_success_payload_is_not_mistaken_for_an_error(self, tool, kwargs):
        assert not _is_error_result(tool.invoke(kwargs))

    def test_the_exported_list_matches_the_tools_under_test(self):
        assert {t.name for t in CALCULATOR_TOOLS} == {t.name for t, _ in VALID_CALLS}


class TestProjectSavings:
    def test_reports_the_documented_keys(self):
        result = call(
            project_savings,
            starting_balance=50_000,
            monthly_contribution=1_500,
            years=20,
            annual_return=0.07,
        )
        assert {
            "final_balance_nominal",
            "final_balance_real",
            "total_contributed",
            "total_growth",
        } <= set(result)

    def test_inflation_defaults_are_applied_by_the_wrapper(self):
        """The default lives on the tool signature, not in finance.py."""
        explicit = call(
            project_savings,
            starting_balance=10_000,
            monthly_contribution=500,
            years=10,
            annual_return=0.07,
            annual_inflation=0.025,
        )
        implied = call(
            project_savings,
            starting_balance=10_000,
            monthly_contribution=500,
            years=10,
            annual_return=0.07,
        )
        assert explicit["final_balance_real"] == implied["final_balance_real"]

    def test_real_is_below_nominal_under_positive_inflation(self):
        result = call(
            project_savings,
            starting_balance=10_000,
            monthly_contribution=500,
            years=10,
            annual_return=0.07,
            annual_inflation=0.025,
        )
        assert result["final_balance_real"] < result["final_balance_nominal"]

    def test_the_error_message_survives_serialization(self):
        """The model reads this string; it has to say what to change."""
        result = call(
            project_savings,
            starting_balance=1_000,
            monthly_contribution=100,
            years=20,
            annual_return=7.0,
        )
        assert "7.0" in result["error"] or "decimal" in result["error"].lower()


class TestLoanPayment:
    def test_matches_the_lender_convention(self):
        """$300k at 6% over 30y is $1,798.65 on any mortgage calculator.

        Pinned to an externally verifiable figure so a switch from APR/12 to a
        geometric monthly rate -- which is the correct choice for investments
        and the wrong one here -- fails loudly.
        """
        result = call(loan_payment, principal=300_000, apr=0.06, years=30)
        assert result["monthly_payment"] == pytest.approx(1798.65, abs=0.01)

    def test_zero_interest_repays_exactly_the_principal(self):
        result = call(loan_payment, principal=12_000, apr=0.0, years=1)
        assert result["total_interest"] == pytest.approx(0.0, abs=0.01)
        assert result["monthly_payment"] == pytest.approx(1000.0, abs=0.01)


class TestPlanDebtPayoff:
    def test_strategy_defaults_to_avalanche(self):
        default = call(plan_debt_payoff, debts=DEBTS, monthly_budget=500.0)
        avalanche = call(plan_debt_payoff, debts=DEBTS, monthly_budget=500.0, strategy="avalanche")
        assert default["total_interest_paid"] == avalanche["total_interest_paid"]

    def test_avalanche_never_costs_more_interest_than_snowball(self):
        avalanche = call(plan_debt_payoff, debts=DEBTS, monthly_budget=500.0, strategy="avalanche")
        snowball = call(plan_debt_payoff, debts=DEBTS, monthly_budget=500.0, strategy="snowball")
        assert avalanche["total_interest_paid"] <= snowball["total_interest_paid"]

    def test_an_unknown_strategy_is_reported_not_silently_defaulted(self):
        result = call(
            plan_debt_payoff, debts=DEBTS, monthly_budget=500.0, strategy="highest-balance"
        )
        assert "error" in result

    def test_a_malformed_debt_entry_is_reported(self):
        """The model builds these dicts by hand from conversation."""
        result = call(
            plan_debt_payoff,
            debts=[{"name": "Card", "balance": 1_000.0}],
            monthly_budget=200.0,
        )
        assert "error" in result


class TestWithdrawalPlan:
    def test_the_monte_carlo_caveat_is_always_returned(self):
        """The tool docstring promises the caveat is relayed; it must be there."""
        result = call(
            test_withdrawal_plan,
            portfolio_value=1_000_000,
            annual_withdrawal=40_000,
            years=30,
            annual_return=0.05,
        )
        assert result["caveat"]

    def test_an_unsustainable_plan_reports_depletion(self):
        result = call(
            test_withdrawal_plan,
            portfolio_value=200_000,
            annual_withdrawal=80_000,
            years=30,
            annual_return=0.05,
        )
        assert result["survives_horizon"] is False
        assert result["depleted_in_year"] is not None

    def test_a_sustainable_plan_survives_the_horizon(self):
        result = call(
            test_withdrawal_plan,
            portfolio_value=2_000_000,
            annual_withdrawal=40_000,
            years=30,
            annual_return=0.05,
        )
        assert result["survives_horizon"] is True
