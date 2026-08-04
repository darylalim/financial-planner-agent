"""Tests for the deterministic financial math.

These pin behaviour against externally verifiable values (a standard mortgage
payment, textbook compound growth) rather than against whatever the code
currently happens to return.
"""

from __future__ import annotations

import pytest

from financial_planner.finance import (
    amortize_loan,
    apr_monthly_rate,
    monthly_rate,
    payoff_debts,
    project_portfolio,
    real_rate,
    required_monthly_contribution,
    withdrawal_sustainability,
)


class TestRateConversions:
    def test_geometric_monthly_rate_compounds_back_to_annual(self):
        """Twelve monthly periods must compound to exactly the annual rate."""
        assert (1 + monthly_rate(0.07)) ** 12 == pytest.approx(1.07, abs=1e-12)

    def test_geometric_differs_from_naive_division(self):
        """The shortcut annual/12 is the bug this conversion exists to avoid."""
        assert monthly_rate(0.07) < 0.07 / 12

    def test_apr_uses_lender_convention(self):
        assert apr_monthly_rate(0.06) == pytest.approx(0.005)

    def test_real_rate_is_fisher_not_subtraction(self):
        # Exact Fisher: (1.07 / 1.03) - 1 = 0.038835...
        assert real_rate(0.07, 0.03) == pytest.approx(0.0388349514, abs=1e-9)
        assert real_rate(0.07, 0.03) != pytest.approx(0.04)

    def test_rejects_percentage_instead_of_decimal(self):
        """7 instead of 0.07 is the most likely caller mistake."""
        with pytest.raises(ValueError, match="decimals"):
            monthly_rate(7.0)


class TestProjectPortfolio:
    def test_no_contributions_matches_closed_form_compounding(self):
        result = project_portfolio(
            starting_balance=10_000,
            monthly_contribution=0,
            years=10,
            annual_return=0.07,
            annual_inflation=0.0,
        )
        assert result.final_balance_nominal == pytest.approx(10_000 * 1.07**10, abs=0.05)

    def test_zero_inflation_leaves_real_equal_to_nominal(self):
        result = project_portfolio(
            starting_balance=5_000,
            monthly_contribution=100,
            years=5,
            annual_return=0.06,
            annual_inflation=0.0,
        )
        assert result.final_balance_real == pytest.approx(result.final_balance_nominal)

    def test_inflation_reduces_real_below_nominal(self):
        result = project_portfolio(
            starting_balance=100_000,
            monthly_contribution=500,
            years=30,
            annual_return=0.07,
            annual_inflation=0.025,
        )
        assert result.final_balance_real < result.final_balance_nominal

    def test_components_reconcile_with_final_balance(self):
        """start + contributions + growth must equal the final balance."""
        result = project_portfolio(
            starting_balance=25_000,
            monthly_contribution=750,
            years=20,
            annual_return=0.065,
        )
        reconstructed = 25_000 + result.total_contributed + result.total_growth
        assert reconstructed == pytest.approx(result.final_balance_nominal, abs=0.05)

    def test_contribution_growth_increases_total_contributed(self):
        flat = project_portfolio(
            starting_balance=0, monthly_contribution=500, years=20, annual_return=0.07
        )
        rising = project_portfolio(
            starting_balance=0,
            monthly_contribution=500,
            years=20,
            annual_return=0.07,
            annual_contribution_growth=0.03,
        )
        assert rising.total_contributed > flat.total_contributed
        assert rising.final_balance_nominal > flat.final_balance_nominal

    def test_yearly_series_length_matches_horizon(self):
        result = project_portfolio(
            starting_balance=1_000, monthly_contribution=50, years=7, annual_return=0.05
        )
        assert len(result.yearly_balances) == 7
        assert result.yearly_balances[-1]["year"] == 7

    def test_rejects_absurd_horizon(self):
        with pytest.raises(ValueError, match="years"):
            project_portfolio(
                starting_balance=1, monthly_contribution=1, years=200, annual_return=0.05
            )


class TestRequiredContribution:
    def test_inverts_the_projection(self):
        """Solving for PMT then projecting it forward must hit the target."""
        target, start, years, rate = 1_000_000.0, 50_000.0, 25, 0.07
        pmt = required_monthly_contribution(
            target_amount=target, starting_balance=start, years=years, annual_return=rate
        )
        projected = project_portfolio(
            starting_balance=start,
            monthly_contribution=pmt,
            years=years,
            annual_return=rate,
            annual_inflation=0.0,
        )
        assert projected.final_balance_nominal == pytest.approx(target, rel=1e-4)

    def test_returns_zero_when_already_on_track(self):
        pmt = required_monthly_contribution(
            target_amount=50_000, starting_balance=500_000, years=10, annual_return=0.07
        )
        assert pmt == 0.0


class TestAmortizeLoan:
    def test_matches_standard_mortgage_payment(self):
        """$300k at 6% over 30y is $1,798.65/mo on any lender's calculator."""
        result = amortize_loan(principal=300_000, apr=0.06, years=30)
        assert result.monthly_payment == pytest.approx(1798.65, abs=0.01)
        assert result.months == 360

    def test_total_interest_reconciles(self):
        result = amortize_loan(principal=300_000, apr=0.06, years=30)
        assert result.total_paid - result.total_interest == pytest.approx(300_000, abs=1.0)

    def test_zero_rate_is_simple_division(self):
        result = amortize_loan(principal=12_000, apr=0.0, years=1)
        assert result.monthly_payment == pytest.approx(1_000.0)
        assert result.total_interest == pytest.approx(0.0)


