"""Tests for agent assembly.

These pin the *shape* of the compiled agent rather than its behaviour: which
tools the model can actually call, and whether the middleware that loads the
household profile and the skills library is still installed.

That is worth a test file of its own because the failure mode is silent. Deep
Agents assembles its own middleware stack and ``build_agent`` amends it by
passing ``middleware=``; the merge is keyed on ``AgentMiddleware.name``, which
defaults to the class name. A library rename or a signature change does not
raise -- it just stops overriding, and the agent quietly regains `delete` or
loses `write_todos` with nothing in the suite to notice. Both of those bugs
shipped once already.

A fake chat model stands in for the real one, so no API key or network access is
required. Tool binding and graph topology are settled at build time, so this is
sufficient to verify composition.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langgraph.checkpoint.memory import InMemorySaver

from financial_planner.agent import build_agent, build_model
from financial_planner.config import (
    AGENT_HOME,
    ANTHROPIC_EFFORT,
    CHECKPOINT_DB,
    DEFAULT_MODEL,
    PROJECT_ROOT,
    READER_MODEL,
    THINKING_BINDING_BETA,
    anthropic_model_name,
)
from financial_planner.prompts import FINANCE_SUMMARY_PROMPT
from financial_planner.tools import ALL_TOOLS
from financial_planner.tools.documents import DOCUMENT_READER

# The filesystem tools build_agent asks for by name. `delete` and `execute` are
# deliberately absent -- see the middleware list in agent.py.
HARNESS_TOOLS = {
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "task",
    "write_todos",
}

WITHHELD_TOOLS = {"delete", "execute"}

# Everything the document-reader may call. Exact: it is the agent that reads
# untrusted PDF text, so anything else it could call is a way to act on it.
READER_TOOL_NAMES = {"read_pdf_text"}


class ToolBindableFake(GenericFakeChatModel):
    """Fake model that accepts tool binding; Deep Agents always binds built-ins."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ARG002 - parity only
        return self


def fake() -> ToolBindableFake:
    return ToolBindableFake(messages=iter([]))


def build_fake_agent(**kwargs: Any) -> Any:
    """``build_agent`` with both models injected, so no chat model is built.

    Both, because ``reader_model`` defaults to a model *name*: a call passing
    only ``model=`` would build a real Sonnet client, which needs a key that
    CI does not have.
    """
    kwargs.setdefault("model", fake())
    kwargs.setdefault("reader_model", fake())
    kwargs.setdefault("checkpointer", InMemorySaver())
    return build_agent(**kwargs)


@pytest.fixture(scope="module")
def agent() -> Any:
    """A compiled agent built exactly as the app builds it, minus the real model.

    ``InMemorySaver`` rather than the default: ``build_checkpointer()`` opens a
    real SQLite connection and creates ``planner_state.sqlite`` on disk. That
    now lands beside conftest's throwaway home rather than in the repo, but a
    composition test has no business creating a database at all.
    """
    return build_fake_agent()


def bound_tools(agent: Any) -> dict[str, Any]:
    return agent.nodes["tools"].bound._tools_by_name


def bound_tool_names(agent: Any) -> set[str]:
    return set(bound_tools(agent))


class TestBoundTools:
    def test_every_custom_tool_is_bound(self, agent: Any) -> None:
        assert {t.name for t in ALL_TOOLS} <= bound_tool_names(agent)

    def test_requested_harness_tools_are_bound(self, agent: Any) -> None:
        assert HARNESS_TOOLS <= bound_tool_names(agent)

    def test_write_todos_is_bound(self, agent: Any) -> None:
        """The system prompt tells the agent to plan with it and the UI labels it.

        deepagents 0.7 dropped todo planning from its default stack, so this
        arrives only via the explicit ``TodoListMiddleware()``. It went unbound
        for a release while the prompt still instructed its use.
        """
        assert "write_todos" in bound_tool_names(agent)

    @pytest.mark.parametrize("name", sorted(WITHHELD_TOOLS))
    def test_withheld_tools_are_not_bound(self, agent: Any, name: str) -> None:
        """Absent from the tool node, not merely hidden from the model's schema.

        `delete` would let an agent that ingests prompt-injectable PDFs remove
        the household's statements, plans and profile, with no approval gate in
        the UI to catch it.
        """
        assert name not in bound_tool_names(agent)

    def test_the_bound_set_is_exactly_what_we_asked_for(self, agent: Any) -> None:
        """Catches tools arriving from a library upgrade as well as leaving."""
        assert bound_tool_names(agent) == {t.name for t in ALL_TOOLS} | HARNESS_TOOLS

    def test_the_main_agent_cannot_read_pdf_text(self, agent: Any) -> None:
        """The quarantine. The main agent can write -- to /AGENTS.md, the memory
        every later session loads -- so a PDF's text reaching it is one injected
        instruction away from a poisoned profile. Only the document-reader,
        which cannot write, reads it. By name, so the exact-set assertion above
        being edited to match cannot quietly let it back.
        """
        assert "read_pdf_text" not in bound_tool_names(agent)


