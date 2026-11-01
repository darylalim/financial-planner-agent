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


@pytest.fixture(autouse=True)
def clear_streamlit_resource_cache():
    """`app.load_agent` is `@st.cache_resource`, and that cache is process-global.

    `AppTest` re-executes app.py per run, but the resource cache survives both
    the rerun and the test: whichever test first submits a turn populates it,
    and every later fixture's `build_agent` stand-in is then never called. It is
    harmless while all of them assert on `stream_agent_events` -- patched at
    module level and re-imported each run -- but the fixtures read as though
    they control agent construction, so the first test that needs a distinct
    agent would silently get the previous test's and pass for the wrong reason.

    Autouse rather than named in each fixture, so a test added later inherits it.
    """
    import streamlit as st_module

    st_module.cache_resource.clear()
    yield
    st_module.cache_resource.clear()


@pytest.fixture
def with_api_key(monkeypatch):
    """Pretend a key is configured so the app renders past its startup guard."""
    monkeypatch.setattr("financial_planner.config.ANTHROPIC_API_KEY", "sk-ant-test", raising=False)


def _run() -> AppTest:
    app = AppTest.from_file(APP_PATH, default_timeout=60)
    app.run()
    return app


def _button(app: AppTest, label: str):
    """Find a button by its label. By index, a new button anywhere above it
    would silently re-point the test at a different control."""
    return next(b for b in app.button if b.label == label)


class TestInitialRender:
    def test_renders_without_exception(self, with_api_key):
        app = _run()
        assert not app.exception, [e.value for e in app.exception]

    def test_shows_title(self, with_api_key):
        assert "Financial planner" in [t.value for t in _run().title]

    def test_offers_a_chat_input(self, with_api_key):
        assert len(_run().chat_input) > 0

    def test_new_conversation_is_in_the_main_area_not_the_sidebar(self, with_api_key):
        """A narrow window starts with the sidebar collapsed, so a control that
        lives only there is hidden behind an arrow. One control, one place."""
        app = _run()
        assert "New conversation" in [b.label for b in app.main.button]
        assert "New conversation" not in [b.label for b in app.sidebar.button]

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


@pytest.fixture
def failing_tool_turn(monkeypatch):
    """A turn that calls a tool, has it report an error, and then fails.

    It has to fail. A completed turn ends in st.rerun(), and the st.status the
    tool lines are written into goes with the page that held it, so a turn that
    succeeds leaves nothing to assert against.
    """
    from financial_planner import agent as agent_module
    from financial_planner import streaming as streaming_module

    monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
    monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)

    def _call_a_tool(_agent, _messages, _config):
        yield streaming_module.ToolStart("summarize_spending")
        yield streaming_module.ToolEnd("summarize_spending", ok=False)
        raise RuntimeError("upstream refused a quote for $2,000 of $VOO")

    monkeypatch.setattr(streaming_module, "stream_agent_events", _call_a_tool)


@pytest.fixture
def tool_turn(monkeypatch):
    """A tool reports an error and the agent answers anyway, so the turn ends in
    the st.rerun() that used to take the only record of the failure with it.
    """
    from financial_planner import agent as agent_module
    from financial_planner import streaming as streaming_module

    monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
    monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)

    def _call_a_tool(_agent, _messages, _config):
        yield streaming_module.ToolStart("summarize_spending")
        yield streaming_module.ToolEnd("summarize_spending", ok=False)
        yield streaming_module.Token("I could not read that file.")

    monkeypatch.setattr(streaming_module, "stream_agent_events", _call_a_tool)


@pytest.fixture
def turn_that_fails_mid_answer(monkeypatch):
    """Text reaches the screen, and then the connection drops."""
    from financial_planner import agent as agent_module
    from financial_planner import streaming as streaming_module

    monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
    monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)

    def _drop(_agent, _messages, _config):
        yield streaming_module.Token("Your projected balance is $1.2M and the shortfall ")
        raise RuntimeError("upstream refused a quote for $2,000 of $VOO")

    monkeypatch.setattr(streaming_module, "stream_agent_events", _drop)


