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
* **Estimates are whole dollars; amounts that must match a statement keep
  cents.** A projection, a withdrawal path, a fee comparison or a required
  contribution rests on an assumed return, so cents on it are false precision,
  and a model shown ``820949.52`` wrote "$820,949" -- a truncation no tool
  produced. A loan payment, debt interest or a spending total keeps cents,
  because ``$1,798.65`` has to match the user's statement.
* "Real" values are inflation-adjusted into today's purchasing power.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

__all__ = [
    "AmortizationResult",
    "DebtPayoffResult",
    "ProjectionResult",
    "amortize_loan",
    "apr_monthly_rate",
    "compare_fees",
    "monthly_rate",
    "payoff_debts",
    "project_portfolio",
    "real_rate",
    "required_monthly_contribution",
    "simulate_withdrawal",
    "whole_dollars",
    "sum_amounts",
    "withdrawal_sustainability",
]

# Guard rails. These are deliberately generous -- they exist to catch unit
# mistakes (7 instead of 0.07) and runaway loops, not to express opinions.
_MAX_YEARS = 100
_MAX_MONTHS = _MAX_YEARS * 12
_MAX_RATE = 1.0  # 100% annual
# The dearest ordinary funds charge around 2.5%. Ten percent is far past that and
# well short of 59% -- what 0.59 means if a percentage is passed as a decimal.
_MAX_EXPENSE_RATIO = 0.10


def _check_rate(name: str, value: float) -> None:
    if not -_MAX_RATE <= value <= _MAX_RATE:
        raise ValueError(
            f"{name}={value} is outside the supported range "
            f"[-1.0, 1.0]. Rates are decimals: use 0.07 for 7%, not 7."
        )


def _check_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def whole_dollars(amount: float) -> int:
    """An estimate rounded to the dollar, halves away from zero (not to even)."""
    return int(math.copysign(math.floor(abs(amount) + 0.5), amount))


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

    final_balance_nominal: int
    final_balance_real: int
    total_contributed: int
    total_growth: int
    years: int
    yearly_balances: list[dict[str, int]] = field(default_factory=list)

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
    yearly: list[dict[str, int]] = []

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
                "balance_nominal": whole_dollars(balance),
                "balance_real": whole_dollars(balance / deflator),
                "contributed_to_date": whole_dollars(total_contributed),
            }
        )

    deflator = (1.0 + annual_inflation) ** years
    final, contributed = whole_dollars(balance), whole_dollars(total_contributed)
    return ProjectionResult(
        final_balance_nominal=final,
        final_balance_real=whole_dollars(balance / deflator),
        total_contributed=contributed,
        # From the rounded parts, so the three add up to the dollar.
        total_growth=final - whole_dollars(starting_balance) - contributed,
        years=years,
        yearly_balances=yearly,
    )