def subagent_tools(agent: Any, name: str = "general-purpose") -> dict[str, Any]:
    """Reach the bound tools of the compiled subagent hanging off the `task` tool.

    deepagents exposes no public accessor, so this walks the tool's closure. If
    a future version restructures that, this raises and the test fails loudly --
    which is the point: silence here is what the assertions below exist to
    prevent.
    """
    task = agent.nodes["tools"].bound._tools_by_name["task"]
    cells = dict(zip(task.func.__code__.co_freevars, task.func.__closure__, strict=True))
    return cells["subagent_graphs"].cell_contents[name].nodes["tools"].bound._tools_by_name


def subagent_tool_names(agent: Any, name: str = "general-purpose") -> set[str]:
    return set(subagent_tools(agent, name))


class TestSubagentInheritsTheNarrowedFilesystem:
    """The `task` subagent is a second agent over the same agent_home.

    deepagents builds it its own FilesystemMiddleware and only inherits the
    narrowed one through a name-matching rule. Pinning only the main graph would
    leave the subagent free to regain `delete` over the household's statements
    while every assertion in TestBoundTools still passed.
    """

    @pytest.mark.parametrize("name", sorted(WITHHELD_TOOLS))
    def test_the_subagent_does_not_regain_withheld_tools(self, agent: Any, name: str) -> None:
        assert name not in subagent_tool_names(agent)

    def test_the_subagent_keeps_the_filesystem_tools_it_needs(self, agent: Any) -> None:
        assert {"ls", "read_file", "write_file", "edit_file", "glob", "grep"} <= (
            subagent_tool_names(agent)
        )

    def test_the_subagent_can_still_do_the_finance_work(self, agent: Any) -> None:
        assert {t.name for t in ALL_TOOLS} <= subagent_tool_names(agent)

    def test_the_subagent_cannot_read_pdf_text(self, agent: Any) -> None:
        """It inherits the main tool list and can write, so a PDF reader left
        in that list would be a way around the quarantine."""
        assert "read_pdf_text" not in subagent_tool_names(agent)


def subagent_graph(agent: Any, name: str) -> Any:
    task = agent.nodes["tools"].bound._tools_by_name["task"]
    cells = dict(zip(task.func.__code__.co_freevars, task.func.__closure__, strict=True))
    return cells["subagent_graphs"].cell_contents[name]


def run_reader(pdf_text: str, monkeypatch: pytest.MonkeyPatch | None = None) -> Any:
    """Run the document-reader graph on a one-page PDF, driven by a fake model
    that calls read_pdf_text once and then answers. Returns the final state."""
    from langchain_core.messages import AIMessage, HumanMessage
    from test_documents import _pdf_bytes

    from financial_planner.config import WORKSPACE_DIR, ensure_directories

    ensure_directories()
    path = WORKSPACE_DIR / "_pytest-reader.pdf"
    path.write_bytes(_pdf_bytes(pdf_text))
    call = {"name": "read_pdf_text", "args": {"path": "/workspace/_pytest-reader.pdf"}, "id": "r1"}
    reader = ToolBindableFake(
        messages=iter([AIMessage(content="", tool_calls=[call]), AIMessage(content="{}")])
    )
    try:
        graph = subagent_graph(build_fake_agent(reader_model=reader), DOCUMENT_READER)
        return graph.invoke({"messages": [HumanMessage("Read /workspace/_pytest-reader.pdf")]})
    finally:
        path.unlink(missing_ok=True)


