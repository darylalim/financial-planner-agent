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
