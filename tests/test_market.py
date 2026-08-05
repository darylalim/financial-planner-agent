"""Tests for the market data tools, with yfinance stubbed out.

These ran only under `scripts/live_check.py` before, which costs money and needs
a network, so in practice they did not run. The behaviour worth pinning is not
yfinance's -- it is what this module does to yfinance's output: ticker cleaning,
per-symbol error isolation, and the CAGR annualization, whose result is fed
straight into a multi-decade projection as an `annual_return` assumption.

`yf` is replaced at the module level rather than patching `yfinance.Ticker`
globally, so nothing here can reach the network even if a stub is incomplete.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from financial_planner.tools import market
from financial_planner.tools.market import (
    MAX_TICKERS,
    get_fund_profile,
    get_historical_return,
    get_quote,
)

QUOTE = {
    "lastPrice": 251.4321,
    "currency": "USD",
    "quoteType": "ETF",
    "previousClose": 249.1,
    "yearHigh": 260.0,
    "yearLow": 190.0,
}

FUND_INFO = {
    "longName": "Vanguard Total Stock Market ETF",
    "quoteType": "ETF",
    "category": "Large Blend",
    "netExpenseRatio": 0.03,
}


class FakeTicker:
    """Stands in for ``yfinance.Ticker`` for exactly the three attributes used."""

    def __init__(self, symbol, *, fast_info=None, info=None, history=None, raises=None):
        self.symbol = symbol
        self._fast_info = fast_info if fast_info is not None else dict(QUOTE)
        self._info = info if info is not None else dict(FUND_INFO)
        self._history = history
        self._raises = raises

    @property
    def fast_info(self):
        if self._raises:
            raise self._raises
        return self._fast_info

    @property
    def info(self):
        if self._raises:
            raise self._raises
        return self._info

    def history(self, period=None, auto_adjust=None):  # noqa: ARG002 - signature parity
        if self._raises:
            raise self._raises
        return self._history if self._history is not None else pd.DataFrame()


def install(monkeypatch, factory):
    """Replace the module's yfinance handle with one that calls `factory(symbol)`."""
    monkeypatch.setattr(market, "yf", SimpleNamespace(Ticker=factory))


def price_history(start_price, end_price, *, days, bars=60):
    """A close series rising geometrically over exactly `days` of calendar time.

    ``bars`` is deliberately unrelated to ``days``: annualizing by row count
    rather than elapsed time is the bug these fixtures exist to catch, so the
    two must not be able to stand in for each other.
    """
    start = pd.Timestamp("2016-01-01")
    index = pd.DatetimeIndex(
        [start + pd.Timedelta(days=round(i * days / (bars - 1))) for i in range(bars)]
    )
    ratio = (end_price / start_price) ** (1 / (bars - 1))
    closes = [start_price * ratio**i for i in range(bars)]
    return pd.DataFrame({"Close": closes}, index=index)


def call(tool, **kwargs) -> dict:
    return json.loads(tool.invoke(kwargs))


class TestTickerCleaning:
    def test_symbols_are_uppercased_and_stripped(self, monkeypatch):
        seen = []
        install(monkeypatch, lambda s: seen.append(s) or FakeTicker(s))
        call(get_quote, tickers=[" vti ", "bnd"])
        assert seen == ["VTI", "BND"]

    def test_duplicates_collapse_to_one_request(self, monkeypatch):
        seen = []
        install(monkeypatch, lambda s: seen.append(s) or FakeTicker(s))
        result = call(get_quote, tickers=["VTI", "vti", " VTI"])
        assert seen == ["VTI"]
        assert list(result["quotes"]) == ["VTI"]

    def test_an_empty_list_is_reported(self, monkeypatch):
        install(monkeypatch, FakeTicker)
        assert "error" in call(get_quote, tickers=[])

    def test_blank_strings_do_not_count_as_tickers(self, monkeypatch):
        install(monkeypatch, FakeTicker)
        assert "error" in call(get_quote, tickers=["", "  "])

    def test_the_batch_limit_is_enforced_before_any_request(self, monkeypatch):
        """The cap exists to bound latency, so it must trip before the loop."""
        seen = []
        install(monkeypatch, lambda s: seen.append(s) or FakeTicker(s))
        result = call(get_quote, tickers=[f"T{i}" for i in range(MAX_TICKERS + 1)])
        assert "error" in result
        assert seen == []