def required_monthly_contribution(
    *,
    target_amount: float,
    starting_balance: float,
    years: int,
    annual_return: float,
) -> int:
    """Solve for the monthly contribution needed to reach ``target_amount``.

    Inverts the ordinary-annuity future-value formula::

        FV = PV(1+r)^n + PMT * [((1+r)^n - 1) / r]

    Returns ``0`` when the starting balance already compounds past the target
    on its own. Rounded *up* to the dollar, unlike the other estimates: a
    required amount rounded down would fall short of the target it was solved for.
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
        return 0
    payment = shortfall / n if r == 0 else shortfall / (((1.0 + r) ** n - 1.0) / r)
    # Rounded to the cent first, so float noise (1654.0000000001) cannot add a dollar.
    return math.ceil(round(payment, 2))


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
    # The split every answer about the plan describes: what the minimums take,
    # and what is left to aim at the target debt. Returned so the model never
    # sums the minimums or subtracts them from the budget itself.
    total_minimum_payments: float = 0.0
    extra_beyond_minimums: float = 0.0

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
    # What each debt was paid last month: a cleared debt's last full payment is
    # what rolls to the next target ("add the card's $630 to the car loan").
    last_month: dict[str, float] = {}

    while any(d.balance > 0.005 for d in working) and month < _MAX_MONTHS:
        month += 1
        # Interest accrues before payments are applied.
        for d in working:
            if d.balance > 0:
                interest = d.balance * d.rate
                d.balance += interest
                total_interest += interest

        budget = monthly_budget
        paid: dict[str, float] = {}

        # Minimums first, capped at the outstanding balance.
        for d in working:
            if d.balance <= 0:
                continue
            pay = min(d.minimum, d.balance, budget)
            d.balance -= pay
            budget -= pay
            paid[d.name] = paid.get(d.name, 0.0) + pay

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
            # Strictly positive: `budget > 0` is the loop condition and
            # `target.balance > 0.005` is what put the target in `remaining`.
            # Termination does not rest on a zero-payment guard, then -- each
            # pass either exhausts the budget or drops one debt out of
            # `remaining`, which bounds the loop at len(working) + 1 passes.
            pay = min(budget, target.balance)
            target.balance -= pay
            budget -= pay
            paid[target.name] = paid.get(target.name, 0.0) + pay

        for d in working:
            if 0 < d.balance <= 0.005:
                d.balance = 0.0  # absorb float dust
            if d.balance == 0.0 and not any(o["name"] == d.name for o in order):
                # The final month's payment is only the remainder, so the full
                # one is the month before -- unless it cleared in month 1.
                full = last_month.get(d.name, paid.get(d.name, 0.0))
                order.append(
                    {
                        "name": d.name,
                        "paid_off_month": month,
                        "monthly_payment_before_payoff": round(full, 2),
                    }
                )
        last_month = paid

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
        total_minimum_payments=round(total_minimum, 2),
        extra_beyond_minimums=round(monthly_budget - total_minimum, 2),
    )


def _first_year_withdrawal(
    *,
    portfolio_value: float,
    years: int,
    annual_return: float,
    annual_withdrawal: float | None,
    monthly_withdrawal: float | None,
    annual_inflation: float,
) -> float:
    """Validate a drawdown's inputs and return the first year's withdrawal.

    Takes exactly one of ``annual_withdrawal`` or ``monthly_withdrawal``.
    People state retirement spending per month; an annual-only input made the
    model multiply by twelve itself and quote the product, a figure no tool
    had produced.
    """
    if (annual_withdrawal is None) == (monthly_withdrawal is None):
        raise ValueError("give exactly one of annual_withdrawal or monthly_withdrawal")
    if monthly_withdrawal is not None:
        _check_non_negative("monthly_withdrawal", monthly_withdrawal)
        annual_withdrawal = monthly_withdrawal * 12
    assert annual_withdrawal is not None
    _check_non_negative("portfolio_value", portfolio_value)
    _check_non_negative("annual_withdrawal", annual_withdrawal)
    _check_rate("annual_return", annual_return)
    _check_rate("annual_inflation", annual_inflation)
    if not 0 < years <= _MAX_YEARS:
        raise ValueError(f"years must be in (0, {_MAX_YEARS}], got {years}")
    return float(annual_withdrawal)


def _withdrawal_figures(portfolio_value: float, annual_withdrawal: float) -> dict:
    """The first-year figures both drawdown tools return, so either can be quoted alone."""
    initial_rate = annual_withdrawal / portfolio_value if portfolio_value else 0.0
    return {
        "initial_withdrawal_rate": round(initial_rate, 4),
        "first_year_annual_withdrawal": whole_dollars(annual_withdrawal),
        "first_year_monthly_withdrawal": whole_dollars(annual_withdrawal / 12),
    }


def _draw_down(
    *,
    portfolio_value: float,
    first_year_withdrawal: float,
    annual_inflation: float,
    growth_factors: Iterable[float],
) -> tuple[list[float], int | None]:
    """The yearly step both drawdown tools take, so the two cannot drift apart.

    Each year the balance grows by that year's factor, then the year's
    withdrawal comes out; the withdrawal then grows with inflation. Returns
    the end-of-year balances and the year the money ran out, or ``None``.
    """
    balance = float(portfolio_value)
    withdrawal = float(first_year_withdrawal)
    depleted_year: int | None = None
    balances: list[float] = []

    for year, factor in enumerate(growth_factors, start=1):
        if depleted_year is None:
            balance = balance * factor - withdrawal
            if balance <= 0:
                # Clamp at zero and stop drawing down. Without this the loop
                # keeps subtracting from an empty portfolio and reports a
                # meaningless large negative "balance".
                depleted_year = year
                balance = 0.0
        balances.append(balance)
        withdrawal *= 1.0 + annual_inflation
    return balances, depleted_year


def withdrawal_sustainability(
    *,
    portfolio_value: float,
    years: int,
    annual_return: float,
    annual_withdrawal: float | None = None,
    monthly_withdrawal: float | None = None,
    annual_inflation: float = 0.025,
) -> dict:
    """Test whether a portfolio survives a given inflation-adjusted drawdown.

    This is a single deterministic path, **not** a Monte Carlo simulation. It
    ignores sequence-of-returns risk, which is the dominant risk in early
    retirement -- a portfolio that survives this test can still fail in practice
    if poor returns land in the first few years. Treat the result as a
    necessary condition, not a sufficient one; :func:`simulate_withdrawal`
    gives the probability.

    Takes the first-year withdrawal as exactly one of ``annual_withdrawal`` or
    ``monthly_withdrawal``, and returns both.
    """
    annual = _first_year_withdrawal(
        portfolio_value=portfolio_value,
        years=years,
        annual_return=annual_return,
        annual_withdrawal=annual_withdrawal,
        monthly_withdrawal=monthly_withdrawal,
        annual_inflation=annual_inflation,
    )
    balances, depleted_year = _draw_down(
        portfolio_value=portfolio_value,
        first_year_withdrawal=annual,
        annual_inflation=annual_inflation,
        growth_factors=[1.0 + annual_return] * years,
    )
    return {
        "survives_horizon": depleted_year is None,
        "depleted_in_year": depleted_year,
        "ending_balance": whole_dollars(balances[-1]),
        **_withdrawal_figures(portfolio_value, annual),
        "yearly_path": [
            {"year": year, "balance": whole_dollars(balance)}
            for year, balance in enumerate(balances, start=1)
        ],
        "caveat": (
            "Deterministic single-path projection. Ignores sequence-of-returns "
            "risk and market volatility; simulate_withdrawal_plan gives a "
            "probability of success."
        ),
    }


_MAX_RETIREMENT_AGE = 120


def _log_sigma(median_factor: float, volatility: float) -> float:
    """The log-space spread that gives yearly returns a standard deviation of
    ``volatility`` around a lognormal whose median is ``median_factor``.

    For ``X = m * exp(s * Z)``, ``Var(X) = m^2 * u * (u - 1)`` with
    ``u = exp(s^2)``; solving the quadratic in ``u`` gives ``s``. Using the
    measured volatility as ``s`` directly would understate the spread.
    """
    ratio = volatility / median_factor
    u = (1.0 + math.sqrt(1.0 + 4.0 * ratio * ratio)) / 2.0
    return math.sqrt(math.log(u))


def _nearest_rank(ordered: list, share: float):
    """The value at or below which ``share`` of a sorted list falls (nearest rank)."""
    return ordered[max(math.ceil(share * len(ordered)), 1) - 1]


def simulate_withdrawal(
    *,
    portfolio_value: float,
    years: int,
    annual_return: float,
    annual_volatility: float,
    seed: int,
    annual_withdrawal: float | None = None,
    monthly_withdrawal: float | None = None,
    annual_inflation: float = 0.025,
    retirement_age: int | None = None,
    paths: int = 10_000,
) -> dict:
    """The share of simulated markets in which an inflation-adjusted drawdown lasts.

    Runs :func:`withdrawal_sustainability`'s yearly step ``paths`` times, each
    year's growth drawn from a lognormal whose *median* is ``1 + annual_return``
    (so the middle path stays near the deterministic one) and whose yearly
    returns have a standard deviation of ``annual_volatility``. Inflation stays
    fixed. With ``annual_volatility=0`` every path is the deterministic one.

    ``seed`` is a parameter so the result is reproducible: the same inputs
    always give the same answer, which a probability that moved on a rerun
    would not. Probabilities are rounded to two decimals -- at 10,000 paths the
    sampling error is about half a point, so a decimal place would be false
    precision. Every figure an answer needs is returned, so none has to be
    derived: both probabilities, ages as well as years.
    """
    annual = _first_year_withdrawal(
        portfolio_value=portfolio_value,
        years=years,
        annual_return=annual_return,
        annual_withdrawal=annual_withdrawal,
        monthly_withdrawal=monthly_withdrawal,
        annual_inflation=annual_inflation,
    )
    _check_non_negative("annual_volatility", annual_volatility)
    _check_rate("annual_volatility", annual_volatility)
    if annual_return <= -1.0:
        raise ValueError("annual_return must be above -1.0 (a total loss every year)")
    if retirement_age is not None and not 0 <= retirement_age <= _MAX_RETIREMENT_AGE:
        raise ValueError(
            f"retirement_age must be in [0, {_MAX_RETIREMENT_AGE}], got {retirement_age}"
        )
    if paths < 1:
        raise ValueError(f"paths must be at least 1, got {paths}")

    median_factor = 1.0 + annual_return
    sigma = _log_sigma(median_factor, annual_volatility)
    rng = random.Random(seed)
    endings: list[float] = []
    depletions: list[float] = []  # the year each path ran out; inf if it never did
    for _ in range(paths):
        # exp(0.0) is exactly 1.0, so zero volatility reproduces 1 + r to the bit.
        factors = (median_factor * math.exp(sigma * rng.gauss()) for _ in range(years))
        balances, depleted_year = _draw_down(
            portfolio_value=portfolio_value,
            first_year_withdrawal=annual,
            annual_inflation=annual_inflation,
            growth_factors=factors,
        )
        endings.append(balances[-1])
        depletions.append(math.inf if depleted_year is None else depleted_year)
    endings.sort()
    depletions.sort()

    survived = sum(1 for year in depletions if year == math.inf)
    # Whole percent, halves up, in integers so the two always add to 100.
    success_percent = (200 * survived + paths) // (2 * paths)
    deflator = (1.0 + annual_inflation) ** years

    def balances_at(scale: float) -> dict[str, int]:
        return {
            label: whole_dollars(_nearest_rank(endings, share) / scale)
            for label, share in (
                ("10th_percentile", 0.10),
                ("median", 0.50),
                ("90th_percentile", 0.90),
            )
        }

    def depleted_by(share: float) -> int | None:
        year = _nearest_rank(depletions, share)
        return None if year == math.inf else int(year)

    worst_10, worst_25 = depleted_by(0.10), depleted_by(0.25)
    result: dict = {
        "success_probability": success_percent / 100,
        "failure_probability": (100 - success_percent) / 100,
        "ending_balance": balances_at(1.0),
        "ending_balance_real": balances_at(deflator),
        "worst_10_percent_depleted_by_year": worst_10,
        "worst_25_percent_depleted_by_year": worst_25,
    }
    if retirement_age is not None:
        # The age at the end of that year: the drawdown starts at retirement_age.
        def age(year: int | None) -> int | None:
            return None if year is None else retirement_age + year

        result |= {
            "horizon_end_age": age(years),
            "worst_10_percent_depleted_by_age": age(worst_10),
            "worst_25_percent_depleted_by_age": age(worst_25),
        }
    return result | {
        **_withdrawal_figures(portfolio_value, annual),
        "years": years,
        "annual_return": annual_return,
        "annual_volatility": annual_volatility,
        "annual_inflation": annual_inflation,
        "paths": paths,
        "seed": seed,
        "assumptions": [
            "Each year's return is drawn at random: its median is annual_return "
            "and its spread is annual_volatility.",
            "Inflation is fixed at annual_inflation, and the withdrawal grows with it.",
            "One return and one volatility for the whole portfolio: no asset mix, "
            "rebalancing, fees or taxes.",
            "The withdrawal comes out at the end of each year, after that year's growth.",
            "The seed is fixed, so the same inputs always give the same result.",
        ],
        "caveat": (
            "A model, not a forecast. Running out in a simulated market means "
            "spending would have had to be cut, not that nothing is left to live on."
        ),
    }


def _grow_net_of_fee(
    *,
    starting_balance: float,
    monthly_contribution: float,
    years: int,
    gross_annual_return: float,
    expense_ratio: float,
) -> tuple[float, float]:
    """The ending balance and the fees paid, with the fee taken off the return."""
    gross_month = monthly_rate(gross_annual_return)
    net_month = monthly_rate(gross_annual_return - expense_ratio)
    balance, fees = float(starting_balance), 0.0
    for _ in range(years * 12):
        # The month's fee is the growth the gross return would have added and
        # the net return does not: charging it this way keeps the balance on
        # the net path exactly, so the fees reconcile with the ending balances.
        fees += balance * (gross_month - net_month)
        balance = balance * (1.0 + net_month) + monthly_contribution
    return balance, fees


def compare_fees(
    *,
    starting_balance: float,
    monthly_contribution: float,
    years: int,
    gross_annual_return: float,
    current_expense_ratio: float,
    alternative_expense_ratio: float,
) -> dict:
    """What a fund's fee costs against a cheaper (or dearer) alternative.

    The expense ratio comes off the annual return before the geometric monthly
    conversion: a fund returning 7% gross with a 0.59% fee is a 6.41% fund. Two
    ``project_portfolio`` calls could give the two balances, but their
    difference would then be the model's subtraction, so this returns it.

    Expense ratios are decimals like every rate here (0.0059 for 0.59%).
    """
    _check_non_negative("starting_balance", starting_balance)
    _check_non_negative("monthly_contribution", monthly_contribution)
    _check_rate("gross_annual_return", gross_annual_return)
    for name, ratio in (
        ("current_expense_ratio", current_expense_ratio),
        ("alternative_expense_ratio", alternative_expense_ratio),
    ):
        if not 0 <= ratio <= _MAX_EXPENSE_RATIO:
            raise ValueError(
                f"{name}={ratio} is outside [0, {_MAX_EXPENSE_RATIO}]. "
                "Expense ratios are decimals here: 0.0059 for 0.59%."
            )
        _check_rate("net annual return", gross_annual_return - ratio)
    if not 0 < years <= _MAX_YEARS:
        raise ValueError(f"years must be in (0, {_MAX_YEARS}], got {years}")

    common = {
        "starting_balance": starting_balance,
        "monthly_contribution": monthly_contribution,
        "years": years,
        "gross_annual_return": gross_annual_return,
    }
    current, current_fees = _grow_net_of_fee(**common, expense_ratio=current_expense_ratio)
    alternative, alternative_fees = _grow_net_of_fee(
        **common, expense_ratio=alternative_expense_ratio
    )
    # Rounded once, and every difference taken from the rounded figures, so
    # the gap below is exactly the fee difference plus the lost growth.
    current, current_fees = whole_dollars(current), whole_dollars(current_fees)
    alternative, alternative_fees = whole_dollars(alternative), whole_dollars(alternative_fees)
    return {
        "years": years,
        "total_contributed": whole_dollars(monthly_contribution * 12 * years),
        "current": {
            "expense_ratio": current_expense_ratio,
            "net_annual_return": round(gross_annual_return - current_expense_ratio, 6),
            "ending_balance": current,
            "total_fees_paid": current_fees,
        },
        "alternative": {
            "expense_ratio": alternative_expense_ratio,
            "net_annual_return": round(gross_annual_return - alternative_expense_ratio, 6),
            "ending_balance": alternative,
            "total_fees_paid": alternative_fees,
        },
        # Signed, and named for the direction: negative when the alternative
        # is the dearer fund, so no sign has to be inferred from the prose.
        "alternative_ending_balance_higher_by": alternative - current,
        "alternative_fees_lower_by": current_fees - alternative_fees,
        # The rest of the gap: what the fee money would have earned. Answers
        # explain the gap as fees plus this, so it is returned, not subtracted.
        "growth_lost_on_the_fee_difference": (alternative - current)
        - (current_fees - alternative_fees),
        "assumptions": [
            "Each expense ratio comes off the gross annual return before it is "
            "converted to a monthly rate.",
            "Both funds earn the same gross return; only the fee differs.",
            "Nominal dollars, not adjusted for inflation; no taxes or loads.",
            "Contributions at the end of each month, with no increases.",
        ],
    }


def sum_amounts(items: list[tuple[str, float]]) -> dict:
    """Total a list of labelled amounts. A negative amount is a subtraction.

    For totals no calculator produces -- two contribution limits from a search,
    say -- so the total still comes from a tool rather than the model.
    """
    if len(items) < 2:
        raise ValueError("give at least two amounts to total")
    for label, amount in items:
        if not math.isfinite(amount):
            raise ValueError(f"amount for {label!r} is not a finite number: {amount}")
    return {
        "items": [{"label": label, "amount": amount} for label, amount in items],
        "total": round(math.fsum(amount for _, amount in items), 2),
    }
