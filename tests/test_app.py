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


@pytest.fixture
def offline_turn(monkeypatch):
    """Let a full turn run with no model, no network and no checkpoint file."""
    from financial_planner import agent as agent_module
    from financial_planner import streaming as streaming_module

    monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
    monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)
    monkeypatch.setattr(
        streaming_module,
        "stream_agent_events",
        lambda _agent, _messages, _config: iter([streaming_module.Token("Projection ready.")]),
    )


def _raw_options(app: AppTest) -> list[str]:
    """Reconstruct the option values a real click sends.

    ``pills.options`` reports the display label; Streamlit splits the
    ``:material/x:`` prefix into a separate ``content_icon`` field. The value
    the widget actually returns is the original prefixed string, and passing
    the stripped label instead silently selects nothing -- which is how an
    earlier version of this test missed a hang.
    """
    return [f"{o.content_icon} {o.content}" for o in app.pills[0].proto.options]


class TestSuggestionChips:
    """Regression: clicking a chip used to hang the app forever.

    The prompt was stashed in session_state followed by st.rerun(), but the
    rerun aborted the script before the stash was read, and the pills widget
    still held the selection on the next run -- so it stashed and reran again,
    indefinitely. AppTest surfaces it as a script timeout.
    """

    def test_clicking_a_chip_completes_without_hanging(self, with_api_key, offline_turn):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert not app.exception, [e.value for e in app.exception]

    def test_clicking_a_chip_actually_sends_its_prompt(self, with_api_key, offline_turn):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        roles = [m["role"] for m in app.session_state["messages"]]
        assert roles == ["user", "assistant"]
        assert "retirement readiness" in app.session_state["messages"][0]["content"]

    def test_chips_disappear_once_the_conversation_starts(self, with_api_key, offline_turn):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert len(app.pills) == 0


class TestThreadIdentity:
    def test_new_conversation_always_mints_a_distinct_thread(self, with_api_key):
        """Two clicks in the same second must not resume the old thread.

        The transcript lives in session_state and is cleared either way, so a
        reused id looks like a fresh chat while the checkpointer silently hands
        the agent the entire previous conversation back.
        """
        app = _run()
        first = app.session_state["thread_id"]
        app.sidebar.button[0].click().run()
        assert app.session_state["thread_id"] != first


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