class TestDocumentReaderIsQuarantined:
    """The one agent that reads PDF text, so the one that must not act on it.

    deepagents gives a declared subagent every main tool when its spec has no
    `tools`, and a *fresh* FilesystemMiddleware -- write_file, edit_file,
    delete and execute included -- rather than the narrowed one. Both have to
    be overridden in the spec, and both fail open.
    """

    def test_its_bound_set_is_exactly_reading(self, agent: Any) -> None:
        """Exact, like TestBoundTools, so an upgrade that adds a tool fails
        loudly. No filesystem tool at all, and no `task` to delegate onward."""
        assert subagent_tool_names(agent, DOCUMENT_READER) == READER_TOOL_NAMES

    def test_it_runs_with_no_filesystem_middleware(self) -> None:
        """_NoFilesystem takes a slot deepagents' own stack expects to hold a
        real FilesystemMiddleware, so a graph that builds is not enough: this
        runs a whole reader turn, tool call and answer."""
        messages = run_reader("Total purchases 1,234.56")["messages"]
        assert "1,234.56" in messages[2].content
        assert messages[-1].content == "{}"


class TestTheReaderNeverOffloads:
    """deepagents writes a tool result over its token limit to
    /large_tool_results/ -- for the reader, raw PDF text, in a folder the main
    agent's read_file can open. MAX_PDF_CHARS keeps read_pdf_text far below the
    limit today; the reader having no FilesystemMiddleware, and so no eviction,
    makes that a property of the reader rather than a margin.
    """

    def test_an_oversized_result_stays_in_the_readers_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """By behaviour: lift the cap, read a PDF holding ~3x the default
        eviction limit, and check the text came back inline and nothing
        reached disk."""
        import financial_planner.tools.documents as documents

        monkeypatch.setattr(documents, "MAX_PDF_CHARS", 10**7)
        text = "TOTAL " * 40_000  # 240k chars; eviction is 20k tokens of 4 chars
        offloaded = AGENT_HOME / "large_tool_results"
        before = set(offloaded.iterdir()) if offloaded.exists() else set()

        messages = run_reader(text)["messages"]
        assert len(messages[2].content) > 200_000
        after = set(offloaded.iterdir()) if offloaded.exists() else set()
        assert after == before

    def test_the_default_would_have_offloaded_it(self) -> None:
        """The control: what the reader would have got without _NoFilesystem
        does evict, so the test above is not passing because nothing does."""
        from deepagents import FilesystemMiddleware
        from deepagents.backends import FilesystemBackend

        default = FilesystemMiddleware(backend=FilesystemBackend(root_dir=AGENT_HOME))
        assert default._tool_token_limit_before_evict is not None
        assert default._tool_token_limit_before_evict * 4 < 240_000


def summarizers_by_agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Build the agent and return each graph's summarizers, keyed by agent name.

    The middleware instances are not reachable from a compiled graph -- they
    are folded into nested ``wrap_model_call`` closures -- so this records the
    ``middleware=`` list deepagents hands ``create_agent`` for the main graph
    and for each subagent, and calls through. That is the assembled stack
    itself, after every name match has been applied.
    """
    import deepagents.graph
    import deepagents.middleware.subagents

    built: dict[str, list[Any]] = {}

    def spy(real: Any) -> Any:
        def create_agent(*args: Any, **kwargs: Any) -> Any:
            built[kwargs.get("name") or "?"] = [
                m for m in kwargs.get("middleware") or [] if m.name == "SummarizationMiddleware"
            ]
            return real(*args, **kwargs)

        return create_agent

    for module in (deepagents.graph, deepagents.middleware.subagents):
        monkeypatch.setattr(module, "create_agent", spy(module.create_agent))
    build_fake_agent()
    return built


class TestSummarizerIsFinanceAware:
    """The default summary prompt is replaced by a name match, which fails open.

    A summary replaces the turns it covers, so a figure it drops or rewrites
    re-enters the conversation with no tool behind it. If the replacement stops
    matching -- a rename upstream, or a subclass, whose name differs -- the
    coding-session default comes back with no error, or the agent runs two
    summarizers. The `general-purpose` subagent inherits the replacement through
    a second name match, so it is pinned separately.
    """

    @pytest.mark.parametrize("graph", ["financial-planner", "general-purpose"])
    def test_each_graph_has_one_summarizer_with_the_finance_prompt(
        self, monkeypatch: pytest.MonkeyPatch, graph: str
    ) -> None:
        summarizers = summarizers_by_agent(monkeypatch)[graph]
        assert len(summarizers) == 1
        # deepagents wraps LangChain's middleware and keeps the prompt on it. A
        # private attribute, but a rename raises here rather than passing.
        assert summarizers[0]._lc_helper.summary_prompt == FINANCE_SUMMARY_PROMPT

    def test_the_summarizer_shares_the_agents_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So the effort and thinking settings reach summarization calls too."""
        model = fake()
        import deepagents.graph

        seen: list[Any] = []
        real = deepagents.graph.create_agent

        def create_agent(graph_model: Any, *args: Any, **kwargs: Any) -> Any:
            seen.extend(
                m.model for m in kwargs["middleware"] if m.name == "SummarizationMiddleware"
            )
            return real(graph_model, *args, **kwargs)

        monkeypatch.setattr(deepagents.graph, "create_agent", create_agent)
        build_fake_agent(model=model)
        assert seen == [model]


