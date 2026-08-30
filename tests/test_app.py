"""Smoke tests for the Streamlit app.

`AppTest` actually executes ``app.py`` in-process, so these catch import errors,
bad widget signatures, and exceptions on the initial render -- none of which a
plain HTTP check would surface, because Streamlit does not run the script until
a session connects.

No model is invoked: the agent is only built when a message is submitted.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


@pytest.fixture
def failing_turn(monkeypatch):
    """Let a turn start and then blow up the way a dropped connection would."""
    from financial_planner import agent as agent_module
    from financial_planner import streaming as streaming_module

    monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
    monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)

    def _explode(_agent, _messages, _config):
        raise RuntimeError("upstream refused a quote for $2,000 of $VOO")

    monkeypatch.setattr(streaming_module, "stream_agent_events", _explode)


class _FakeUpload:
    """Stands in for Streamlit's UploadedFile (only .name and .getvalue used)."""

    def __init__(self, name: str, data: bytes = b"col\n1\n") -> None:
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


@pytest.fixture
def attach(monkeypatch, tmp_path):
    """Submit the chat input with files attached, into a throwaway workspace.

    AppTest can set the chat input's text but has no way to attach files: the
    real widget resolves them through the server's upload manager, which AppTest
    does not run. Replacing ``st.chat_input`` for a single call is the smallest
    stand-in, and returning None afterwards matches the real widget -- a
    submission is consumed once, so the post-turn rerun must not resubmit it.
    """
    import streamlit as st_module

    from financial_planner import config as config_module

    monkeypatch.setattr(config_module, "WORKSPACE_DIR", tmp_path)

    def _attach(*names: str, text: str = "") -> None:
        pending = [SimpleNamespace(text=text, files=[_FakeUpload(n) for n in names])]

        def fake_chat_input(*_args, **_kwargs):
            return pending.pop() if pending else None

        monkeypatch.setattr(st_module, "chat_input", fake_chat_input)

    return _attach


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


class TestUploadsThatCannotBeSaved:
    """Regression: an attachment whose name has no usable final component was
    dropped with nothing said, so the user watched their file vanish.
    """

    def test_a_skipped_upload_is_named_in_a_warning(self, with_api_key, attach):
        attach("..")
        app = _run()
        assert not app.exception, [e.value for e in app.exception]
        assert any(".." in w.value for w in app.warning)

    def test_a_usable_attachment_is_still_saved_alongside_it(
        self, with_api_key, offline_turn, attach, tmp_path
    ):
        """And the warning survives the redraw the finished turn triggers."""
        attach("..", "statement.csv")
        app = _run()
        assert (tmp_path / "statement.csv").exists()
        assert any(".." in w.value for w in app.warning)


class TestFailedTurns:
    """Regression: a turn that raised left a user message with no reply, and the
    st.error vanished on the next rerun, so the transcript read as if the agent
    had ignored the question.
    """

    def test_a_failure_is_recorded_in_the_transcript(self, with_api_key, failing_turn):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert [m["role"] for m in app.session_state["messages"]] == ["user", "assistant"]
        assert "RuntimeError" in app.session_state["messages"][-1]["content"]

    def test_the_error_is_escaped_before_it_reaches_markdown(self, with_api_key, failing_turn):
        """st.error renders markdown, so "$2,000 of $VOO" would LaTeX-ify."""
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert any("\\$2,000" in e.value for e in app.error)


class TestStreamedAnswerIsComplete:
    def test_every_token_survives_the_render_throttle(self, with_api_key, monkeypatch):
        """The paint is throttled, so the final flush must still be exact.

        The last tokens of an answer are usually below the redraw threshold, and
        an accumulator that forgot them would silently truncate the reply.
        """
        from financial_planner import agent as agent_module
        from financial_planner import streaming as streaming_module

        pieces = ["A ", "sh", "ort ", "answer", "."]
        monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
        monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)
        monkeypatch.setattr(
            streaming_module,
            "stream_agent_events",
            lambda *_: iter([streaming_module.Token(p) for p in pieces]),
        )

        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert app.session_state["messages"][-1]["content"] == "".join(pieces)


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
