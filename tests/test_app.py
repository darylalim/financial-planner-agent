"""Smoke tests for the Streamlit app.

`AppTest` actually executes ``app.py`` in-process, so these catch import errors,
bad widget signatures, and exceptions on the initial render -- none of which a
plain HTTP check would surface, because Streamlit does not run the script until
a session connects.

No model is invoked: the agent is only built when a message is submitted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def with_api_key(monkeypatch):
    """Pretend a key is configured so the app renders past its startup guard."""
    monkeypatch.setattr("financial_planner.config.ANTHROPIC_API_KEY", "sk-ant-test", raising=False)


def _run() -> AppTest:
    app = AppTest.from_file(APP_PATH, default_timeout=60)
    app.run()
    return app


class TestInitialRender:
    def test_renders_without_exception(self, with_api_key):
        app = _run()
        assert not app.exception, [e.value for e in app.exception]

    def test_shows_title(self, with_api_key):
        assert "Financial planner" in [t.value for t in _run().title]

    def test_offers_a_chat_input(self, with_api_key):
        assert len(_run().chat_input) > 0

    def test_sidebar_has_new_conversation_button(self, with_api_key):
        assert "New conversation" in [b.label for b in _run().sidebar.button]

    def test_starts_with_an_empty_transcript(self, with_api_key):
        assert _run().session_state["messages"] == []

    def test_assigns_a_thread_id(self, with_api_key):
        assert _run().session_state["thread_id"].startswith("session-")

    def test_shows_suggestion_chips_on_an_empty_conversation(self, with_api_key):
        app = _run()
        assert len(app.pills) == 1


class TestStartupGuard:
    def test_missing_api_key_shows_an_error_instead_of_crashing(self, monkeypatch):
        """A missing key must be an actionable message, not a stack trace."""
        monkeypatch.setattr("financial_planner.config.ANTHROPIC_API_KEY", None, raising=False)
        app = _run()
        assert not app.exception
        assert any("ANTHROPIC_API_KEY" in e.value for e in app.error)

    def test_missing_api_key_halts_before_the_chat_input(self, monkeypatch):
        """st.stop() must prevent a chat box that cannot possibly work."""
        monkeypatch.setattr("financial_planner.config.ANTHROPIC_API_KEY", None, raising=False)
        assert len(_run().chat_input) == 0
