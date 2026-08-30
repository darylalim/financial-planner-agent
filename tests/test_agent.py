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
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langgraph.checkpoint.memory import InMemorySaver

from financial_planner.agent import build_agent
from financial_planner.config import AGENT_HOME, CHECKPOINT_DB, DEFAULT_MODEL, PROJECT_ROOT
from financial_planner.tools import ALL_TOOLS

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


class ToolBindableFake(GenericFakeChatModel):
    """Fake model that accepts tool binding; Deep Agents always binds built-ins."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ARG002 - parity only
        return self


@pytest.fixture(scope="module")
def agent() -> Any:
    """A compiled agent built exactly as the app builds it, minus the real model.

    ``InMemorySaver`` rather than the default: ``build_checkpointer()`` opens a
    real SQLite connection and creates ``planner_state.sqlite`` on disk. That
    now lands beside conftest's throwaway home rather than in the repo, but a
    composition test has no business creating a database at all.
    """
    return build_agent(model=ToolBindableFake(messages=iter([])), checkpointer=InMemorySaver())


def bound_tool_names(agent: Any) -> set[str]:
    return set(agent.nodes["tools"].bound._tools_by_name)


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


def subagent_tool_names(agent: Any, name: str = "general-purpose") -> set[str]:
    """Reach the compiled subagent graph hanging off the `task` tool.

    deepagents exposes no public accessor, so this walks the tool's closure. If
    a future version restructures that, this raises and the test fails loudly --
    which is the point: silence here is what the assertions below exist to
    prevent.
    """
    task = agent.nodes["tools"].bound._tools_by_name["task"]
    cells = dict(zip(task.func.__code__.co_freevars, task.func.__closure__, strict=True))
    return set(cells["subagent_graphs"].cell_contents[name].nodes["tools"].bound._tools_by_name)


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
        register_harness_profile(
            self.SENTINEL_SPEC,
            HarnessProfile(
                tool_description_overrides={"read_file": "sentinel"},
                excluded_tools=frozenset({"edit_file"}),
            ),
        )
        yield
        harness_profiles._HARNESS_PROFILES.clear()
        harness_profiles._HARNESS_PROFILES.update(snapshot)

    def test_the_configured_model_has_no_profile_overrides_to_drop(self) -> None:
        profile = self._profile(DEFAULT_MODEL)
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
        assert CHECKPOINT_DB == AGENT_HOME.parent / "planner_state.sqlite"


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
