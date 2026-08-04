"""Tests for the LangGraph -> UI event translation.

Coverage is split deliberately:

* **Integration** (`TestAgainstRealAgent`) drives a real compiled deep agent with
  a fake chat model, confirming the ``(mode, chunk)`` stream contract this
  module depends on actually holds for the installed langgraph.
* **Unit** (everything else) drives the translator with synthetic streams of
  those same shapes. `GenericFakeChatModel` silently drops ``tool_calls`` on its
  streaming path, so tool events, error envelopes and Anthropic content blocks
  cannot be produced through a real agent without a live model -- and the
  translator is the code under test regardless.

No API key or network access is required by any test here.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    ToolMessageChunk,
)
from langgraph.checkpoint.memory import InMemorySaver

from financial_planner.streaming import Token, ToolEnd, ToolStart, stream_agent_events


class FakeGraph:
    """Replays a scripted list of ``(mode, chunk)`` tuples.

    The shapes below were captured from a live compiled agent, so this stands in
    for langgraph faithfully.
    """

    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self._events = events

    def stream(self, _inputs, _config, stream_mode=None):  # noqa: ARG002
        yield from self._events


def _events(script: list[tuple[str, Any]]) -> list:
    return list(stream_agent_events(FakeGraph(script), [], {}))


def _text(events: list) -> str:
    return "".join(e.text for e in events if isinstance(e, Token))


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


class TestTextStreaming:
    def test_string_content_is_streamed(self):
        script = [("messages", (AIMessage(content="on track"), {}))]
        assert _text(_events(script)) == "on track"

    def test_multiple_chunks_concatenate_in_order(self):
        script = [
            ("messages", (AIMessage(content="You are "), {})),
            ("messages", (AIMessage(content="on track."), {})),
        ]
        assert _text(_events(script)) == "You are on track."

    def test_empty_chunks_produce_no_tokens(self):
        script = [("messages", (AIMessage(content=""), {}))]
        assert _events(script) == []


class TestOnlyAssistantTextIsRendered:
    """Regression: langgraph's "messages" mode emits every message type.

    A live run leaked raw tool-result JSON and "Successfully replaced 1
    instance(s)..." into the visible answer, because the translator only checked
    that content was a string. Anything that is not an assistant message must be
    dropped from the transcript.
    """

    def test_tool_result_json_is_not_rendered_as_answer_text(self):
        result = ToolMessage(
            content='{"final_balance_nominal":599513.32,"years":20}',
            tool_call_id="call_1",
            name="project_savings",
        )
        script = [("messages", (result, {}))]
        assert _text(_events(script)) == ""

    def test_file_tool_confirmation_is_not_rendered(self):
        result = ToolMessage(
            content="Successfully replaced 1 instance(s) of the string in '/AGENTS.md'",
            tool_call_id="call_2",
            name="edit_file",
        )
        script = [("messages", (result, {}))]
        assert _text(_events(script)) == ""

    def test_human_and_system_messages_are_not_rendered(self):
        script = [
            ("messages", (HumanMessage(content="my question"), {})),
            ("messages", (SystemMessage(content="system prompt"), {})),
        ]
        assert _text(_events(script)) == ""

    def test_streamed_chunks_are_recognised_as_assistant_text(self):
        """The chunk type string is "AIMessageChunk", not "ai".

        Filtering on ``.type == "ai"`` looks correct and silently discards every
        real streamed token. The guard must be an isinstance check.
        """
        assert AIMessageChunk(content="x").type != "ai"
        script = [("messages", (AIMessageChunk(content="streamed"), {}))]
        assert _text(_events(script)) == "streamed"

    def test_streamed_tool_output_is_not_rendered(self):
        chunk = ToolMessageChunk(content='{"balance":1}', tool_call_id="c")
        script = [("messages", (chunk, {}))]
        assert _text(_events(script)) == ""

    def test_assistant_text_still_renders_alongside_tool_traffic(self):
        """The realistic interleaving: tool result, then the actual answer."""
        script = [
            ("messages", (AIMessage(content="Let me project that."), {})),
            (
                "messages",
                (ToolMessage(content='{"x":1}', tool_call_id="c", name="t"), {}),
            ),
            ("messages", (AIMessage(content=" About $600,000."), {})),
        ]
        assert _text(_events(script)) == "Let me project that. About $600,000."


class TestMessageBoundaries:
    """Regression: separate assistant messages ran together into one paragraph.

    A turn emits a preamble before each tool call and then the answer. A live
    run rendered "...inspecting the file.Sign convention confirmed..." because
    the translator concatenated them with no separator. Chunks of one message
    share an ``id``; a change of id marks a new message.
    """

    def test_distinct_messages_are_separated_by_a_blank_line(self):
        script = [
            ("messages", (AIMessageChunk(content="Reading the file.", id="msg_1"), {})),
            ("messages", (AIMessageChunk(content="Spending is $2,390.", id="msg_2"), {})),
        ]
        assert _text(_events(script)) == "Reading the file.\n\nSpending is $2,390."

    def test_chunks_of_one_message_are_not_separated(self):
        script = [
            ("messages", (AIMessageChunk(content="You are ", id="msg_1"), {})),
            ("messages", (AIMessageChunk(content="on track.", id="msg_1"), {})),
        ]
        assert _text(_events(script)) == "You are on track."

    def test_a_content_free_chunk_does_not_swallow_the_break(self):
        """The shape a live provider actually sends.

        Anthropic emits a content-free chunk carrying the NEW message id before
        the first text delta. Recording that id as "the last message seen" made
        the following text compare equal to itself, so the break never fired --
        a bug that only appeared live, because the earlier synthetic scripts
        jumped straight from text to text.
        """
        script = [
            ("messages", (AIMessageChunk(content="Running the base case.", id="msg_1"), {})),
            ("messages", (AIMessageChunk(content="", id="msg_2"), {})),
            ("messages", (AIMessageChunk(content="Now", id="msg_2"), {})),
            ("messages", (AIMessageChunk(content=" the stress case.", id="msg_2"), {})),
        ]
        assert _text(_events(script)) == "Running the base case.\n\nNow the stress case."

    def test_tool_call_chunks_between_messages_do_not_swallow_the_break(self):
        """Tool-use blocks carry no text but do carry an id."""
        tool_block: list[Any] = [{"type": "tool_use", "name": "project_savings", "input": {}}]
        script = [
            ("messages", (AIMessageChunk(content="Checking.", id="msg_1"), {})),
            ("messages", (AIMessageChunk(content=tool_block, id="msg_1"), {})),
            ("messages", (AIMessageChunk(content=tool_block, id="msg_2"), {})),
            ("messages", (AIMessageChunk(content="Done.", id="msg_2"), {})),
        ]
        assert _text(_events(script)) == "Checking.\n\nDone."

    def test_no_separator_is_emitted_before_the_first_text(self):
        script = [("messages", (AIMessageChunk(content="First.", id="msg_1"), {}))]
        assert _text(_events(script)) == "First."

    def test_a_text_free_message_does_not_leave_a_dangling_separator(self):
        """A tool-call-only message has no text; it must not add whitespace."""
        script = [
            ("messages", (AIMessageChunk(content="Answer.", id="msg_1"), {})),
            ("messages", (AIMessageChunk(content="", id="msg_2"), {})),
        ]
        assert _text(_events(script)) == "Answer."

    def test_existing_trailing_break_is_not_doubled(self):
        script = [
            ("messages", (AIMessageChunk(content="A list:\n\n", id="msg_1"), {})),
            ("messages", (AIMessageChunk(content="- one", id="msg_2"), {})),
        ]
        assert _text(_events(script)) == "A list:\n\n- one"

    def test_multiple_blocks_in_one_message_are_not_separated(self):
        """Two text blocks of the same message share an id -- no break between."""
        blocks: list[Any] = [
            {"type": "text", "text": "part one "},
            {"type": "text", "text": "part two"},
        ]
        script = [("messages", (AIMessageChunk(content=blocks, id="msg_1"), {}))]
        assert _text(_events(script)) == "part one part two"


class TestAnthropicBlockContent:
    """Anthropic returns content as typed blocks; only text belongs on screen."""

    def test_thinking_blocks_are_not_rendered_as_answer_text(self):
        blocks: list[Any] = [
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "visible answer"},
        ]
        script = [("messages", (AIMessage(content=blocks), {}))]
        assert _text(_events(script)) == "visible answer"

    def test_tool_use_blocks_are_not_rendered_as_text(self):
        blocks: list[Any] = [
            {"type": "tool_use", "name": "project_savings", "input": {}},
            {"type": "text", "text": "done"},
        ]
        script = [("messages", (AIMessage(content=blocks), {}))]
        assert _text(_events(script)) == "done"


class TestToolEvents:
    CALL = _tool_call("project_savings", {"years": 20}, "call_1")

    def test_tool_start_carries_name_and_args(self):
        script = [("updates", {"model": {"messages": [self.CALL]}})]
        starts = [e for e in _events(script) if isinstance(e, ToolStart)]
        assert len(starts) == 1
        assert starts[0].name == "project_savings"
        assert starts[0].args["years"] == 20

    def test_tool_end_is_emitted_for_a_tool_message(self):
        result = ToolMessage(
            content='{"final_balance_nominal":123}', tool_call_id="call_1", name="project_savings"
        )
        script = [("updates", {"tools": {"messages": [result]}})]
        ends = [e for e in _events(script) if isinstance(e, ToolEnd)]
        assert len(ends) == 1
        assert ends[0].name == "project_savings"
        assert ends[0].ok is True

    def test_start_precedes_end_across_the_stream(self):
        result = ToolMessage(content="{}", tool_call_id="call_1", name="project_savings")
        script = [
            ("updates", {"model": {"messages": [self.CALL]}}),
            ("updates", {"tools": {"messages": [result]}}),
        ]
        kinds = [type(e).__name__ for e in _events(script)]
        assert kinds.index("ToolStart") < kinds.index("ToolEnd")

    def test_repeated_message_yields_one_start(self):
        """Updates can resurface the same message; dedupe by tool call id."""
        script = [
            ("updates", {"model": {"messages": [self.CALL]}}),
            ("updates", {"model": {"messages": [self.CALL]}}),
        ]
        assert len([e for e in _events(script) if isinstance(e, ToolStart)]) == 1

    def test_parallel_tool_calls_all_reported(self):
        parallel = AIMessage(
            content="",
            tool_calls=[
                {"name": "get_quote", "args": {}, "id": "a"},
                {"name": "search_web", "args": {}, "id": "b"},
            ],
        )
        script = [("updates", {"model": {"messages": [parallel]}})]
        starts = [e for e in _events(script) if isinstance(e, ToolStart)]
        assert {s.name for s in starts} == {"get_quote", "search_web"}

    def test_none_payload_is_tolerated(self):
        """Middleware nodes emit a None payload; that must not crash the stream."""
        script = [("updates", {"SomeMiddleware.before_agent": None})]
        assert _events(script) == []


class TestToolFailureIsVisible:
    """Tools return {"error": ...} rather than raising, so success must be checked."""

    @pytest.mark.parametrize(
        ("content", "expected_ok"),
        [
            ('{"error":"years must be in (0, 100]"}', False),
            ('{ "error": "bad input"}', False),
            ('{"final_balance_nominal":123}', True),
            ("", True),
        ],
    )
    def test_error_envelope_marks_the_call_failed(self, content, expected_ok):
        result = ToolMessage(content=content, tool_call_id="x", name="project_savings")
        script = [("updates", {"tools": {"messages": [result]}})]
        ends = [e for e in _events(script) if isinstance(e, ToolEnd)]
        assert [e.ok for e in ends] == [expected_ok]


class ToolBindableFake(GenericFakeChatModel):
    """Fake model that accepts tool binding; Deep Agents always binds built-ins."""

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002 - signature parity only
        return self


class TestAgainstRealAgent:
    """Confirms the stream contract holds for a genuinely compiled deep agent."""

    def test_text_streams_from_a_compiled_agent(self):
        agent = create_deep_agent(
            model=ToolBindableFake(messages=iter([AIMessage(content="Projection ready.")])),
            tools=[],
            system_prompt="test harness",
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 10}
        events = list(stream_agent_events(agent, [{"role": "user", "content": "hello"}], config))
        assert _text(events) == "Projection ready."