@pytest.fixture
def turn_interrupted_by_a_rerun(monkeypatch):
    """A rerun request delivered mid-stream, exactly as a click on a widget that
    stayed live, the Stop button or runOnSave delivers one: as a
    ScriptControlException, which subclasses BaseException so that no
    ``except Exception`` in the turn can see it.
    """
    import streamlit as st_module

    from financial_planner import agent as agent_module
    from financial_planner import streaming as streaming_module

    monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
    monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)

    def _interrupt(_agent, _messages, _config):
        yield streaming_module.Token("Working on it")
        st_module.rerun()

    monkeypatch.setattr(streaming_module, "stream_agent_events", _interrupt)


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


class TestDocumentListing:
    """Regression: the sidebar rendered workspace filenames straight into
    st.caption, which renders markdown. The name is browser-supplied and
    `uploads.destination_for` strips only the directory component from it, never
    a markdown metacharacter -- so a backtick opened a code span that swallowed
    every filename listed after it. The two other places a name or a raw string
    is shown already escape it.
    """

    def test_a_filename_is_escaped_before_it_reaches_the_sidebar(
        self, with_api_key, monkeypatch, tmp_path
    ):
        from financial_planner import config as config_module
        from financial_planner.rendering import escape_markdown

        monkeypatch.setattr(config_module, "WORKSPACE_DIR", tmp_path)
        name = "a`b $200 statement.csv"
        (tmp_path / name).write_bytes(b"col\n1\n")

        captions = [c.value for c in _run().caption]
        # Both directions. Asserting only the escaped form would still pass if
        # escape_markdown were ever reduced to the identity function.
        assert any(escape_markdown(name) in c for c in captions), captions
        assert not any(name in c for c in captions), captions


def _app_constant(name: str):
    """A module-level constant of app.py, read without running the script."""
    import ast

    tree = ast.parse(Path(APP_PATH).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise LookupError(name)


class TestTheEmptyConversation:
    """What the sidebar holds has a copy in the main area, because a narrow
    window starts with the sidebar collapsed. Shown only before the first
    message, the same condition the suggestion chips use."""

    @pytest.fixture
    def workspace(self, monkeypatch, tmp_path):
        from financial_planner import config as config_module

        monkeypatch.setattr(config_module, "WORKSPACE_DIR", tmp_path)
        return tmp_path

    def test_it_counts_the_documents_on_file(self, with_api_key, workspace):
        for name in ("a.csv", "b.pdf"):
            (workspace / name).write_bytes(b"col\n1\n")
        captions = [c.value for c in _run().main.caption]
        assert any("2 documents on file, listed in the sidebar" in c for c in captions), captions

    def test_with_nothing_on_file_it_points_at_the_upload_button(self, with_api_key, workspace):
        captions = [c.value for c in _run().main.caption]
        # The chat box draws its upload button as a "+" on 1.66, not a paperclip.
        assert any(":material/add: in the chat box" in c for c in captions), captions

    def test_it_never_names_a_file(self, with_api_key, workspace):
        """Only a count, so there is nothing to escape. Naming one would need
        escape_markdown, as the sidebar does."""
        (workspace / "household.csv").write_bytes(b"col\n1\n")
        assert not any("household" in c.value for c in _run().main.caption)

    def test_the_disclaimer_is_in_the_main_area_and_the_sidebar(self, with_api_key, workspace):
        disclaimer = _app_constant("DISCLAIMER")
        app = _run()
        assert any(disclaimer in c.value for c in app.main.caption)
        assert any(disclaimer in c.value for c in app.sidebar.caption)

    def test_it_is_gone_once_the_conversation_starts(self, with_api_key, workspace, offline_turn):
        disclaimer = _app_constant("DISCLAIMER")
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert not any(disclaimer in c.value for c in app.main.caption)
        assert any(disclaimer in c.value for c in app.sidebar.caption)

    def test_the_app_menu_carries_the_same_disclaimer(self):
        """The menu sits top-right at every width. AppTest does not expose page
        config, so this reads the call: About must be the constant itself, not
        a copy of its text that could drift."""
        import ast

        tree = ast.parse(Path(APP_PATH).read_text())
        call = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "set_page_config"
        )
        menu = next(k.value for k in call.keywords if k.arg == "menu_items")
        assert isinstance(menu, ast.Dict)
        about = menu.values[[ast.literal_eval(k) for k in menu.keys].index("About")]
        assert isinstance(about, ast.Name) and about.id == "DISCLAIMER"


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

    def test_the_transcript_copy_is_escaped_too(self, with_api_key, failing_turn):
        """Regression: only the st.error copy was escaped, and it is the one
        that does not last. The transcript copy is redrawn on every later run,
        through escape_dollars, which leaves every other metacharacter live.
        """
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert "\\$2,000" in app.session_state["messages"][-1]["content"]