class TestDebtPayoff:
    ONE_CARD = [{"name": "Card", "balance": 1_000.0, "apr": 0.0, "minimum_payment": 100.0}]

    MIXED = [
        {"name": "CreditCard", "balance": 5_000.0, "apr": 0.22, "minimum_payment": 100.0},
        {"name": "CarLoan", "balance": 12_000.0, "apr": 0.06, "minimum_payment": 250.0},
        {"name": "Store", "balance": 800.0, "apr": 0.28, "minimum_payment": 25.0},
    ]

    def test_interest_free_debt_pays_off_in_exact_months(self):
        result = payoff_debts(debts=self.ONE_CARD, monthly_budget=100.0)
        assert result.months_to_debt_free == 10
        assert result.total_interest_paid == pytest.approx(0.0)

    def test_avalanche_never_costs_more_interest_than_snowball(self):
        """This is the mathematical guarantee that justifies recommending it."""
        avalanche = payoff_debts(debts=self.MIXED, monthly_budget=800, strategy="avalanche")
        snowball = payoff_debts(debts=self.MIXED, monthly_budget=800, strategy="snowball")
        assert avalanche.total_interest_paid <= snowball.total_interest_paid

    def test_snowball_clears_smallest_balance_first(self):
        result = payoff_debts(debts=self.MIXED, monthly_budget=800, strategy="snowball")
        assert result.payoff_order[0]["name"] == "Store"

    def test_every_debt_appears_in_payoff_order(self):
        result = payoff_debts(debts=self.MIXED, monthly_budget=800)
        assert {d["name"] for d in result.payoff_order} == {"CreditCard", "CarLoan", "Store"}

    def test_bigger_budget_finishes_sooner(self):
        lean = payoff_debts(debts=self.MIXED, monthly_budget=500)
        rich = payoff_debts(debts=self.MIXED, monthly_budget=1_500)
        assert rich.months_to_debt_free < lean.months_to_debt_free

    def test_budget_below_minimums_is_rejected_not_silently_wrong(self):
        with pytest.raises(ValueError, match="below the sum of minimum"):
            payoff_debts(debts=self.MIXED, monthly_budget=100)

    def test_negative_amortization_is_caught(self):
        """Minimums that never outrun interest must error, not loop forever."""
        trap = [{"name": "Trap", "balance": 50_000.0, "apr": 0.30, "minimum_payment": 10.0}]
        with pytest.raises(ValueError, match="do not amortize"):
            payoff_debts(debts=trap, monthly_budget=10.0)

    def test_missing_field_names_the_missing_field(self):
        with pytest.raises(ValueError, match="apr"):
            payoff_debts(
                debts=[{"name": "X", "balance": 100.0, "minimum_payment": 10.0}],
                monthly_budget=50,
            )


class TestWithdrawalSustainability:
    def test_modest_withdrawal_survives(self):
        result = withdrawal_sustainability(
            portfolio_value=1_000_000,
            annual_withdrawal=35_000,
            years=30,
            annual_return=0.06,
        )
        assert result["survives_horizon"] is True
        assert result["depleted_in_year"] is None

    def test_excessive_withdrawal_depletes(self):
        result = withdrawal_sustainability(
            portfolio_value=500_000,
            annual_withdrawal=100_000,
            years=30,
            annual_return=0.05,
        )
        assert result["survives_horizon"] is False
        assert result["depleted_in_year"] is not None
        assert result["ending_balance"] == 0.0

    def test_reports_initial_withdrawal_rate(self):
        result = withdrawal_sustainability(
            portfolio_value=1_000_000, annual_withdrawal=40_000, years=30, annual_return=0.06
        )
        assert result["initial_withdrawal_rate"] == pytest.approx(0.04)

    def test_always_carries_the_sequence_risk_caveat(self):
        """The caveat is load-bearing: a single path is not a success probability."""
        result = withdrawal_sustainability(
            portfolio_value=1_000_000, annual_withdrawal=40_000, years=30, annual_return=0.06
        )
        assert "sequence-of-returns" in result["caveat"]


class TestGuardsFoundByReview:
    """Edge cases that passed validation and then divided by zero."""

    def test_real_rate_rejects_total_deflation(self):
        """_check_rate permits the closed interval [-1, 1]; -1 divides by zero."""
        with pytest.raises(ValueError, match="cannot be -1.0"):
            real_rate(0.07, -1.0)

    def test_real_rate_still_accepts_ordinary_deflation(self):
        assert real_rate(0.07, -0.01) == pytest.approx((1.07 / 0.99) - 1.0)

    @pytest.mark.parametrize("years", [0.01, 0.03, 0.04])
    def test_amortize_loan_rejects_sub_month_terms(self, years):
        """years is a float, so 0 < years <= 100 does not imply months >= 1."""
        with pytest.raises(ValueError, match="at least one month"):
            amortize_loan(principal=5_000, apr=0.06, years=years)

    def test_amortize_loan_accepts_exactly_one_month(self):
        result = amortize_loan(principal=5_000, apr=0.06, years=1 / 12)
        assert result.months == 1
        assert result.monthly_payment == pytest.approx(5_025.0, abs=0.01)

    def test_amortize_loan_rejects_sub_month_at_zero_apr_too(self):
        """The zero-rate branch divides by the month count as well."""
        with pytest.raises(ValueError, match="at least one month"):
            amortize_loan(principal=5_000, apr=0.0, years=0.02)
