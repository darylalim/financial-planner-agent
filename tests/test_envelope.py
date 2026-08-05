"""Tests for the shared tool-result envelope and its redaction.

Two contracts meet here. `streaming._is_error_result` decides whether the UI
reports a call as failed and does so by matching the *serialized* text, so the
separators and key order are behaviour rather than formatting. And whatever
`err` returns reaches the model's context and the saved transcript, so a secret
quoted back by an upstream client has to be stripped before it gets there.

Redaction lives here rather than at one call site because the messages that
carry a leaked key are relayed from upstream, and upstream reaches this process
through whichever tool happens to be calling out.
"""

from __future__ import annotations

import json

import pytest

from financial_planner import config, envelope
from financial_planner.streaming import _is_error_result
from financial_planner.tools import calculators, documents, market, search

ANTHROPIC = "sk-ant-api03-NOTAREALKEY0123456789"
TAVILY = "tvly-NOTAREALKEY0123456789"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", ANTHROPIC)
    monkeypatch.setattr(config, "TAVILY_API_KEY", TAVILY)


class TestErrorEnvelopeContract:
    def test_the_ui_detects_what_err_produces(self):
        """Pinned against the real consumer, not a copy of its expected input."""
        assert _is_error_result(envelope.err(ValueError("bad column")))

    def test_a_success_payload_is_not_mistaken_for_an_error(self):
        assert not _is_error_result(envelope.ok({"total_outflow": 1200.0}))

    def test_the_exception_type_is_kept(self):
        """It is often all that separates "wrong column" from "unreadable file",
        and the model picks a different recovery for each."""
        assert "ValueError" in json.loads(envelope.err(ValueError("nope")))["error"]

    def test_a_plain_string_problem_is_accepted(self):
        """Not every failure has an exception -- the missing-key path has none."""
        assert json.loads(envelope.err("no key configured"))["error"] == "no key configured"

    def test_ok_serializes_values_json_does_not_know(self):
        """pandas Periods and numpy scalars reach this from the document tools."""
        import pandas as pd

        payload = json.loads(envelope.ok({"month": pd.Period("2026-01"), "n": pd.NA}))
        assert payload["month"] == "2026-01"


class TestRedaction:
    @pytest.mark.parametrize("secret", [ANTHROPIC, TAVILY])
    def test_a_configured_key_is_stripped(self, keys, secret):
        assert secret not in envelope.redact(f"401 unauthorized for {secret}")

    def test_the_surrounding_message_survives(self, keys):
        """The model still has to be able to act on what went wrong."""
        redacted = envelope.redact(f"AuthError: bad key {TAVILY} on /search")
        assert redacted.startswith("AuthError: bad key ")
        assert redacted.endswith(" on /search")

    def test_redaction_reaches_the_error_envelope(self, keys):
        assert TAVILY not in envelope.err(RuntimeError(f"auth failed for {TAVILY}"))

    def test_an_unset_key_does_not_break_the_message(self, monkeypatch):
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
        monkeypatch.setattr(config, "TAVILY_API_KEY", None)
        assert envelope.redact("plain message") == "plain message"

    @pytest.mark.parametrize("short", ["", "a", "abc", "1234567"])
    def test_a_short_value_is_ignored_rather_than_shredding_the_text(self, monkeypatch, short):
        """An empty or one-character secret would otherwise replace between
        every character of the message."""
        monkeypatch.setattr(config, "TAVILY_API_KEY", short)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
        assert envelope.redact("abc1234567 the quick brown fox") == "abc1234567 the quick brown fox"

    def test_keys_are_read_at_call_time_not_import_time(self, monkeypatch):
        """config reads the environment once at import and the app rebinds it
        afterwards; a by-value capture here would redact a stale key."""
        monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-rotated-key-value")
        assert "***" in envelope.redact("failed with tvly-rotated-key-value")


class TestEveryToolModuleSharesTheEnvelope:
    """Four copies of these helpers had already drifted -- one omitted the
    exception type, and only one redacted. Import identity is the cheapest way
    to keep a fifth from appearing.
    """

    @pytest.mark.parametrize("module", [calculators, documents, market, search])
    def test_the_module_uses_the_shared_err(self, module):
        assert module.err is envelope.err

    @pytest.mark.parametrize("module", [calculators, documents, market, search])
    def test_the_module_uses_the_shared_ok(self, module):
        assert module.ok is envelope.ok

    def test_a_tool_failure_carries_no_secret(self, keys, monkeypatch):
        """End to end through a real tool, not just the helper."""
        monkeypatch.setattr(
            market, "yf", type("X", (), {"Ticker": staticmethod(lambda s: 1 / 0)})()
        )

        def boom(_):
            raise RuntimeError(f"upstream rejected {ANTHROPIC}")

        monkeypatch.setattr(market, "_clean", boom)
        assert ANTHROPIC not in market.get_quote.invoke({"tickers": ["VTI"]})
