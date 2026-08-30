"""Deterministic financial mathematics.

This module is intentionally free of any LangChain / agent imports. Every number
the agent reports to a user must originate here, never from the model's own
arithmetic: a language model can produce a *plausible* compound-interest figure
that is wrong by tens of thousands of dollars, and the user has no way to tell.

Conventions used throughout:

* Rates are passed as decimals (``0.07`` means 7%), never percentages.
* Two different -- both correct -- annual-to-monthly conversions are used, and
  which one applies depends on what the rate describes:

  - **Investment returns** use the geometric conversion
    ``(1 + annual) ** (1/12) - 1``. An "expected 7% annual return" means 7%
    *effective*, so twelve monthly periods must compound to exactly that.
    Dividing by 12 yields 7.23% effective and overstates a 30-year projection.
  - **Debt (APR)** uses ``apr / 12``. US lenders quote APR as a *nominal* rate
    compounded monthly, so this is the lender's own convention. Applying the
    geometric form to a $300k 6% mortgage gives ~$1,770/mo against the bank's
    actual $1,798.65 -- the agent would contradict the user's statement.

  Mixing these up is the most likely source of a subtly wrong number here.
* Contributions are treated as end-of-period (ordinary annuity).
* "Real" values are inflation-adjusted into today's purchasing power.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

__all__ = [
    "AmortizationResult",
    "DebtPayoffResult",
    "ProjectionResult",
    "amortize_loan",
    "apr_monthly_rate",
    "monthly_rate",
    "payoff_debts",
    "project_portfolio",
    "real_rate",
    "required_monthly_contribution",
    "withdrawal_sustainability",
]

# Guard rails. These are deliberately generous -- they exist to catch unit
# mistakes (7 instead of 0.07) and runaway loops, not to express opinions.
_MAX_YEARS = 100
_MAX_MONTHS = _MAX_YEARS * 12
_MAX_RATE = 1.0  # 100% annual


def _check_rate(name: str, value: float) -> None:
    if not -_MAX_RATE <= value <= _MAX_RATE:
        raise ValueError(
            f"{name}={value} is outside the supported range "
            f"[-1.0, 1.0]. Rates are decimals: use 0.07 for 7%, not 7."
        )


def _check_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def monthly_rate(annual_rate: float) -> float:
    """Convert an annual rate to its equivalent monthly compounding rate.

    Uses the geometric conversion so that twelve monthly periods compound to
    exactly ``annual_rate``. ``annual_rate / 12`` is the common shortcut and is
    wrong; at 7% it produces 7.23% effective, which is a meaningful error once
    compounded over decades.
    """
    _check_rate("annual_rate", annual_rate)
    return (1.0 + annual_rate) ** (1.0 / 12.0) - 1.0


def apr_monthly_rate(apr: float) -> float:
    """Convert a quoted APR to a monthly rate the way lenders do: ``apr / 12``.

    Use this for anything the user owes -- mortgages, auto loans, student loans,
    credit cards. US lenders quote APR as a nominal annual rate compounded
    monthly, so dividing by 12 reproduces the payment on their statement.

    Do **not** use this for investment returns; see :func:`monthly_rate`.
    """
    _check_rate("apr", apr)
    return apr / 12.0


def real_rate(nominal_rate: float, inflation_rate: float) -> float:
    """Fisher equation: the inflation-adjusted (real) return.

    ``nominal - inflation`` is the usual approximation and drifts once either
    rate is non-trivial, so the exact form is used.
    """
    _check_rate("nominal_rate", nominal_rate)
    _check_rate("inflation_rate", inflation_rate)
    # _check_rate permits the closed interval [-1, 1], and -1 would divide by
    # zero here. Total price collapse is not a scenario this models anyway.
    if inflation_rate == -1.0:
        raise ValueError("inflation_rate cannot be -1.0 (total deflation); use decimals like 0.025")
    return (1.0 + nominal_rate) / (1.0 + inflation_rate) - 1.0


@dataclass
class ProjectionResult:
    """Outcome of a portfolio projection."""

    final_balance_nominal: float
    final_balance_real: float
    total_contributed: float
    total_growth: float
    years: int
    yearly_balances: list[dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def project_portfolio(
    *,
    starting_balance: float,
    monthly_contribution: float,
    years: int,
    annual_return: float,
    annual_inflation: float = 0.025,
    annual_contribution_growth: float = 0.0,
) -> ProjectionResult:
    """Project a portfolio forward month by month.

    A closed-form annuity formula cannot express contributions that grow each
    year, so this simulates explicitly. The loop is bounded by ``_MAX_MONTHS``.

    Args:
        starting_balance: Balance today.
        monthly_contribution: Amount added at the end of each month, in year 1.
        years: Projection horizon.
        annual_return: Expected nominal annual return, as a decimal.
        annual_inflation: Used to express the result in today's dollars.
        annual_contribution_growth: Yearly raise applied to the contribution
            (e.g. ``0.03`` to track a 3% salary increase).

    Returns:
        A :class:`ProjectionResult` with both nominal and real final balances.
    """
    _check_non_negative("starting_balance", starting_balance)
    _check_non_negative("monthly_contribution", monthly_contribution)
    _check_rate("annual_return", annual_return)
    _check_rate("annual_inflation", annual_inflation)
    _check_rate("annual_contribution_growth", annual_contribution_growth)
    if not 0 < years <= _MAX_YEARS:
        raise ValueError(f"years must be in (0, {_MAX_YEARS}], got {years}")

    r_month = monthly_rate(annual_return)
    balance = float(starting_balance)
    contribution = float(monthly_contribution)
    total_contributed = 0.0
    yearly: list[dict[str, float]] = []

    for year in range(1, years + 1):
        for _ in range(12):
            balance = balance * (1.0 + r_month) + contribution
            total_contributed += contribution
        # Raise applies at the start of each subsequent year.
        contribution *= 1.0 + annual_contribution_growth
        deflator = (1.0 + annual_inflation) ** year
        yearly.append(
            {
                "year": year,
                "balance_nominal": round(balance, 2),
                "balance_real": round(balance / deflator, 2),
                "contributed_to_date": round(total_contributed, 2),
            }
        )

    deflator = (1.0 + annual_inflation) ** years
    return ProjectionResult(
        final_balance_nominal=round(balance, 2),
        final_balance_real=round(balance / deflator, 2),
        total_contributed=round(total_contributed, 2),
        total_growth=round(balance - starting_balance - total_contributed, 2),
        years=years,
        yearly_balances=yearly,
    )


def required_monthly_contribution(
    *,
    target_amount: float,
    starting_balance: float,
    years: int,
    annual_return: float,
) -> float:
    """Solve for the monthly contribution needed to reach ``target_amount``.

    Inverts the ordinary-annuity future-value formula::

        FV = PV(1+r)^n + PMT * [((1+r)^n - 1) / r]

    Returns ``0.0`` when the starting balance already compounds past the target
    on its own.
    """
    _check_non_negative("target_amount", target_amount)
    _check_non_negative("starting_balance", starting_balance)
    _check_rate("annual_return", annual_return)
    if not 0 < years <= _MAX_YEARS:
        raise ValueError(f"years must be in (0, {_MAX_YEARS}], got {years}")

    r = monthly_rate(annual_return)
    n = years * 12
    grown = starting_balance * (1.0 + r) ** n
    shortfall = target_amount - grown
    if shortfall <= 0:
        return 0.0
    if r == 0:
        return round(shortfall / n, 2)
    annuity_factor = ((1.0 + r) ** n - 1.0) / r
    return round(shortfall / annuity_factor, 2)


@dataclass
class AmortizationResult:
    monthly_payment: float
    total_paid: float
    total_interest: float
    months: int

    def to_dict(self) -> dict:
        return asdict(self)


def amortize_loan(*, principal: float, apr: float, years: float) -> AmortizationResult:
    """Standard fixed-rate loan amortization (mortgage, auto, student).

    Uses the lender convention ``apr / 12`` so the monthly payment matches the
    figure on the user's statement.

    A fractional ``years`` is rounded to the nearest whole number of months, so
    a 30.5-year term is amortized over 366 months.
    """
    _check_non_negative("principal", principal)
    _check_rate("apr", apr)
    if not 0 < years <= _MAX_YEARS:
        raise ValueError(f"years must be in (0, {_MAX_YEARS}], got {years}")

    n = int(round(years * 12))
    if n < 1:
        # ``years`` is a float here, so sub-month terms reach this point having
        # passed the 0 < years check and would divide by a zero month count.
        raise ValueError(f"years must cover at least one month (>= 1/12), got {years}")
    r = apr_monthly_rate(apr)
    if r == 0:
        payment = principal / n
    else:
        payment = principal * (r * (1.0 + r) ** n) / ((1.0 + r) ** n - 1.0)
    # Round the payment *before* deriving the totals. The borrower writes a
    # cheque for the rounded figure every month, so that is the number the
    # totals have to be built from; totalling the unrounded payment leaves the
    # three reported numbers irreconcilable (on $300k/6%/30y it reports
    # $1,798.65 alongside a total of $647,514.57, while 1798.65 * 360 is
    # $647,514.00) and a user with a calculator can see the discrepancy.
    payment = round(payment, 2)
    total = payment * n
    # ...but a rounded payment does not divide the principal exactly, and the
    # residue is signed. Where the interest is smaller than that residue the
    # total lands *below* the principal: an interest-free $10,000 over 12
    # months totals 12 * $833.33 = $9,999.96 and so reports -4c of interest --
    # the lender paying the borrower to hold their money, which `loan_payment`
    # hands to the model as "lifetime interest". In a real schedule the final
    # payment is the one that settles the balance, so it absorbs the residue;
    # the total therefore never falls below what was borrowed.
    total = float(max(total, principal))
    return AmortizationResult(
        monthly_payment=payment,
        total_paid=round(total, 2),
        total_interest=round(total - principal, 2),
        months=n,
    )


@dataclass
class _Debt:
    """Mutable per-debt state for the payoff simulation."""

    name: str
    balance: float
    rate: float  # already converted to monthly
    minimum: float


@dataclass
class DebtPayoffResult:
    strategy: str
    months_to_debt_free: int
    total_interest_paid: float
    payoff_order: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def payoff_debts(
    *,
    debts: list[dict],
    monthly_budget: float,
    strategy: str = "avalanche",
) -> DebtPayoffResult:
    """Simulate paying down multiple debts with a fixed monthly budget.

    Both strategies pay every debt's minimum, then direct all remaining budget
    at a single target debt; when that debt clears, its payment rolls into the
    next target (the "snowball" effect, which both strategies share).

    Args:
        debts: Dicts with ``name``, ``balance``, ``apr``, ``minimum_payment``.
        monthly_budget: Total available for debt each month. Must cover the sum
            of all minimum payments.
        strategy: ``"avalanche"`` targets the highest interest rate first and is
            always the cheaper option mathematically. ``"snowball"`` targets the
            smallest balance first, clearing individual debts sooner; it costs
            more in interest but has better adherence for many people.

    Raises:
        ValueError: If the budget cannot cover the combined minimum payments,
            or if the plan fails to terminate (negative amortization).
    """
    if strategy not in ("avalanche", "snowball"):
        raise ValueError(f"strategy must be 'avalanche' or 'snowball', got {strategy!r}")
    if not debts:
        raise ValueError("debts list is empty")

    working: list[_Debt] = []
    for d in debts:
        try:
            entry = _Debt(
                name=str(d["name"]),
                balance=float(d["balance"]),
                rate=apr_monthly_rate(float(d["apr"])),
                minimum=float(d["minimum_payment"]),
            )
        except KeyError as exc:
            raise ValueError(
                f"each debt needs name/balance/apr/minimum_payment; missing {exc}"
            ) from exc
        _check_non_negative(f"{entry.name}.balance", entry.balance)
        _check_non_negative(f"{entry.name}.minimum_payment", entry.minimum)
        working.append(entry)

    total_minimum = sum(d.minimum for d in working)
    if monthly_budget < total_minimum:
        raise ValueError(
            f"monthly_budget {monthly_budget:.2f} is below the sum of minimum "
            f"payments {total_minimum:.2f}. The plan is infeasible; the user "
            f"needs to increase the budget or renegotiate terms."
        )

    total_interest = 0.0
    order: list[dict] = []
    month = 0

    while any(d.balance > 0.005 for d in working) and month < _MAX_MONTHS:
        month += 1
        # Interest accrues before payments are applied.
        for d in working:
            if d.balance > 0:
                interest = d.balance * d.rate
                d.balance += interest
                total_interest += interest

        budget = monthly_budget

        # Minimums first, capped at the outstanding balance.
        for d in working:
            if d.balance <= 0:
                continue
            pay = min(d.minimum, d.balance, budget)
            d.balance -= pay
            budget -= pay

        # Everything left goes to the target debt, and cascades onward the
        # moment that debt clears. This is what makes both strategies
        # "snowball": a cleared debt's payment rolls forward -- and it has to
        # roll forward *within* the month too. Applying the leftover to exactly
        # one debt and stopping strands the remainder until next month while
        # every other balance keeps accruing, which is money the user handed
        # over and got no credit for. The target is re-selected on each pass, so
        # the strategy ordering still decides who is next.
        while budget > 0:
            remaining = [d for d in working if d.balance > 0.005]
            if not remaining:
                break
            if strategy == "avalanche":
                target = max(remaining, key=lambda d: d.rate)
            else:
                target = min(remaining, key=lambda d: d.balance)
            pay = min(budget, target.balance)
            if pay <= 0:
                # Nothing else in this loop changes, so a pass that pays out
                # nothing would spin forever. Bail rather than hang.
                break
            target.balance -= pay
            budget -= pay

        for d in working:
            if 0 < d.balance <= 0.005:
                d.balance = 0.0  # absorb float dust
            if d.balance == 0.0 and not any(o["name"] == d.name for o in order):
                order.append({"name": d.name, "paid_off_month": month})

    # Test the balances, not the counter. A plan whose final payment lands
    # exactly on month _MAX_MONTHS has cleared, and reporting it as impossible
    # would tell the user a workable plan cannot work.
    if any(d.balance > 0.005 for d in working):
        raise ValueError(
            "debts do not amortize within 100 years at this budget -- interest "
            "is outpacing payments. Increase the budget or reduce the rates."
        )

    return DebtPayoffResult(
        strategy=strategy,
        months_to_debt_free=month,
        total_interest_paid=round(total_interest, 2),
        payoff_order=order,
    )


def withdrawal_sustainability(
    *,
    portfolio_value: float,
    annual_withdrawal: float,
    years: int,
    annual_return: float,
    annual_inflation: float = 0.025,
) -> dict:
    """Test whether a portfolio survives a given inflation-adjusted drawdown.

    This is a single deterministic path, **not** a Monte Carlo simulation. It
    ignores sequence-of-returns risk, which is the dominant risk in early
    retirement -- a portfolio that survives this test can still fail in practice
    if poor returns land in the first few years. Treat the result as a
    necessary condition, not a sufficient one.
    """
    _check_non_negative("portfolio_value", portfolio_value)
    _check_non_negative("annual_withdrawal", annual_withdrawal)
    _check_rate("annual_return", annual_return)
    _check_rate("annual_inflation", annual_inflation)
    if not 0 < years <= _MAX_YEARS:
        raise ValueError(f"years must be in (0, {_MAX_YEARS}], got {years}")

    balance = float(portfolio_value)
    withdrawal = float(annual_withdrawal)
    depleted_year: int | None = None
    path: list[dict[str, float]] = []

    for year in range(1, years + 1):
        if depleted_year is None:
            balance = balance * (1.0 + annual_return) - withdrawal
            if balance <= 0:
                # Clamp at zero and stop drawing down. Without this the loop
                # keeps subtracting from an empty portfolio and reports a
                # meaningless large negative "balance".
                depleted_year = year
                balance = 0.0
        path.append({"year": year, "balance": round(balance, 2)})
        withdrawal *= 1.0 + annual_inflation

    initial_rate = annual_withdrawal / portfolio_value if portfolio_value else 0.0
    return {
        "survives_horizon": depleted_year is None,
        "depleted_in_year": depleted_year,
        "ending_balance": round(balance, 2),
        "initial_withdrawal_rate": round(initial_rate, 4),
        "yearly_path": path,
        "caveat": (
            "Deterministic single-path projection. Ignores sequence-of-returns "
            "risk and market volatility; a Monte Carlo simulation is required "
            "for a genuine probability of success."
        ),
    }
