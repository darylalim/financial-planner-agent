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


class TestSecretsAreDiscoveredNotListed:
    """Regression: redaction scrubbed a hardcoded two-name tuple.

    A third credential added to config.py leaked until someone remembered to
    extend that tuple -- a maintenance step with no failing test behind it.
    Secrets are now recognised by the shape of their name on `config`.
    """

    @pytest.mark.parametrize(
        "name", ["OPENAI_API_KEY", "FRED_API_KEY", "PLAID_TOKEN", "WEBHOOK_SECRET"]
    )
    def test_a_newly_added_credential_is_redacted(self, monkeypatch, name):
        monkeypatch.setattr(config, name, "brand-new-credential-value", raising=False)
        assert "***" in envelope.redact(f"401 for brand-new-credential-value via {name}")

    def test_a_short_new_credential_is_still_ignored(self, monkeypatch):
        """The eight-character floor holds for discovered names too: a short
        value would replace between every character of the message."""
        monkeypatch.setattr(config, "FRED_API_KEY", "abc", raising=False)
        assert envelope.redact("abc the quick brown fox") == "abc the quick brown fox"

    def test_a_non_string_attribute_is_not_mistaken_for_a_secret(self, monkeypatch):
        """`config` holds Paths and None alongside the keys; neither has a
        length to floor or a value to replace."""
        monkeypatch.setattr(config, "CACHE_TOKEN", None, raising=False)
        monkeypatch.setattr(config, "LEDGER_SECRET", 12345678, raising=False)
        assert envelope.redact("nothing to strip") == "nothing to strip"

    @pytest.mark.parametrize("name", ["PARTITION_KEY", "GROUP_KEY", "SORT_KEY"])
    def test_an_ordinary_constant_ending_in_key_is_not_treated_as_a_secret(self, monkeypatch, name):
        """Discovery is only worth having while its false positives are impossible.

        A bare `_KEY` suffix names an ordinary constant as readily as a
        credential. With it in the tuple, adding `PARTITION_KEY =
        "transaction_date"` to config.py made `redact` strip that word out of
        every tool result that mentioned it -- a spending breakdown's category
        labels, a document's schema listing, an extracted PDF page -- with no
        error raised and no log line written. The model read mangled data and
        nothing anywhere said so.
        """
        monkeypatch.setattr(config, name, "transaction_date", raising=False)
        assert envelope.redact("grouped by transaction_date") == "grouped by transaction_date"
        assert "transaction_date" in envelope.ok({"group_by": "transaction_date"})

    def test_a_secret_containing_another_is_replaced_whole(self, monkeypatch):
        """Longest first, so the longer key is not left as a redacted stub."""
        monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-shared-prefix")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "tvly-shared-prefix-and-more")
        assert envelope.redact("auth failed for tvly-shared-prefix-and-more") == (
            "auth failed for ***"
        )


class TestSuccessIsRedactedToo:
    """Regression: only `err` redacted, so the guarantee the README makes --
    redaction over everything a tool returns -- was false on the success path.
    Search snippets and other relayed upstream text ride back through `ok`.
    """

    def test_ok_strips_a_configured_key(self, keys):
        payload = {"snippet": f"curl -H 'Authorization: {TAVILY}' https://api.tavily.com"}
        assert TAVILY not in envelope.ok(payload)

    def test_ok_strips_a_key_nested_in_the_payload(self, keys):
        assert ANTHROPIC not in envelope.ok({"results": [{"body": f"key={ANTHROPIC}"}]})

    def test_the_rest_of_the_payload_survives_redaction(self, keys):
        payload = json.loads(envelope.ok({"total_outflow": 1200.0, "note": f"saw {TAVILY}"}))
        assert payload["total_outflow"] == 1200.0
        assert payload["note"] == "saw ***"

    @pytest.mark.parametrize(
        "secret",
        ["tvly-caf\u00e9-NOTAREALKEY", 'tvly-"quoted"-NOTAREALKEY', "tvly-back\\slash-NOTAREAL"],
    )
    def test_ok_strips_a_key_json_would_escape(self, monkeypatch, secret):
        """Regression: `ok` redacted the *serialized* text while `err` redacted
        the raw message, so any key holding a character `json.dumps` escapes --
        non-ASCII under ensure_ascii, a quote, a backslash -- survived on the
        success path and failed to survive on the error path. The tests above
        all use ASCII-safe keys, which is why the asymmetry went unseen.
        """
        monkeypatch.setattr(config, "TAVILY_API_KEY", secret)
        payload = json.loads(envelope.ok({"snippet": f"echoed {secret} back"}))
        assert payload["snippet"] == "echoed *** back"

    def test_redacting_ok_does_not_disturb_the_serialization_contract(self, keys):
        """Compact separators and `default=str` are what `_is_error_result`
        and the pandas-carrying tools depend on; neither redaction pass -- the
        one over the payload's strings nor the backstop over the serialized
        text -- touches the keys or the separators."""
        import pandas as pd

        serialized = envelope.ok({"month": pd.Period("2026-01"), "n": 1})
        assert serialized == '{"month":"2026-01","n":1}'
        assert not _is_error_result(serialized)


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