class TestChipsCannotInterruptATurn:
    def test_the_chips_are_unmounted_before_the_agent_streams(self, with_api_key, failing_turn):
        """They are the one input left live during a turn -- st.chat_input has
        submit_mode="disable" -- and a click on one is a rerun request honoured
        inside the streaming loop, throwing the turn and its spend away.

        The failing fixture is the one that can see it: a turn that completes
        ends in st.rerun(), and the chips are then absent because the transcript
        is no longer empty, whether or not the turn ever unmounted them.
        """
        app = _run()
        assert len(app.pills) == 1
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert len(app.pills) == 0


class TestToolActivity:
    """The tool branch of the stream loop. Every other fixture yields nothing
    but Tokens, so TOOL_LABELS and the tool-failure notice -- the only st.*
    calls in app.py -- went unexecuted by the whole suite.
    """

    def test_a_tool_is_announced_by_its_label_not_its_identifier(
        self, with_api_key, failing_tool_turn
    ):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert app.status[0].markdown[0].value == ":material/play_arrow: Summarizing spending"

    def test_a_tool_that_reports_an_error_says_so(self, with_api_key, failing_tool_turn):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert "returned an error" in app.status[0].markdown[1].value

    def test_a_tool_failure_outlives_the_post_turn_rerun(self, with_api_key, tool_turn):
        """Regression: it was written only into an st.status, which is collapsed
        by default and is destroyed by the rerun that ends a finished turn. The
        one signal that a tool did not do what the answer implies it did was
        readable for a few hundred milliseconds, behind a click.
        """
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert app.session_state["messages"][-1]["tool_errors"] == ["summarize_spending"]
        assert any("returned an error" in c.value for c in app.caption)


class TestATurnAlwaysLeavesAReply:
    """Regression: the assistant entry was appended once an answer existed, so
    every early exit left the user's question in the transcript with nothing
    under it -- which redraws as an agent that was asked and ignored it.
    """

    def test_text_already_on_screen_survives_a_failure(
        self, with_api_key, turn_that_fails_mid_answer
    ):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        content = app.session_state["messages"][-1]["content"]
        assert "Your projected balance is $1.2M" in content
        assert "RuntimeError" in content

    def test_an_interrupted_turn_still_records_a_reply(
        self, with_api_key, turn_interrupted_by_a_rerun
    ):
        """A rerun mid-stream is a BaseException, so the turn's own except never
        runs. Only a slot claimed before the first token survives it.
        """
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        assert [m["role"] for m in app.session_state["messages"]] == ["user", "assistant"]
        assert "interrupted" in app.session_state["messages"][-1]["content"]


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
        _button(app, "New conversation").click().run()
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


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Run turns offline and record the text each one sent the agent.

    The workspace is a throwaway one holding a single statement, so a listing
    has something to name.
    """
    from financial_planner import agent as agent_module
    from financial_planner import config as config_module
    from financial_planner import streaming as streaming_module

    monkeypatch.setattr(config_module, "WORKSPACE_DIR", tmp_path)
    (tmp_path / "statement-sept.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(agent_module, "build_agent", lambda **_: object())
    monkeypatch.setattr(agent_module, "build_checkpointer", lambda: None)
    texts: list[str] = []

    def _record(_agent, messages, _config):
        texts.append(messages[-1]["content"])
        return iter([streaming_module.Token("Done.")])

    monkeypatch.setattr(streaming_module, "stream_agent_events", _record)
    return texts


class TestTheAgentIsToldWhatIsOnFile:
    """The agent's filesystem denies every read of a PDF, ls and glob included,
    so a statement uploaded in an earlier conversation is invisible to it. The
    app names what is on file instead: at a conversation's start, and whenever
    something is uploaded -- never on other turns, which would only repeat it.
    """

    def test_the_first_message_carries_the_listing(self, with_api_key, sent):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        [text] = sent
        assert "Files on file in /workspace/" in text
        assert '"/workspace/statement-sept.pdf"' in text

    def test_a_later_message_without_an_upload_does_not(self, with_api_key, sent):
        app = _run()
        app.pills[0].set_value(_raw_options(app)[0]).run()
        app.chat_input[0].set_value("And next month?").run()
        assert len(sent) == 2
        assert sent[1] == "And next month?"

    def test_a_later_message_with_an_upload_does(self, with_api_key, sent, attach):
        app = _run()
        app.session_state["messages"] = [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ]
        attach("checking.csv", text="Here is checking too")
        app.run()
        [text] = sent
        assert text.startswith("Here is checking too\n\nFiles on file")
        assert '"/workspace/checking.csv" (uploaded with this message)' in text
        # The earlier statement is listed too, and not as this turn's upload.
        assert text.endswith('\n- "/workspace/statement-sept.pdf"')

    def test_the_listing_is_not_shown_in_the_users_bubble(self, with_api_key, sent, attach):
        """It is for the agent. The bubble keeps what the user typed, and the
        attached names go in an escaped caption, as the sidebar's do."""
        attach("a`b.csv", text="See attached")
        app = _run()
        user = app.session_state["messages"][0]
        assert user["content"] == "See attached"
        assert user["attachments"] == ["a`b.csv"]
        assert "Files on file" not in "".join(m.value for m in app.markdown)


