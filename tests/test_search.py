"""Tests for the web search tool, with the Tavily client stubbed out.

Two behaviours here are load-bearing and neither was covered:

* **The missing-key path.** `TAVILY_API_KEY` is optional, so this runs for real
  users. What comes back is not a bare error but an instruction not to state
  contribution limits from memory -- the whole point being that a silent
  degradation to the model's stale prior is the harmful outcome, not the
  unavailable search.
* **The domain allowlist is opt-in.** `authoritative_only` defaults to False, so
  the .gov restriction is a model-chosen argument rather than an enforced
  boundary. Tests pin that both ways so the distinction stays visible.

`config` is patched by attribute rather than by value because search.py reads
`config.TAVILY_API_KEY` at call time, deliberately, so a credential change is
picked up without a restart.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from financial_planner import config
from financial_planner.tools.search import (
    AUTHORITATIVE_DOMAINS,
    MAX_RESULTS,
    SNIPPET_CHARS,
    search_web,
)

RAW = {
    "answer": "The 2026 elective deferral limit is $24,500.",
    "results": [
        {
            "title": "401(k) limits",
            "url": "https://www.irs.gov/retirement-plans/401k-limits",
            "content": "Long page text. " * 200,
        },
        {"title": "Second", "url": "https://www.ssa.gov/x", "content": "short"},
    ],
}


class FakeTavilyClient:
    """Records the kwargs it was called with; returns a canned payload."""

    calls: list[dict] = []
    payload: dict = RAW
    raises: Exception | None = None

    def __init__(self, api_key=None):
        self.api_key = api_key

    def search(self, **kwargs):
        FakeTavilyClient.calls.append(kwargs)
        if FakeTavilyClient.raises:
            raise FakeTavilyClient.raises
        return FakeTavilyClient.payload


@pytest.fixture
def tavily(monkeypatch):
    """Install the fake client and a key, and hand back the call recorder.

    search.py imports TavilyClient inside the function body, so the stub goes
    onto the `tavily` module in sys.modules rather than onto search.py.
    """
    FakeTavilyClient.calls = []
    FakeTavilyClient.payload = RAW
    FakeTavilyClient.raises = None
    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=FakeTavilyClient))
    monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-test-key")
    return FakeTavilyClient


def call(**kwargs) -> dict:
    return json.loads(search_web.invoke(kwargs))


class TestMissingKey:
    def test_degrades_with_an_instruction_not_a_bare_error(self, monkeypatch):
        """The failure mode being guarded is the model filling the gap itself."""
        monkeypatch.setattr(config, "TAVILY_API_KEY", None)
        message = call(query="2026 401k contribution limit")["error"]
        assert "TAVILY_API_KEY" in message
        assert "from memory" in message

    def test_an_empty_string_key_counts_as_missing(self, monkeypatch):
        """`.env` files supply empty values as readily as absent ones."""
        monkeypatch.setattr(config, "TAVILY_API_KEY", "")
        assert "error" in call(query="anything")

    def test_no_client_is_constructed_without_a_key(self, monkeypatch):
        FakeTavilyClient.calls = []
        monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=FakeTavilyClient))
        monkeypatch.setattr(config, "TAVILY_API_KEY", None)
        call(query="anything")
        assert FakeTavilyClient.calls == []


class TestDomainAllowlist:
    def test_the_allowlist_is_opt_in_not_enforced(self, tavily):
        """Default is open-web search; the .gov restriction is model-chosen.

        The system prompt asks for authoritative_only on limits and brackets,
        but nothing in code requires it. Pinned so the gap is not mistaken for
        a security boundary.
        """
        call(query="best high yield savings account")
        assert "include_domains" not in tavily.calls[0]

    def test_authoritative_only_pins_the_government_domains(self, tavily):
        call(query="2026 401k contribution limit", authoritative_only=True)
        assert tavily.calls[0]["include_domains"] == AUTHORITATIVE_DOMAINS

    def test_the_allowlist_holds_only_government_sources(self):
        assert all(d.endswith(".gov") for d in AUTHORITATIVE_DOMAINS)


class TestResultShaping:
    def test_the_answer_and_urls_survive(self, tavily):
        result = call(query="2026 401k contribution limit")
        assert result["answer"] == RAW["answer"]
        assert result["results"][0]["url"].startswith("https://www.irs.gov/")

    def test_snippets_are_trimmed(self, tavily):
        """Tavily returns full page text; untrimmed it crowds out the session."""
        result = call(query="anything")
        assert len(result["results"][0]["snippet"]) == SNIPPET_CHARS

    def test_the_result_count_is_capped(self, tavily):
        tavily.payload = {
            "answer": None,
            "results": [
                {"title": f"r{i}", "url": f"https://x/{i}", "content": "c"} for i in range(20)
            ],
        }
        assert len(call(query="anything")["results"]) == MAX_RESULTS

    def test_advanced_depth_and_answer_are_requested(self, tavily):
        call(query="anything")
        assert tavily.calls[0]["search_depth"] == "advanced"
        assert tavily.calls[0]["include_answer"] is True

    def test_a_missing_results_key_does_not_raise(self, tavily):
        tavily.payload = {"answer": "just an answer"}
        assert call(query="anything")["results"] == []

    def test_a_null_content_field_becomes_an_empty_snippet(self, tavily):
        tavily.payload = {"answer": None, "results": [{"title": "t", "url": "u", "content": None}]}
        assert call(query="anything")["results"][0]["snippet"] == ""


class TestUpstreamFailure:
    def test_a_client_exception_returns_an_error_envelope(self, tavily):
        tavily.raises = RuntimeError("rate limited")
        result = call(query="anything")
        assert "RuntimeError" in result["error"]
        assert "rate limited" in result["error"]

    def test_the_key_is_never_echoed_into_the_payload(self, tavily):
        """The reply reaches the model and the transcript; the key must not."""
        tavily.raises = RuntimeError("auth failed for tvly-test-key")
        assert "tvly-test-key" not in json.dumps(call(query="anything"))
