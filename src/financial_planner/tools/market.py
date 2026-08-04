"""Market data tools backed by yfinance (no API key required).

Scope note: this is a *personal planning* agent, not a trading agent. These
tools exist to price a user's existing holdings and to surface the two numbers
that actually change long-run outcomes for an index investor -- expense ratios
and realized long-horizon returns -- not to support security selection or
timing.
"""

from __future__ import annotations

import json
from typing import Any

import yfinance as yf
from langchain.tools import tool

MAX_TICKERS = 15
TRADING_DAYS = 252


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)


def _err(exc: Exception) -> str:
    return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


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
        range. Symbols that cannot be resolved appear with an "error" key rather
        than failing the whole call.
    """
    try:
        symbols = _clean(tickers)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)

    results: dict[str, Any] = {}
    for sym in symbols:
        try:
            fi = yf.Ticker(sym).fast_info
            price = fi.get("lastPrice")
            if price is None:
                results[sym] = {"error": "no price available; check the symbol"}
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
            results[sym] = {"error": f"{type(exc).__name__}: {exc}"}
    return _ok({"quotes": results})


def _round(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


@tool
def get_fund_profile(ticker: str) -> str:
    """Get the expense ratio and category for a fund or ETF.

    Call this whenever a user names a fund they hold or is considering. The
    expense ratio is the single most reliable lever on long-run outcomes that a
    household actually controls, so surface it and translate it into dollars:
    a 0.75% fee against a 0.03% alternative is roughly a quarter of the
    portfolio's growth over a working lifetime.

    Args:
        ticker: A fund or ETF symbol, e.g. "VTI".

    Returns:
        JSON with name, quote_type, category and expense_ratio (as a decimal;
        0.0003 means 0.03%). Fields are null when the provider lacks the data.
    """
    try:
        sym = _clean([ticker])[0]
        info = yf.Ticker(sym).info or {}
        expense = info.get("netExpenseRatio")
        if expense is None:
            expense = info.get("annualReportExpenseRatio")
        return _ok(
            {
                "ticker": sym,
                "name": info.get("longName") or info.get("shortName"),
                "quote_type": info.get("quoteType"),
                "category": info.get("category"),
                # yfinance reports this inconsistently: some feeds give percent
                # (0.03 == 0.03%), others a decimal. Report the raw value and
                # label the ambiguity rather than silently guessing a scale.
                "expense_ratio_raw": expense,
                "expense_ratio_note": (
                    "Provider reports this as a percentage figure (0.03 means "
                    "0.03%). Confirm against the fund's own factsheet before "
                    "quoting it to the user."
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@tool
def get_historical_return(ticker: str, years: int = 10) -> str:
    """Get the realized annualized return and volatility for a ticker.

    Call this to ground a return assumption in evidence instead of picking a
    number. Use the result to *inform* the `annual_return` argument you pass to
    `project_savings` -- and tell the user that past returns are an input to the
    assumption, not a forecast.

    Args:
        ticker: Symbol to analyze, e.g. "VTI".
        years: Lookback window in years, 1-25. Defaults to 10.

    Returns:
        JSON with annualized_return and annualized_volatility (both decimals),
        total_return, the window actually covered, and a caveat to relay.
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
        # count only equals time if the series has exactly TRADING_DAYS bars a
        # year -- untrue after a trading halt, for a non-US calendar, or for any
        # non-daily series, and the resulting CAGR error is then fed straight
        # into a multi-decade projection as an annual_return assumption.
        elapsed_days = (closes.index[-1] - closes.index[0]).days
        span_years = elapsed_days / 365.25
        if span_years <= 0:
            raise ValueError(f"price history for {sym!r} spans no measurable time")
        total_return = end / start - 1.0
        cagr = (end / start) ** (1.0 / span_years) - 1.0
        volatility = float(closes.pct_change().dropna().std() * (TRADING_DAYS**0.5))

        return _ok(
            {
                "ticker": sym,
                "years_covered": round(span_years, 2),
                "annualized_return": round(cagr, 4),
                "annualized_volatility": round(volatility, 4),
                "total_return": round(total_return, 4),
                "basis": "total return (dividends reinvested, split-adjusted)",
                "caveat": (
                    "Realized past return over one window. It is an input to a "
                    "forward assumption, not a forecast; a 10-year window that "
                    "excludes a major drawdown will read optimistically."
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


MARKET_TOOLS = [get_quote, get_fund_profile, get_historical_return]