CONFIG_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml"

# WCAG 2.x minimums: 4.5 for body-size text, 3.0 for non-text UI (focus rings, borders).
_AA_TEXT = 4.5
_AA_NON_TEXT = 3.0
_STATUS_HUES = ("red", "orange", "yellow", "green", "blue")


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    lighter, darker = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.fixture(scope="module")
def config() -> dict:
    import tomllib

    return tomllib.loads(CONFIG_PATH.read_text())


class TestStreamlitConfig:
    """`.streamlit/config.toml` is half of the security boundary and all of the theme.

    Deleting or "tidying" the file breaks nothing visible -- the app still starts, on
    every interface, with a Network URL -- so the server latch is pinned here. The
    theme checks keep a colour tweak from quietly dropping a pair below WCAG AA.
    """

    def test_server_binds_loopback_only(self, config):
        assert config["server"]["address"] == "127.0.0.1"

    def test_allowed_hosts_is_pinned_not_empty(self, config):
        # An empty list accepts any Host header (DNS rebinding onto loopback).
        assert config["server"]["allowedHosts"] == ["localhost", "127.0.0.1"]

    def test_both_modes_are_defined_so_the_viewer_can_switch(self, config):
        assert {"light", "dark"} <= config["theme"].keys()

    def test_no_web_fonts(self, config):
        # A remote font URL tells its host every time the app is opened.
        theme = config["theme"]
        for key in ("font", "codeFont"):
            assert "://" not in theme.get(key, "")
        for face in theme.get("fontFaces", []):
            assert "://" not in face.get("url", "")

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_text_meets_aa(self, config, mode):
        theme = config["theme"][mode]
        sidebar = theme.get("sidebar", {})
        surfaces = {
            "main": theme["backgroundColor"],
            "secondary": theme["secondaryBackgroundColor"],
            "code": theme["codeBackgroundColor"],
            **{f"sidebar {k}": v for k, v in sidebar.items() if k != "borderColor"},
        }
        pairs = {f"text on {name}": (theme["textColor"], bg) for name, bg in surfaces.items()}
        pairs["caption on main"] = (theme["grayColor"], theme["backgroundColor"])
        if "backgroundColor" in sidebar:
            pairs["caption on sidebar"] = (theme["grayColor"], sidebar["backgroundColor"])
        # Streamlit draws primary buttons with white text in both modes.
        pairs["button text on primary"] = ("#FFFFFF", theme["primaryColor"])
        pairs["link on main"] = (theme["linkColor"], theme["backgroundColor"])
        for hue in _STATUS_HUES:
            pairs[f"{hue} text on its tint"] = (
                theme[f"{hue}TextColor"],
                theme[f"{hue}BackgroundColor"],
            )
        for hue in (*_STATUS_HUES, "violet"):
            pairs[f"{hue} on main"] = (theme[f"{hue}Color"], theme["backgroundColor"])

        failing = {
            name: round(_contrast(fg, bg), 2)
            for name, (fg, bg) in pairs.items()
            if _contrast(fg, bg) < _AA_TEXT
        }
        assert not failing

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_primary_is_visible_as_a_focus_ring(self, config, mode):
        theme = config["theme"][mode]
        assert _contrast(theme["primaryColor"], theme["backgroundColor"]) >= _AA_NON_TEXT
