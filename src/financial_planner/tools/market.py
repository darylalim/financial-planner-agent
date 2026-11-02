"""Market data tools backed by yfinance (no API key required).

Scope note: this is a *personal planning* agent, not a trading agent. These
tools exist to price a user's existing holdings and to surface the two numbers
that actually change long-run outcomes for an index investor -- expense ratios
and realized long-horizon returns -- not to support security selection or
timing.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import yfinance as yf
from langchain.tools import tool

from financial_planner.envelope import err, ok

MAX_TICKERS = 15


def _clean(tickers: list[str]) -> list[str]:
    seen: list[str] = []
    for t in tickers:
        sym = str(t).strip().upper()
        if sym and sym not in seen:
            seen.append(sym)
    if not seen:
        raise ValueError("no valid tickers supplied")
    if len(seen) > MAX_TICKERS:
        raise ValueError(f"too many tickers ({len(seen)}); limit is {MAX_TICKERS} per call")
    return seen


@tool
def get_quote(tickers: list[str]) -> str:
    """Look up current prices for one or more tickers.

    Call this to value a user's holdings, or when they mention a ticker and you
    need its current price. Batch symbols into a single call rather than making
    one call per ticker.

    Args:
        tickers: Ticker symbols, e.g. ["VTI", "VXUS", "BND"]. Max 15 per call.

    Returns:
        JSON mapping each ticker to price, currency, quote_type and 52-week
        range, plus a "failed" list naming the symbols that did not resolve.
        A symbol that cannot be resolved appears with an "error" key rather than
        failing the whole call; if *every* symbol fails, the call returns an
        error envelope instead of a batch of failures.
    """
    try:
        symbols = _clean(tickers)
    except Exception as exc:  # noqa: BLE001
        return err(exc)

    results: dict[str, Any] = {}
    # Named separately from `results` so a partial failure is visible without
    # walking every entry looking for an "error" key -- the model reads the
    # summary, not the whole payload, when a batch is large.
    failed: list[str] = []

    # One place a failure is recorded, so the two collections cannot fall out of
    # step. They did not yet, but a third failure branch that appended to
    # `failed` and forgot the rest is the obvious next edit.
    def record_failure(symbol: str, reason: str) -> None:
        results[symbol] = {"error": reason}
        failed.append(symbol)

    for sym in symbols:
        try:
            fi = yf.Ticker(sym).fast_info
            price = fi.get("lastPrice")
            if price is None:
                record_failure(sym, "no price available; check the symbol")
                continue
            results[sym] = {
                "price": round(float(price), 2),
                "currency": fi.get("currency"),
                "quote_type": fi.get("quoteType"),
                "previous_close": _round(fi.get("previousClose")),
                "year_high": _round(fi.get("yearHigh")),
                "year_low": _round(fi.get("yearLow")),
            }
        except Exception as exc:  # noqa: BLE001 - per-symbol isolation
            record_failure(sym, f"{type(exc).__name__}: {exc}")

    # An all-failed call differs in kind from a partial one, not in degree. A
    # partial batch still carries prices the model can use, so it stays a
    # success that names its casualties. An all-failed batch carries nothing --
    # and `streaming._is_error_result` decides failure by looking for an error
    # envelope, so returning ok() here paints a wholly failed lookup green in
    # the UI while the model reads a "successful" result containing no prices.
    if failed and len(failed) == len(symbols):
        # Read the reason back out of the payload rather than tracking it
        # alongside: it is derivable, and a tracked copy is one more thing every
        # future failure branch has to remember to set.
        first_reason = results[failed[0]]["error"]
        return err(f"no quotes resolved for {', '.join(failed)}; first reason: {first_reason}")
    return ok({"quotes": results, "failed": failed})


def _round(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


# yfinance exposes the expense ratio through two fields on two *different*
# scales, and which one a fund populates varies by feed. Each field's scale is
# fixed, though (checked on VTI, VOO, SPY, QQQ, ARKK, FXAIX, VFIAX and AGTHX, and
# where a fund has both they agree), so the tool converts and the model never
# has to: a misread scale is a 100x error in a fee the user acts on.
#
# Insertion order is *preference* order -- the net ratio is the one a fund
# actually charges after waivers, so it wins where both are present.
_EXPENSE_RATIO_FIELDS = {
    "netExpenseRatio": (1, "a percentage figure (0.03 means 0.03%)"),
    "annualReportExpenseRatio": (100, "a decimal fraction (0.0003 means 0.03%)"),
}
_FACTSHEET_CAVEAT = "Confirm against the fund's own factsheet before relying on it."


def _expense_ratio(info: dict) -> tuple[Any, str | None, float | None, str | None]:
    """The raw value, the field it came from, the percentage, and a note."""
    for field, (to_percent, scale) in _EXPENSE_RATIO_FIELDS.items():
        raw = info.get(field)
        if raw is None:
            continue
        try:
            # Rounded because the fraction field carries float noise: FXAIX
            # reports 0.00014999999, which would otherwise surface as 0.014999999%.
            percent = round(float(raw) * to_percent, 4)
        except (TypeError, ValueError):
            percent = None
        note = f"The provider reports {field} as {scale}. {_FACTSHEET_CAVEAT}"
        return raw, field, percent, note
    return None, None, None, None


@tool
def get_fund_profile(ticker: str) -> str:
    """Get the expense ratio and category for a fund or ETF.

    Call this whenever a user names a fund they hold or is considering. The
    expense ratio is the single most reliable lever on long-run outcomes that a
    household actually controls, so surface it. To put a fee in dollars, pass
    expense_ratio_percent as is to `compare_fund_fees`; never estimate the cost
    yourself.

    Args:
        ticker: A fund or ETF symbol, e.g. "VTI".

    Returns:
        JSON with name, quote_type, category, expense_ratio_percent (already a
        percentage: 0.03 means 0.03%), expense_ratio_raw and
        expense_ratio_source (the provider's own value and field, for audit),
        and expense_ratio_note, a caveat to relay. Report expense_ratio_percent;
        do not convert any of these. Fields are null when the provider lacks
        the data.
    """
    try:
        sym = _clean([ticker])[0]
        info = yf.Ticker(sym).info or {}
        raw, source, percent, note = _expense_ratio(info)
        return ok(
            {
                "ticker": sym,
                "name": info.get("longName") or info.get("shortName"),
                "quote_type": info.get("quoteType"),
                "category": info.get("category"),
                "expense_ratio_percent": percent,
                # The provider's own number, unconverted, so the user can check
                # it against a factsheet.
                "expense_ratio_raw": raw,
                "expense_ratio_source": source,
                "expense_ratio_note": note,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return err(exc)


def _realized_swings(closes: pd.Series) -> dict[str, float | None]:
    """The worst and best 12-month returns and the deepest fall in ``closes``.

    Returned so the model can say how much a holding swings without turning
    the volatility into a range itself: an after-fix live run read an 18%
    volatility as "swings of 20-30%", a figure no tool produced. These are what
    actually happened in the window, not a distribution's band.

    A 12-month return is measured from the last close at or before the same
    date a year earlier, so gaps in the series cannot shorten the year. Null unless
    the history runs past a year: a 1-year window has no full year to compare.
    """
    year_ago = closes.asof(closes.index - pd.DateOffset(years=1))
    twelve_month = pd.Series(closes.to_numpy() / year_ago.to_numpy() - 1.0).dropna()
    drawdown = float((closes / closes.cummax() - 1.0).min())
    return {
        "worst_12_month_return": round(float(twelve_month.min()), 4) if len(twelve_month) else None,
        "best_12_month_return": round(float(twelve_month.max()), 4) if len(twelve_month) else None,
        "max_drawdown": round(drawdown, 4),
    }


@tool
def get_historical_return(ticker: str, years: int = 10) -> str:
    """Get the realized annualized return and volatility for a ticker.

    Call this to ground a return assumption in evidence instead of picking a
    number. Use the result to *inform* the `annual_return` argument you pass to
    `project_savings` -- and tell the user that past returns are an input to the
    assumption, not a forecast.

    If you say how much it swings, quote worst_12_month_return,
    best_12_month_return and max_drawdown; never turn the volatility into a
    range yourself.

    Args:
        ticker: Symbol to analyze, e.g. "VTI".
        years: Lookback window in years, 1-25. Defaults to 10.

    Returns:
        JSON with annualized_return and annualized_volatility, total_return,
        growth_of_1_dollar (what $1 at the start was worth at the end),
        worst_12_month_return and best_12_month_return (null unless the history
        runs past a year), max_drawdown (the deepest peak-to-trough fall, negative), the
        window actually covered, and a caveat to relay. Every rate is a
        decimal: -0.25 means -25%.
    """
    try:
        sym = _clean([ticker])[0]
        if not 1 <= years <= 25:
            raise ValueError(f"years must be between 1 and 25, got {years}")

        # auto_adjust=True folds dividends and splits into Close, making this a
        # total-return series. Price-only return would understate a dividend
        # payer badly over a long window.
        hist = yf.Ticker(sym).history(period=f"{years}y", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            raise ValueError(f"no price history available for {sym!r}")

        closes = hist["Close"].dropna()
        start, end = float(closes.iloc[0]), float(closes.iloc[-1])

        # Annualize over elapsed calendar time, not the number of bars. Row
        # count only equals time if the series has exactly 252 bars a
        # year -- untrue after a trading halt, for a non-US calendar, or for any
        # non-daily series, and the resulting CAGR error is then fed straight
        # into a multi-decade projection as an annual_return assumption.
        elapsed_days = (closes.index[-1] - closes.index[0]).days
        span_years = elapsed_days / 365.25
        if span_years <= 0:
            raise ValueError(f"price history for {sym!r} spans no measurable time")
        total_return = end / start - 1.0
        cagr = (end / start) ** (1.0 / span_years) - 1.0

        # Scale by the bars this series actually contains per year, derived the
        # same way the CAGR is. Multiplying by sqrt(252) instead asserted daily
        # US trading bars: correct for the common case and wrong by a factor of
        # ~4.6 on a weekly series and ~14.5 on a monthly one, in the direction
        # that makes a portfolio look far more volatile than it is. The two
        # figures in this payload were annualized on different bases, so they
        # could not both be right for the same input.
        returns = closes.pct_change().dropna()
        periods_per_year = len(returns) / span_years
        volatility = float(returns.std() * (periods_per_year**0.5))

        return ok(
            {
                "ticker": sym,
                "years_covered": round(span_years, 2),
                "annualized_return": round(cagr, 4),
                "annualized_volatility": round(volatility, 4),
                "total_return": round(total_return, 4),
                # What $1 became. A live run added 1 to total_return itself to
                # say "$1 grew to about $4.07", a figure no tool produced.
                "growth_of_1_dollar": round(end / start, 4),
                **_realized_swings(closes),
                "basis": "total return (dividends reinvested, split-adjusted)",
                "caveat": (
                    "Realized past return over one window. It is an input to a "
                    "forward assumption, not a forecast; a 10-year window that "
                    "excludes a major drawdown will read optimistically."
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return err(exc)


MARKET_TOOLS = [get_quote, get_fund_profile, get_historical_return]
