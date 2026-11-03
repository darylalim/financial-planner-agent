"""Tests for the deterministic financial math.

These pin behaviour against externally verifiable values (a standard mortgage
payment, textbook compound growth) rather than against whatever the code
currently happens to return.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

import pytest

from financial_planner.finance import (
    _draw_down,
    amortize_loan,
    apr_monthly_rate,
    compare_fees,
    monthly_rate,
    payoff_debts,
    project_portfolio,
    real_rate,
    required_monthly_contribution,
    simulate_withdrawal,
    sum_amounts,
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
        # Whole dollars: an estimate carries no cents (finance.py's conventions).
        assert result.final_balance_nominal == pytest.approx(10_000 * 1.07**10, abs=0.5)

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
        # Rounded up to the dollar, so it reaches the target and overshoots by
        # no more than an extra dollar a month would grow to.
        one_dollar_a_month = project_portfolio(
            starting_balance=0,
            monthly_contribution=1,
            years=years,
            annual_return=rate,
            annual_inflation=0.0,
        ).final_balance_nominal
        assert target <= projected.final_balance_nominal <= target + one_dollar_a_month

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

    def test_reports_the_minimums_and_the_extra_beyond_them(self):
        """The model summed the minimums and subtracted them itself, unsourced."""
        result = payoff_debts(debts=self.MIXED, monthly_budget=800)
        assert result.total_minimum_payments == 375.0
        assert result.extra_beyond_minimums == 425.0

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

    def test_a_monthly_withdrawal_is_the_same_plan_as_twelve_times_it(self):
        """People state retirement spending per month. With an annual-only input
        the model multiplied by twelve itself and quoted the product unsourced."""
        common = {"portfolio_value": 1_500_000, "years": 30, "annual_return": 0.05}
        monthly = withdrawal_sustainability(monthly_withdrawal=6_500, **common)
        annual = withdrawal_sustainability(annual_withdrawal=78_000, **common)
        assert monthly == annual

    def test_both_units_of_the_withdrawal_are_returned(self):
        result = withdrawal_sustainability(
            portfolio_value=1_000_000, monthly_withdrawal=6_500, years=30, annual_return=0.06
        )
        assert result["first_year_annual_withdrawal"] == 78_000
        assert result["first_year_monthly_withdrawal"] == 6_500

    @pytest.mark.parametrize(
        "amounts",
        [{}, {"annual_withdrawal": 78_000, "monthly_withdrawal": 6_500}],
        ids=["neither", "both"],
    )
    def test_exactly_one_withdrawal_unit_is_required(self, amounts):
        with pytest.raises(ValueError, match="exactly one"):
            withdrawal_sustainability(
                portfolio_value=1_000_000, years=30, annual_return=0.06, **amounts
            )

    def test_a_negative_monthly_withdrawal_is_rejected(self):
        with pytest.raises(ValueError, match="monthly_withdrawal"):
            withdrawal_sustainability(
                portfolio_value=1_000_000, monthly_withdrawal=-1, years=30, annual_return=0.06
            )


# A plan near the edge, so its probability is neither 0 nor 1 and can move.
EDGE_PLAN: dict[str, Any] = {
    "portfolio_value": 1_000_000,
    "annual_withdrawal": 35_000,
    "years": 30,
    "annual_return": 0.05,
    "annual_volatility": 0.11,
}


class TestSimulateWithdrawal:
    """The Monte Carlo beside the single path. Its trustworthiness rests on the
    zero-volatility test: with no spread it *is* the deterministic tool, which
    pins the yearly step the two share."""

    @pytest.mark.parametrize(
        "plan",
        [
            {"portfolio_value": 1_000_000, "annual_withdrawal": 40_000, "annual_return": 0.05},
            {"portfolio_value": 500_000, "annual_withdrawal": 100_000, "annual_return": 0.05},
            {"portfolio_value": 1_234_567, "monthly_withdrawal": 6_543, "annual_return": 0.07},
        ],
        ids=["survives", "depletes", "monthly"],
    )
    def test_zero_volatility_reproduces_the_single_path(self, plan):
        single = withdrawal_sustainability(years=30, **plan)
        simulated = simulate_withdrawal(years=30, annual_volatility=0.0, seed=1, paths=50, **plan)
        assert simulated["success_probability"] == (1.0 if single["survives_horizon"] else 0.0)
        assert set(simulated["ending_balance"].values()) == {single["ending_balance"]}
        assert simulated["worst_10_percent_depleted_by_year"] == single["depleted_in_year"]
        assert simulated["worst_25_percent_depleted_by_year"] == single["depleted_in_year"]
        for key in (
            "initial_withdrawal_rate",
            "first_year_annual_withdrawal",
            "first_year_monthly_withdrawal",
        ):
            assert simulated[key] == single[key]

    def test_the_same_seed_gives_the_same_payload(self):
        assert simulate_withdrawal(**EDGE_PLAN, seed=7) == simulate_withdrawal(**EDGE_PLAN, seed=7)

    def test_another_seed_moves_the_probability_only_by_sampling_error(self):
        """At p near 0.8 over 10,000 paths the standard error is 0.4 points."""
        a = simulate_withdrawal(**EDGE_PLAN, seed=1)["success_probability"]
        b = simulate_withdrawal(**EDGE_PLAN, seed=2)["success_probability"]
        assert 0.05 < a < 0.95
        assert abs(a - b) <= 0.02

    def test_the_median_path_compounds_at_the_assumed_return(self):
        """The median growth is the user's rate, so the simulation's
        middle agrees with the single path they can follow year by year."""
        result = simulate_withdrawal(
            portfolio_value=1_000_000,
            annual_withdrawal=0,
            years=30,
            annual_return=0.05,
            annual_volatility=0.11,
            annual_inflation=0.0,
            seed=3,
        )
        compounded = (result["ending_balance"]["median"] / 1_000_000) ** (1 / 30) - 1
        assert compounded == pytest.approx(0.05, abs=0.001)

    def test_the_volatility_is_the_spread_of_yearly_returns(self):
        """The input is a spread of returns, as measured; the log-space sigma
        the draws use is derived from it, not equal to it."""
        result = simulate_withdrawal(
            portfolio_value=1_000_000,
            annual_withdrawal=0,
            years=1,
            annual_return=0.05,
            annual_volatility=0.11,
            annual_inflation=0.0,
            seed=4,
        )
        # Three points of the distribution, recovered from what was returned:
        # under a lognormal, p90/p50 = p50/p10 = exp(1.2816 * sigma).
        balances = result["ending_balance"]
        sigma = (balances["90th_percentile"] / balances["10th_percentile"]) ** 0.5
        spread = statistics.NormalDist().inv_cdf(0.9)
        log_sigma = math.log(sigma) / spread
        median = balances["median"] / 1_000_000
        stdev = median * math.sqrt(math.exp(log_sigma**2) * (math.exp(log_sigma**2) - 1))
        assert stdev == pytest.approx(0.11, abs=0.004)

    def test_bad_years_first_do_more_damage_than_bad_years_last(self):
        """Sequence-of-returns risk, the reason this tool exists: the same
        returns in a different order leave a drawing-down portfolio worse off."""
        returns = [-0.25, -0.15, -0.10] + [0.08] * 24 + [0.15, 0.20, 0.25]
        common = {"portfolio_value": 1_000_000, "first_year_withdrawal": 50_000}
        early, early_depleted = _draw_down(
            **common, annual_inflation=0.025, growth_factors=[1 + r for r in returns]
        )
        late, late_depleted = _draw_down(
            **common, annual_inflation=0.025, growth_factors=[1 + r for r in reversed(returns)]
        )
        assert late_depleted is None
        assert early_depleted is not None or early[-1] < late[-1]

    def test_more_volatility_lowers_success_on_the_same_median(self):
        """Only for a plan whose median path survives, as a retirement plan
        worth testing does. When the median path fails, spread *raises*
        success, since only a lucky run of years gets through."""
        assert withdrawal_sustainability(
            **{k: v for k, v in EDGE_PLAN.items() if k != "annual_volatility"}
        )["survives_horizon"]
        calm = simulate_withdrawal(**{**EDGE_PLAN, "annual_volatility": 0.05}, seed=5)
        wild = simulate_withdrawal(**{**EDGE_PLAN, "annual_volatility": 0.18}, seed=5)
        assert wild["success_probability"] < calm["success_probability"]

    def test_probabilities_are_two_decimals_and_add_to_one(self):
        """A whole percent is honest at 10,000 paths. Both are
        returned so "a 19% chance of running out" is never 1 - 0.81."""
        result = simulate_withdrawal(**EDGE_PLAN, seed=6)
        for key in ("success_probability", "failure_probability"):
            assert result[key] == round(result[key], 2)
        assert result["success_probability"] + result["failure_probability"] == pytest.approx(1)

    def test_balances_are_whole_dollars_nominal_and_real(self):
        result = simulate_withdrawal(**EDGE_PLAN, seed=6)
        for key in ("ending_balance", "ending_balance_real"):
            assert set(result[key]) == {"10th_percentile", "median", "90th_percentile"}
            assert all(isinstance(v, int) for v in result[key].values())
        assert result["ending_balance_real"]["median"] < result["ending_balance"]["median"]

    def test_depletion_is_null_when_too_few_paths_fail(self):
        result = simulate_withdrawal(**{**EDGE_PLAN, "annual_withdrawal": 25_000}, seed=6)
        assert result["failure_probability"] < 0.10
        assert result["worst_10_percent_depleted_by_year"] is None
        assert result["worst_25_percent_depleted_by_year"] is None

    def test_the_worst_tenth_runs_out_before_the_worst_quarter(self):
        result = simulate_withdrawal(**{**EDGE_PLAN, "annual_withdrawal": 70_000}, seed=6)
        assert result["failure_probability"] > 0.25
        ten = result["worst_10_percent_depleted_by_year"]
        quarter = result["worst_25_percent_depleted_by_year"]
        assert ten is not None and quarter is not None
        assert ten <= quarter <= 30

    def test_ages_are_returned_so_the_model_never_adds_them(self):
        """The checker sees only $ and %, so an age the model worked out
        (year + age) would pass it unchecked. Returning the age is the guard."""
        plan = {**EDGE_PLAN, "annual_withdrawal": 70_000}
        result = simulate_withdrawal(**plan, retirement_age=65, seed=6)
        assert result["horizon_end_age"] == 95
        assert result["worst_10_percent_depleted_by_age"] == (
            65 + result["worst_10_percent_depleted_by_year"]
        )
        assert result["worst_25_percent_depleted_by_age"] == (
            65 + result["worst_25_percent_depleted_by_year"]
        )

    def test_without_a_retirement_age_there_are_no_ages(self):
        result = simulate_withdrawal(**EDGE_PLAN, seed=6)
        assert not [key for key in result if "age" in key.split("_")]

    def test_the_inputs_are_echoed_as_assumptions_to_state(self):
        result = simulate_withdrawal(**EDGE_PLAN, seed=6)
        assert result["paths"] == 10_000
        assert result["seed"] == 6
        assert result["annual_return"] == 0.05
        assert result["annual_volatility"] == 0.11
        assert result["annual_inflation"] == 0.025
        assert result["caveat"]

    def test_a_monthly_withdrawal_is_the_same_plan_as_twelve_times_it(self):
        plan = {k: v for k, v in EDGE_PLAN.items() if k != "annual_withdrawal"}
        monthly = simulate_withdrawal(**plan, monthly_withdrawal=5_000, seed=8, paths=500)
        annual = simulate_withdrawal(**plan, annual_withdrawal=60_000, seed=8, paths=500)
        assert monthly == annual

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"annual_volatility": 18}, "annual_volatility"),
            ({"annual_volatility": -0.1}, "annual_volatility"),
            ({"annual_return": -1.0}, "annual_return"),
            ({"monthly_withdrawal": 5_000}, "exactly one"),
            ({"years": 0}, "years"),
            ({"retirement_age": -1}, "retirement_age"),
        ],
        ids=["percent-not-decimal", "negative-volatility", "total-loss", "both", "years", "age"],
    )
    def test_bad_inputs_are_rejected(self, change, message):
        with pytest.raises(ValueError, match=message):
            simulate_withdrawal(**{**EDGE_PLAN, **change}, seed=1, paths=10)


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

    def test_amortize_totals_are_built_from_the_reported_payment(self):
        """The three reported numbers must reconcile exactly, not to within $1.

        Totalling the *unrounded* payment reported $1,798.65/mo alongside a
        total of $647,514.57, while 1798.65 * 360 is $647,514.00 -- a 57c gap a
        user reproduces on any calculator.
        """
        result = amortize_loan(principal=300_000, apr=0.06, years=30)
        assert result.monthly_payment == pytest.approx(1798.65, abs=0.01)
        assert result.total_paid == pytest.approx(result.monthly_payment * result.months, abs=0.005)
        assert result.total_interest == pytest.approx(result.total_paid - 300_000, abs=0.005)

    @pytest.mark.parametrize(
        ("principal", "years"),
        [(10_000, 1), (5_000, 2), (7_500, 3), (1_000, 7), (2_000, 0.25)],
    )
    def test_an_interest_free_loan_never_reports_negative_interest(self, principal, years):
        """The rounded payment does not divide the principal, and at 0% the
        residue is all there is: 12 * $833.33 is $9,999.96, reported as -4c of
        interest and $9,999.96 repaid on $10,000 borrowed. `loan_payment` hands
        that to the model as "lifetime interest", so a 0% promo balance or a
        family loan came back as the lender paying the borrower.

        The sibling test above pins the reconciliation invariant at apr=0.06,
        where the residue hides inside six figures of interest -- which is why
        it never saw this.
        """
        result = amortize_loan(principal=principal, apr=0.0, years=years)
        assert result.total_interest >= 0.0
        assert result.total_paid >= principal

    def test_an_interest_free_loan_repays_exactly_what_was_borrowed(self):
        """The headline case, pinned exactly rather than as an inequality."""
        result = amortize_loan(principal=10_000, apr=0.0, years=1)
        assert result.total_paid == 10_000.0
        assert result.total_interest == 0.0

    def test_amortize_rounds_a_fractional_term_to_whole_months(self):
        """The docstring promises nearest-month rounding; pin it."""
        assert amortize_loan(principal=10_000, apr=0.06, years=30.5).months == 366
        assert amortize_loan(principal=10_000, apr=0.06, years=1.01).months == 12

    def test_payoff_cascades_leftover_budget_within_the_same_month(self):
        """A target that clears mid-month must hand its remainder straight on.

        Applying the leftover to exactly one debt stranded $850 of a $1,000
        budget for a month: snowball cleared A ($100) in month 1 and then sat
        idle while B accrued at 24%.
        """
        debts = [
            {"name": "A", "balance": 100.0, "apr": 0.0, "minimum_payment": 10.0},
            {"name": "B", "balance": 10_000.0, "apr": 0.24, "minimum_payment": 50.0},
        ]
        result = payoff_debts(debts=debts, monthly_budget=1_000.0, strategy="snowball")
        # Month 1: interest takes B to 10,200; minimums (10 + 50) and then the
        # whole 940 leftover -- 90 clearing A, 850 cascading to B -- leave B at
        # 10,200 - 50 - 850 = 9,300. Stranding the 850 would leave it at 10,150.
        assert {k: result.payoff_order[0][k] for k in ("name", "paid_off_month")} == {
            "name": "A",
            "paid_off_month": 1,
        }
        assert result.months_to_debt_free == 12

    def test_payoff_cascade_still_respects_strategy_ordering(self):
        """Re-selecting each pass must not let snowball beat avalanche."""
        avalanche = payoff_debts(
            debts=TestDebtPayoff.MIXED, monthly_budget=2_000, strategy="avalanche"
        )
        snowball = payoff_debts(
            debts=TestDebtPayoff.MIXED, monthly_budget=2_000, strategy="snowball"
        )
        assert avalanche.total_interest_paid <= snowball.total_interest_paid
        assert snowball.payoff_order[0]["name"] == "Store"

    def test_payoff_absorbs_a_budget_far_larger_than_the_total_debt(self):
        """The cascade must terminate once every balance is gone, not spin."""
        result = payoff_debts(debts=TestDebtPayoff.MIXED, monthly_budget=1_000_000)
        assert result.months_to_debt_free == 1
        assert len(result.payoff_order) == 3


class TestCompareFees:
    BASE = {
        "starting_balance": 80_000.0,
        "monthly_contribution": 500.0,
        "years": 25,
        "gross_annual_return": 0.07,
    }

    def test_each_balance_is_a_projection_at_the_net_return(self):
        """The fee comes off the annual return, then the geometric conversion."""
        result = compare_fees(
            **self.BASE, current_expense_ratio=0.0059, alternative_expense_ratio=0.0003
        )
        for side, ratio in (("current", 0.0059), ("alternative", 0.0003)):
            projected = project_portfolio(
                starting_balance=80_000.0,
                monthly_contribution=500.0,
                years=25,
                annual_return=0.07 - ratio,
                annual_inflation=0.0,
            )
            assert result[side]["ending_balance"] == pytest.approx(
                projected.final_balance_nominal, abs=0.01
            )

    def test_a_zero_fee_pays_no_fees(self):
        result = compare_fees(**self.BASE, current_expense_ratio=0.0, alternative_expense_ratio=0.0)
        assert result["current"]["total_fees_paid"] == 0
        assert result["alternative_ending_balance_higher_by"] == 0

    def test_fees_reconcile_with_the_gross_path(self):
        """Gross balance minus net balance is the fees plus the growth they lost."""
        result = compare_fees(
            **self.BASE, current_expense_ratio=0.01, alternative_expense_ratio=0.0
        )
        gap = result["alternative_ending_balance_higher_by"]
        fees = result["current"]["total_fees_paid"]
        assert 0 < fees < gap

    def test_the_gap_splits_into_fees_and_lost_growth(self):
        """Answers explain the gap this way; the model subtracted to do it."""
        result = compare_fees(
            **self.BASE, current_expense_ratio=0.0059, alternative_expense_ratio=0.0003
        )
        parts = result["alternative_fees_lower_by"] + result["growth_lost_on_the_fee_difference"]
        assert parts == pytest.approx(result["alternative_ending_balance_higher_by"], abs=0.01)
        assert result["growth_lost_on_the_fee_difference"] > 0

    def test_a_dearer_alternative_is_a_negative_gap(self):
        result = compare_fees(
            **self.BASE, current_expense_ratio=0.0003, alternative_expense_ratio=0.0059
        )
        assert result["alternative_ending_balance_higher_by"] < 0
        assert result["alternative_fees_lower_by"] < 0

    @pytest.mark.parametrize("ratio", [0.59, -0.001])
    def test_rejects_a_ratio_on_the_wrong_scale(self, ratio):
        with pytest.raises(ValueError, match="decimals"):
            compare_fees(**self.BASE, current_expense_ratio=ratio, alternative_expense_ratio=0.0)


class TestSumAmounts:
    def test_totals_without_float_drift(self):
        assert sum_amounts([("a", 0.1), ("b", 0.2)])["total"] == 0.3

    def test_needs_two_amounts(self):
        with pytest.raises(ValueError, match="at least two"):
            sum_amounts([("a", 1.0)])


class TestDebtRollover:
    """The payment a cleared debt frees is what an answer tells the user to
    move to the next one; the model added minimum and extra to state it."""

    DEBTS = [
        {"name": "card", "balance": 8_400, "apr": 0.229, "minimum_payment": 210},
        {"name": "car", "balance": 14_200, "apr": 0.064, "minimum_payment": 340},
        {"name": "student", "balance": 21_000, "apr": 0.045, "minimum_payment": 230},
    ]

    def test_each_debt_reports_the_payment_that_rolls_over(self):
        """The debt scenario's figures: $210 + $420 extra, then + $340."""
        order = payoff_debts(debts=self.DEBTS, monthly_budget=1_200).payoff_order
        assert [d["monthly_payment_before_payoff"] for d in order] == [630, 970, 1_200]

    def test_a_debt_cleared_by_its_minimum_reports_the_minimum(self):
        """Never the target, so a budget-minus-minimums formula would be wrong."""
        debts = [
            {"name": "big", "balance": 10_000, "apr": 0.25, "minimum_payment": 100},
            {"name": "tiny", "balance": 150, "apr": 0.01, "minimum_payment": 50},
        ]
        order = payoff_debts(debts=debts, monthly_budget=600).payoff_order
        tiny = next(d for d in order if d["name"] == "tiny")
        assert tiny["monthly_payment_before_payoff"] == 50

    def test_a_debt_cleared_in_month_one_reports_what_it_was_paid(self):
        debts = [{"name": "small", "balance": 100, "apr": 0.0, "minimum_payment": 25}]
        order = payoff_debts(debts=debts, monthly_budget=500).payoff_order
        assert order[0]["monthly_payment_before_payoff"] == 100
