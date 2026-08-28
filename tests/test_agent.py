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

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langgraph.checkpoint.memory import InMemorySaver

from financial_planner.agent import build_agent
from financial_planner.config import DEFAULT_MODEL
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

    ``InMemorySaver`` rather than the default: ``build_checkpointer()`` writes
    ``planner_state.sqlite`` into the project root, which ``FINANCIAL_PLANNER_HOME``
    does not relocate, so the default would leave a file behind in the repo.
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
                excluded_tools={"edit_file"},
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
