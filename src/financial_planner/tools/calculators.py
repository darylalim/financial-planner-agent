"""Financial calculator tools.

Thin, well-described wrappers over :mod:`financial_planner.finance`. The tool
descriptions carry the *when to call* trigger conditions, not just what each one
does -- that is what actually drives correct tool selection.

Every wrapper converts exceptions into a returned error string rather than
raising. A raised exception ends the agent's turn; a returned message lets it
read the problem, correct the arguments, and retry.
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import tool

from financial_planner import finance


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _err(exc: Exception) -> str:
    return json.dumps({"error": str(exc)})


@tool
def project_savings(
    starting_balance: float,
    monthly_contribution: float,
    years: int,
    annual_return: float,
    annual_inflation: float = 0.025,
    annual_contribution_growth: float = 0.0,
) -> str:
    """Project savings or a retirement portfolio forward over time.

    Call this whenever the user asks what a balance will be worth later, whether
    they are on track to retire, or how a change to their monthly saving affects
    the outcome. Never compute compound growth yourself -- always call this.

    Args:
        starting_balance: Current balance in dollars.
        monthly_contribution: Dollars added each month, in the first year.
        years: Number of years to project (1-100).
        annual_return: Expected annual return as a DECIMAL (0.07 = 7%).
        annual_inflation: Annual inflation as a decimal, for the real (today's
            dollars) figure. Defaults to 0.025.
        annual_contribution_growth: Annual increase in the contribution as a
            decimal, e.g. 0.03 to track expected raises. Defaults to 0.

    Returns:
        JSON with final_balance_nominal, final_balance_real, total_contributed,
        total_growth, and a per-year balance series suitable for charting.
    """
    try:
        return _ok(
            finance.project_portfolio(
                starting_balance=starting_balance,
                monthly_contribution=monthly_contribution,
                years=years,
                annual_return=annual_return,
                annual_inflation=annual_inflation,
                annual_contribution_growth=annual_contribution_growth,
            ).to_dict()
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model for retry
        return _err(exc)


@tool
def required_savings_rate(
    target_amount: float,
    starting_balance: float,
    years: int,
    annual_return: float,
) -> str:
    """Solve for the monthly contribution needed to reach a savings target.

    Call this for goal-driven questions: "how much do I need to save each month
    to have $1M by 65", a house down payment, a college fund. Returns 0 when the
    existing balance already compounds past the target unaided.

    Args:
        target_amount: Goal amount in dollars.
        starting_balance: Amount already saved toward this goal.
        years: Years available to reach it.
        annual_return: Expected annual return as a DECIMAL (0.07 = 7%).

    Returns:
        JSON with required_monthly_contribution in dollars.
    """
    try:
        pmt = finance.required_monthly_contribution(
            target_amount=target_amount,
            starting_balance=starting_balance,
            years=years,
            annual_return=annual_return,
        )
        return _ok({"required_monthly_contribution": pmt})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@tool
def loan_payment(principal: float, apr: float, years: float) -> str:
    """Compute the monthly payment and lifetime interest on a fixed-rate loan.

    Call this for mortgages, auto loans, student loans, or any "what would the
    payment be" question, and when comparing loan terms. Uses the lender
    convention (APR/12) so the result matches the user's statement.

    Args:
        principal: Amount borrowed in dollars.
        apr: Quoted annual percentage rate as a DECIMAL (0.06 = 6%).
        years: Loan term in years.

    Returns:
        JSON with monthly_payment, total_paid, total_interest, months.
    """
    try:
        return _ok(finance.amortize_loan(principal=principal, apr=apr, years=years).to_dict())
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@tool
def plan_debt_payoff(
    debts: list[dict],
    monthly_budget: float,
    strategy: str = "avalanche",
) -> str:
    """Simulate paying off multiple debts and report the payoff timeline.

    Call this whenever the user has more than one debt and asks which to pay
    first, how long until debt-free, or how much interest a strategy saves. Run
    it twice -- once per strategy -- when the user is choosing between them.

    Args:
        debts: List of dicts, each with keys: name (str), balance (float),
            apr (float, DECIMAL e.g. 0.22 for 22%), minimum_payment (float).
        monthly_budget: Total dollars available for debt each month. Must be at
            least the sum of all minimum payments or the call errors.
        strategy: "avalanche" pays the highest APR first and is always cheapest
            in total interest. "snowball" pays the smallest balance first,
            clearing individual debts sooner, which many people stick to better.

    Returns:
        JSON with months_to_debt_free, total_interest_paid, and payoff_order.
    """
    try:
        return _ok(
            finance.payoff_debts(
                debts=debts, monthly_budget=monthly_budget, strategy=strategy
            ).to_dict()
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@tool
def test_withdrawal_plan(
    portfolio_value: float,
    annual_withdrawal: float,
    years: int,
    annual_return: float,
    annual_inflation: float = 0.025,
) -> str:
    """Test whether a portfolio survives a given inflation-adjusted drawdown.

    Call this for retirement-income questions: "can I withdraw $60k a year",
    "is 4% safe for me", "will my savings last 30 years".

    IMPORTANT: this is one deterministic path, not a Monte Carlo simulation. It
    ignores sequence-of-returns risk, the dominant risk in early retirement.
    Always relay the returned caveat -- surviving this test is a necessary but
    not sufficient condition for a plan being safe.

    Args:
        portfolio_value: Portfolio value at retirement, in dollars.
        annual_withdrawal: First-year withdrawal in dollars; grows with inflation.
        years: Length of retirement to test.
        annual_return: Expected annual return as a DECIMAL (0.05 = 5%).
        annual_inflation: Annual inflation as a decimal. Defaults to 0.025.

    Returns:
        JSON with survives_horizon, depleted_in_year, ending_balance,
        initial_withdrawal_rate, a yearly path, and a caveat to relay.
    """
    try:
        return _ok(
            finance.withdrawal_sustainability(
                portfolio_value=portfolio_value,
                annual_withdrawal=annual_withdrawal,
                years=years,
                annual_return=annual_return,
                annual_inflation=annual_inflation,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


CALCULATOR_TOOLS = [
    project_savings,
    required_savings_rate,
    loan_payment,
    plan_debt_payoff,
    test_withdrawal_plan,
]