class TestGetQuote:
    def test_reports_price_and_range(self, monkeypatch):
        install(monkeypatch, FakeTicker)
        quote = call(get_quote, tickers=["VTI"])["quotes"]["VTI"]
        assert quote["price"] == 251.43
        assert quote["currency"] == "USD"
        assert (quote["year_low"], quote["year_high"]) == (190.0, 260.0)

    def test_one_bad_symbol_does_not_fail_the_batch(self, monkeypatch):
        """Per-symbol isolation: a typo must not cost the other lookups."""

        def factory(symbol):
            if symbol == "NOPE":
                return FakeTicker(symbol, raises=RuntimeError("delisted"))
            return FakeTicker(symbol)

        install(monkeypatch, factory)
        quotes = call(get_quote, tickers=["VTI", "NOPE", "BND"])["quotes"]
        assert set(quotes) == {"VTI", "NOPE", "BND"}
        assert "error" in quotes["NOPE"]
        assert quotes["VTI"]["price"] == 251.43

    def test_a_missing_price_is_reported_per_symbol(self, monkeypatch):
        install(monkeypatch, lambda s: FakeTicker(s, fast_info={"currency": "USD"}))
        quote = call(get_quote, tickers=["VTI"])["quotes"]["VTI"]
        assert "check the symbol" in quote["error"]

    def test_unusable_range_fields_become_null_not_an_exception(self, monkeypatch):
        install(
            monkeypatch, lambda s: FakeTicker(s, fast_info={"lastPrice": 10.0, "yearHigh": None})
        )
        quote = call(get_quote, tickers=["VTI"])["quotes"]["VTI"]
        assert quote["year_high"] is None
        assert quote["price"] == 10.0


class TestFundProfile:
    def test_reports_the_raw_expense_ratio_with_its_scale_caveat(self, monkeypatch):
        """The payload key is `expense_ratio_raw`, not `expense_ratio`.

        yfinance reports the scale inconsistently, so the tool refuses to
        normalize it and labels the ambiguity instead. The name and the note
        travel together; a rename that drops either one leaves the model
        converting a fee to dollars on a guessed scale.
        """
        install(monkeypatch, FakeTicker)
        result = call(get_fund_profile, ticker="vti")
        assert result["expense_ratio_raw"] == 0.03
        assert result["expense_ratio_note"]
        assert result["ticker"] == "VTI"

    def test_falls_back_to_the_annual_report_ratio(self, monkeypatch):
        install(
            monkeypatch,
            lambda s: FakeTicker(s, info={"annualReportExpenseRatio": 0.0075}),
        )
        assert call(get_fund_profile, ticker="VTI")["expense_ratio_raw"] == 0.0075

    def test_missing_fields_are_null_rather_than_absent(self, monkeypatch):
        install(monkeypatch, lambda s: FakeTicker(s, info={}))
        result = call(get_fund_profile, ticker="VTI")
        assert result["expense_ratio_raw"] is None
        assert result["name"] is None

    def test_a_provider_failure_returns_an_error_envelope(self, monkeypatch):
        install(monkeypatch, lambda s: FakeTicker(s, raises=RuntimeError("upstream down")))
        assert "error" in call(get_fund_profile, ticker="VTI")