class TestFinanceSummaryPrompt:
    def test_it_keeps_deepagents_media_clause(self) -> None:
        """Built from deepagents' prompt, not LangChain's, so offloaded media
        references are still preserved across a summary."""
        assert "<media_reference_information>" in FINANCE_SUMMARY_PROMPT

    def test_it_requires_figures_verbatim_and_forbids_arithmetic(self) -> None:
        assert "verbatim" in FINANCE_SUMMARY_PROMPT
        assert "Never calculate, round, convert, total or combine figures" in (
            FINANCE_SUMMARY_PROMPT
        )

    def test_the_rules_sit_before_the_messages_it_summarizes(self) -> None:
        assert FINANCE_SUMMARY_PROMPT.index("<financial_figures>") < (
            FINANCE_SUMMARY_PROMPT.index("<messages>")
        )
        assert FINANCE_SUMMARY_PROMPT.count("{messages}") == 1


class TestModelSettings:
    """``build_model`` sends Anthropic-only settings, and only to Anthropic.

    Model construction is patched where ``financial_planner.agent`` looks it
    up, so these record the arguments without building anything.
    """

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
        import financial_planner.agent as agent_module

        recorded: list[tuple[str, str, dict[str, Any]]] = []

        def record(kind: str) -> Any:
            def build(spec: str, **kwargs: Any) -> Any:
                recorded.append((kind, spec, kwargs))
                return ToolBindableFake(messages=iter([]))

            return build

        monkeypatch.setattr(agent_module, "init_chat_model", record("init"))
        monkeypatch.setattr(agent_module, "resolve_model", record("resolve"))
        return recorded

    @pytest.mark.parametrize("spec", ["anthropic:claude-opus-5-5", "claude-sonnet-5-5"])
    def test_a_checking_model_gets_drop_block(self, calls: list, spec: str) -> None:
        """The whole `thinking` dict, not only `block_binding`.

        ChatAnthropic adds adaptive thinking only when `thinking` is unset, so
        a dict holding just the binding would switch adaptive thinking off.
        """
        build_model(spec)
        [(kind, _, kwargs)] = calls
        assert kind == "init"
        assert kwargs == {
            "effort": ANTHROPIC_EFFORT,
            "thinking": {
                "type": "adaptive",
                "display": "omitted",
                "block_binding": {"prefix_mismatch_behavior": "drop_block"},
            },
            "betas": [THINKING_BINDING_BETA],
        }

    def test_the_way_back_to_opus_5_sends_effort_only(self, calls: list) -> None:
        """Opus 5 does not run the history check, so it keeps langchain's default."""
        build_model("anthropic:claude-opus-5")
        assert calls == [("init", "anthropic:claude-opus-5", {"effort": ANTHROPIC_EFFORT})]

    def test_another_provider_gets_no_anthropic_settings(self, calls: list) -> None:
        build_model("openai:gpt-5.4")
        assert calls == [("resolve", "openai:gpt-5.4", {})]

    def test_build_agent_resolves_a_model_name(self, calls: list) -> None:
        """The summarizer factory raises TypeError on a string, so a name must be
        built first -- once, for both the agent and its summarizer."""
        build_agent(
            model="anthropic:claude-opus-5-5", reader_model=fake(), checkpointer=InMemorySaver()
        )
        assert [spec for _, spec, _ in calls] == ["anthropic:claude-opus-5-5"]

    def test_the_reader_is_built_from_reader_model_at_default_effort(self, calls: list) -> None:
        """Not injected, the reader comes from config.READER_MODEL. Sonnet 5.5
        runs the history check, so it gets drop_block like the main model, but
        no effort: it stays at the model's own default."""
        build_agent(model=fake(), checkpointer=InMemorySaver())
        [(kind, spec, kwargs)] = calls
        assert spec == READER_MODEL
        assert "effort" not in kwargs
        if anthropic_model_name(READER_MODEL) == "claude-sonnet-5-5":
            assert kwargs["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}


class TestReaderModelDefault:
    """``config`` reads the environment at import, and conftest fixes it for the
    whole session, so each case runs in a subprocess with its own environment
    -- not ``importlib.reload``, which would leave other modules holding the old
    values and leak into later tests.
    """

    @staticmethod
    def _reader_model(**env: str) -> str:
        base = {k: v for k, v in os.environ.items() if not k.startswith("FINANCIAL_PLANNER_")}
        base["FINANCIAL_PLANNER_HOME"] = os.environ["FINANCIAL_PLANNER_HOME"]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from financial_planner.config import READER_MODEL; print(READER_MODEL)",
            ],
            capture_output=True,
            text=True,
            env={**base, **env},
            cwd=PROJECT_ROOT,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_an_anthropic_main_model_pairs_with_sonnet(self) -> None:
        assert self._reader_model() == "anthropic:claude-sonnet-5-5"

    def test_another_provider_is_reused_rather_than_needing_an_anthropic_key(self) -> None:
        assert self._reader_model(FINANCIAL_PLANNER_MODEL="openai:gpt-5.4") == "openai:gpt-5.4"

    def test_an_explicit_reader_model_wins(self) -> None:
        assert (
            self._reader_model(
                FINANCIAL_PLANNER_MODEL="openai:gpt-5.4",
                FINANCIAL_PLANNER_READER_MODEL="anthropic:claude-haiku-5-5",
            )
            == "anthropic:claude-haiku-5-5"
        )