class TestHistoricalReturn:
    def test_annualizes_over_elapsed_time_not_bar_count(self, monkeypatch):
        """A price doubling over ~5 years is ~14.9%/yr regardless of bar count.

        The series here has 60 bars spanning five years. Annualizing by row
        count -- dividing by 252 trading days -- would report a wildly
        different CAGR, and that number is fed straight into `project_savings`
        as a multi-decade return assumption.
        """
        install(
            monkeypatch, lambda s: FakeTicker(s, history=price_history(100.0, 200.0, days=1826))
        )
        result = call(get_historical_return, ticker="VTI", years=5)
        assert result["years_covered"] == pytest.approx(5.0, abs=0.05)
        assert result["annualized_return"] == pytest.approx(0.1487, abs=0.005)
        assert result["total_return"] == pytest.approx(1.0, abs=0.01)

    def test_volatility_is_annualized_by_the_series_own_bar_rate(self, monkeypatch):
        """A geometric series has zero variance, so it cannot test this.

        The first version asserted `>= 0` against `price_history`, which rises
        at a perfectly constant rate -- its per-bar return std is ~1e-16, so the
        assertion held for any annualization factor, or for none at all.

        The factor is derived from the bars this series actually contains per
        year, the same basis the CAGR uses. A hardcoded sqrt(252) asserts daily
        US trading bars: right for the common case, wrong by ~14.5x on the
        annual bars below, in the direction that makes a portfolio look far more
        volatile than it is.
        """
        closes = [100.0, 110.0, 99.0, 115.0, 104.0, 121.0]
        frame = pd.DataFrame(
            {"Close": closes},
            index=pd.to_datetime(pd.date_range("2020-01-01", periods=len(closes), freq="365D")),
        )
        install(monkeypatch, lambda s: FakeTicker(s, history=frame))

        # Five annual bars: one return per year, so the annualization factor is 1.
        per_bar_std = float(frame["Close"].pct_change().dropna().std())
        result = call(get_historical_return, ticker="VTI")
        assert result["annualized_volatility"] == pytest.approx(per_bar_std, abs=5e-4)
        assert result["annualized_volatility"] < per_bar_std * 15  # what sqrt(252) would give

    def test_daily_bars_still_annualize_by_roughly_the_trading_year(self, monkeypatch):
        """The common case must not regress: ~252 bars a year is what the old
        hardcoded factor got right, and the derived factor has to agree."""
        import numpy as np

        rng = np.random.default_rng(0)
        n = 252 * 4
        index = pd.to_datetime(pd.date_range("2020-01-01", periods=n, freq="B"))
        closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
        frame = pd.DataFrame({"Close": closes}, index=index)
        install(monkeypatch, lambda s: FakeTicker(s, history=frame))

        per_bar_std = float(frame["Close"].pct_change().dropna().std())
        result = call(get_historical_return, ticker="VTI")
        assert result["annualized_volatility"] == pytest.approx(per_bar_std * 252**0.5, rel=0.02)

    def test_a_flat_series_reports_no_volatility(self, monkeypatch):
        flat = pd.DataFrame(
            {"Close": [100.0] * 5},
            index=pd.to_datetime(pd.date_range("2020-01-01", periods=5, freq="365D")),
        )
        install(monkeypatch, lambda s: FakeTicker(s, history=flat))
        result = call(get_historical_return, ticker="VTI")
        assert result["annualized_volatility"] == pytest.approx(0.0, abs=1e-9)
        assert result["annualized_return"] == pytest.approx(0.0, abs=1e-9)

    def test_the_forecast_caveat_is_always_returned(self, monkeypatch):
        install(
            monkeypatch, lambda s: FakeTicker(s, history=price_history(100.0, 150.0, days=1826))
        )
        result = call(get_historical_return, ticker="VTI")
        assert result["caveat"]
        assert "total return" in result["basis"]

    @pytest.mark.parametrize("years", [0, 26, -1])
    def test_the_lookback_window_is_bounded(self, monkeypatch, years):
        install(
            monkeypatch, lambda s: FakeTicker(s, history=price_history(100.0, 200.0, days=1826))
        )
        assert "error" in call(get_historical_return, ticker="VTI", years=years)

    def test_empty_history_is_reported_rather_than_dividing_by_zero(self, monkeypatch):
        install(monkeypatch, lambda s: FakeTicker(s, history=pd.DataFrame()))
        result = call(get_historical_return, ticker="ZZZZ")
        assert "no price history" in result["error"]

    def test_a_single_bar_is_not_enough_to_annualize(self, monkeypatch):
        one = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-01-01"]))
        install(monkeypatch, lambda s: FakeTicker(s, history=one))
        assert "error" in call(get_historical_return, ticker="VTI")