class TestNoRealModelIsBuiltInTests:
    """With both models injected, ``build_agent`` builds no chat model of its own.

    Keeps the "no API key, no network" promise now that ``build_agent`` builds
    models itself. Patched where each name is looked up: deepagents.graph
    imported ``resolve_model`` by name, so patching ``deepagents._models``
    would change nothing.
    """

    def test_an_injected_model_is_used_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import deepagents.graph

        import financial_planner.agent as agent_module

        def refuse(name: str) -> Any:
            def build(model: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG001
                if isinstance(model, str):
                    raise AssertionError(f"{name} built a real model from {model!r}")
                return model

            return build

        monkeypatch.setattr(deepagents.graph, "resolve_model", refuse("deepagents.graph"))
        monkeypatch.setattr(agent_module, "resolve_model", refuse("agent.resolve_model"))
        monkeypatch.setattr(agent_module, "init_chat_model", refuse("agent.init_chat_model"))
        build_fake_agent()


class TestHarnessProfileAssumptions:
    """`build_agent` replaces FilesystemMiddleware and forwards only `backend`.

    deepagents also passes `custom_tool_descriptions` from the resolved harness
    profile, which the replacement drops. That is harmless only while the
    configured model's profile carries no overrides -- and DEFAULT_MODEL is
    env-overridable via FINANCIAL_PLANNER_MODEL, while other shipped profiles do
    set them. Reproducing private constructor arguments would be worse than
    asserting the assumption they rest on.

    ``_harness_profile_for_model`` takes the spec in its *second* argument, and
    getting that wrong fails open. Given the spec first and ``spec=None`` it
    skips the short-circuit and introspects the instance instead, where
    ``get_model_identifier`` wants ``.model_name``/``.model`` and
    ``get_model_provider`` wants ``._get_ls_params()``. A `str` has neither, so
    both come back ``None``, every registry branch is guarded off, and the call
    returns an empty ``HarnessProfile()`` for *any* model -- reducing the
    assertions below to ``{} == {}``.
    """

    #: A spec no provider serves, for registering a profile of our own against.
    SENTINEL_SPEC = "fake-provider-for-tests:sentinel-model"
    #: A Claude name no model has, for the instance path, which reads the
    #: provider and name off a real ChatAnthropic rather than off a spec.
    SENTINEL_CLAUDE = "claude-sentinel-for-tests"

    @staticmethod
    def _profile(spec: str) -> Any:
        """Resolve a harness profile for ``spec`` the way `create_deep_agent` does.

        `create_deep_agent` resolves the model instance and keeps the original
        string as the spec (``graph.py``: ``resolve_model``, then
        ``_harness_profile_for_model(model, _model_spec)``). Only the spec
        reaches the registry -- the lookup returns before the instance is read
        -- so the fake stands in for it. Resolving DEFAULT_MODEL for real would
        need the configured provider's package and key, breaking this module's
        no-API-key promise for every non-Anthropic FINANCIAL_PLANNER_MODEL, and
        SENTINEL_SPEC has no provider to resolve at all.

        That leaves the fake unable to resolve anything if deepagents ever drops
        the short-circuit -- which is what ``test_the_guard_above_is_not_vacuous``
        is there to catch.
        """
        from deepagents.graph import _harness_profile_for_model

        return _harness_profile_for_model(ToolBindableFake(messages=iter([])), spec)

    @pytest.fixture
    def sentinel_profile(self) -> Any:
        """Register SENTINEL_SPEC, then put the global registry back as it was.

        ``register_harness_profile`` merges into a module-global dict and there
        is no unregister. Leaving the key behind is not inert:
        ``_has_any_harness_profile()`` is ``keys() - bootstrap keys``, so one
        non-builtin registration flips it True and promotes deepagents' "no
        harness profile matched" breadcrumb from debug to warning for every
        later test that builds an agent from a model instance.

        The builtins are loaded *before* the snapshot is taken, because their
        bootstrap is one-shot: restoring over an empty snapshot would drop them
        for the rest of the session with nothing to reload them. The dict is
        mutated in place rather than rebound, since deepagents holds a
        reference to that object.
        """
        from deepagents import HarnessProfile, register_harness_profile
        from deepagents.profiles.harness import harness_profiles

        harness_profiles._ensure_harness_profiles_loaded()
        snapshot = dict(harness_profiles._HARNESS_PROFILES)
        for spec in (self.SENTINEL_SPEC, f"anthropic:{self.SENTINEL_CLAUDE}"):
            register_harness_profile(
                spec,
                HarnessProfile(
                    tool_description_overrides={"read_file": "sentinel"},
                    excluded_tools=frozenset({"edit_file"}),
                ),
            )
        yield
        harness_profiles._HARNESS_PROFILES.clear()
        harness_profiles._HARNESS_PROFILES.update(snapshot)

    @pytest.mark.parametrize("spec", [DEFAULT_MODEL, READER_MODEL])
    def test_the_configured_model_has_no_profile_overrides_to_drop(self, spec: str) -> None:
        """The reader replaces its FilesystemMiddleware by name too, so its
        model rests on the same assumption."""
        profile = self._profile(spec)
        assert dict(profile.tool_description_overrides) == {}
        assert set(profile.excluded_tools) == set()

    def test_the_guard_above_is_not_vacuous(self, sentinel_profile: Any) -> None:
        """The assertions above have to be able to fail.

        They could not: the spec went into the model argument, so the lookup
        returned an empty profile whatever FINANCIAL_PLANNER_MODEL was set to,
        and the guard passed even against the shipped profiles that do set
        overrides. Registering one under a spec of our own and reading it back
        through the same call is what keeps that from going quiet again.
        """
        profile = self._profile(self.SENTINEL_SPEC)
        assert dict(profile.tool_description_overrides) == {"read_file": "sentinel"}
        assert set(profile.excluded_tools) == {"edit_file"}

    # -- The instance path ---------------------------------------------------
    # build_agent now hands create_deep_agent a built model, never a string, so
    # deepagents gets `_model_spec=None` and finds the profile by introspecting
    # the instance instead (provider from `_get_ls_params`, name from
    # `.model`). That is the path the app takes; the spec checks above no
    # longer describe it on their own.

    @staticmethod
    def _instance_profile(model: Any) -> Any:
        from deepagents.graph import _harness_profile_for_model

        return _harness_profile_for_model(model, None)

    @pytest.mark.parametrize("spec", [DEFAULT_MODEL, READER_MODEL])
    def test_the_configured_model_instance_has_no_profile_overrides_to_drop(
        self, monkeypatch: pytest.MonkeyPatch, spec: str
    ) -> None:
        """Built by ``build_model`` exactly as the app builds it.

        A ChatAnthropic is built but never called, so a placeholder key is
        enough; nothing reaches the network. Other providers are skipped for
        the reason ``_profile`` gives.
        """
        if anthropic_model_name(spec) is None:
            pytest.skip(f"{spec} is not an Anthropic model")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder-for-tests")
        profile = self._instance_profile(build_model(spec))
        assert dict(profile.tool_description_overrides) == {}
        assert set(profile.excluded_tools) == set()

    def test_the_instance_guard_is_not_vacuous(self, sentinel_profile: Any) -> None:
        """Introspection has to reach the registry, or the test above is ``{} == {}``."""
        from langchain_anthropic import ChatAnthropic

        model = ChatAnthropic(model=self.SENTINEL_CLAUDE, api_key="sk-ant-placeholder")
        profile = self._instance_profile(model)
        assert dict(profile.tool_description_overrides) == {"read_file": "sentinel"}
        assert set(profile.excluded_tools) == {"edit_file"}


class TestMiddlewareSurvives:
    """``middleware=`` is additive, but a name collision replaces in place.

    If a custom middleware ever shadowed one of these, the node would vanish
    from the compiled graph and the agent would silently stop reading the
    household profile or the skills library -- the core of the product.
    """

    @pytest.mark.parametrize(
        "node", ["SkillsMiddleware.before_agent", "MemoryMiddleware.before_agent"]
    )
    def test_default_middleware_node_is_still_present(self, agent: Any, node: str) -> None:
        assert node in agent.nodes

    def test_todo_middleware_node_is_present(self, agent: Any) -> None:
        assert "TodoListMiddleware.after_model" in agent.nodes


class TestRecursionLimitBudget:
    """``recursion_limit`` counts graph steps, and every node is one.

    deepagents 0.7.32 added ``SkillsMiddleware.before_model`` to the model/tools
    loop. That raised a tool round from three steps to four, and with it the
    agent ran out of steps a quarter sooner: the live retirement scenario
    stopped with ``GraphRecursionError``, and nothing offline noticed. The limits
    in ``app.py`` and ``scripts/live_check.py`` are sized for the nodes below; a
    change here means re-sizing them, not just updating this list.
    """

    LOOP = {
        "model",
        "tools",
        "TodoListMiddleware.after_model",
        "SkillsMiddleware.before_model",
    }

    def test_the_nodes_a_tool_round_crosses_are_the_ones_the_limits_assume(
        self, agent: Any
    ) -> None:
        per_round = {
            node
            for node in agent.nodes
            if node in ("model", "tools") or node.endswith((".before_model", ".after_model"))
        }
        assert per_round == self.LOOP


class TestCheckpointDatabaseLocation:
    """Where ``planner_state.sqlite`` lands is a boundary, not a preference.

    ``conftest`` redirects ``FINANCIAL_PLANNER_HOME`` at a throwaway directory
    so nothing in the suite touches the household's real installation. These
    assertions therefore run against a redirected home, which is exactly the
    case the pinned ``PROJECT_ROOT / "planner_state.sqlite"`` got wrong.
    """

    def test_the_checkpoint_db_is_not_inside_the_agent_home(self) -> None:
        """The agent must not be able to read its own conversation history.

        ``AGENT_HOME`` is the ``FilesystemBackend`` root, so anything under it
        is readable by ``read_file``/``grep``. The checkpoint database holds the
        full transcript of the household's finances -- balances, account
        details, debts -- and putting it in the agent's own world would hand a
        prompt-injected turn the entire history in one tool call.

        Both sides are resolved: on macOS the temporary home lives under a
        symlinked ``/var``, and a containment check across a symlink boundary
        that compares unresolved paths answers the wrong question.
        """
        assert not CHECKPOINT_DB.resolve().is_relative_to(AGENT_HOME.resolve())

    def test_the_checkpoint_db_follows_a_redirected_home(self) -> None:
        """It is a sibling of AGENT_HOME, so redirecting the home moves it too.

        It used to be pinned to ``PROJECT_ROOT``, which ``FINANCIAL_PLANNER_HOME``
        does not move -- so this suite and ``scripts/live_check.py``, which exist
        precisely to run against a throwaway home, still wrote their
        conversations into the real repository database.

        The containment check this used to carry -- that the database is not
        under ``PROJECT_ROOT`` -- asserted a property of the machine rather than
        of the code. With ``TMPDIR`` inside the checkout, which ``pytest
        --basetemp`` and many CI images arrange, conftest's throwaway home *is*
        inside ``PROJECT_ROOT`` and the assertion failed with nothing wrong. It
        also compared unresolved paths, unlike its sibling above, which
        documents that resolution as load-bearing across macOS's symlinked
        ``/var``. The equality below pins the regression exactly on its own,
        since ``AGENT_HOME`` here is the throwaway home, not the repository.
        """
        assert CHECKPOINT_DB == AGENT_HOME.parent / f"{AGENT_HOME.name}-state.sqlite"

    def test_two_homes_under_one_parent_do_not_share_a_database(self) -> None:
        """The parent alone is not unique, so the name carries the home.

        ``AGENT_HOME.parent / "planner_state.sqlite"`` sent
        ``/data/homes/alice`` and ``/data/homes/bob`` to the *same* file: two
        households sharing one thread store, either able to resume the other's
        conversation by thread id. Recomputed here rather than reimported --
        ``config`` freezes AGENT_HOME at import and this suite has already
        imported it.
        """
        homes = [Path("/data/homes/alice"), Path("/data/homes/bob")]
        databases = {home.parent / f"{home.name}-state.sqlite" for home in homes}
        assert len(databases) == len(homes)

    def test_the_default_home_keeps_the_historic_database_name(self) -> None:
        """An existing installation's saved conversations are not orphaned.

        The default resolves to ``<repo>/planner_state.sqlite``, the name it has
        always had, so only a *redirected* home takes the home-derived name.
        """
        default_home = (PROJECT_ROOT / "agent_home").resolve()
        expected = (
            PROJECT_ROOT / "planner_state.sqlite"
            if AGENT_HOME == default_home
            else AGENT_HOME.parent / f"{AGENT_HOME.name}-state.sqlite"
        )
        assert CHECKPOINT_DB == expected


class TestLiveCheckRefusesAVacuousRun:
    """``scripts/live_check.py`` must not report a pass when it checked nothing.

    It lives in this module because it is the same class of silent failure the
    file docstring describes: a green result that asserts nothing. ``search`` is
    skipped without ``TAVILY_API_KEY``, which can empty the requested set, and
    ``all({})`` is True -- so the script exited 0 and any wrapper or CI step
    reading that status saw a clean live run that never called the model.

    Run as a subprocess rather than imported: the module redirects
    ``FINANCIAL_PLANNER_HOME`` and copies a throwaway home at *import* time, so
    importing it here would mutate this session's environment. No scenario runs,
    so the key below is never used against the API.
    """

    def test_an_empty_scenario_set_exits_non_zero(self) -> None:
        env = {
            **os.environ,
            # Present so the missing-key gate passes; empty so 'search' is
            # skipped. python-dotenv does not override an already-set variable,
            # so a real .env cannot put the key back and start a paid run.
            "ANTHROPIC_API_KEY": "not-used-no-scenario-runs",
            "TAVILY_API_KEY": "",
        }
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "live_check.py"), "search"],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )

        # 2 rather than 1: main() already uses 2 for "cannot run" and 1 for "a
        # scenario found problems", and nothing ran here.
        assert result.returncode == 2, result.stdout + result.stderr
        assert "Cannot run" in result.stdout
