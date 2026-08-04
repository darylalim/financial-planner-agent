"""Translate LangGraph stream events into UI-friendly events.

Kept out of the Streamlit layer so the event parsing can be tested without a
running app or a live model.

The contract this relies on (verified against langgraph as installed): calling
``agent.stream(..., stream_mode=["updates", "messages"])`` yields
``(mode, chunk)`` tuples where

* ``mode == "messages"`` -> ``chunk`` is ``(AIMessageChunk, metadata)`` and
  carries token-level text;
* ``mode == "updates"`` -> ``chunk`` is ``{node_name: payload}`` and payload may
  be ``None`` or a dict containing ``"messages"``.

Tool activity is read from the ``updates`` stream rather than the token stream
because tool call arguments arrive complete there, instead of as partial JSON.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage

__all__ = ["Token", "ToolEnd", "ToolStart", "StreamEvent", "stream_agent_events"]


@dataclass
class Token:
    """A fragment of assistant text."""

    text: str


@dataclass
class ToolStart:
    """The agent has invoked a tool."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolEnd:
    """A tool returned. ``ok`` is False when the tool reported an error."""

    name: str
    ok: bool = True


StreamEvent = Token | ToolStart | ToolEnd


def _is_error_result(content: Any) -> bool:
    """Detect the error envelope our tools return.

    Tools return errors as ``{"error": ...}`` JSON rather than raising, so a
    failure is a normal-looking ToolMessage. Checking the payload keeps the UI
    honest about what actually succeeded.
    """
    if not isinstance(content, str):
        return False
    stripped = content.lstrip()
    return stripped.startswith('{"error"') or stripped.startswith('{ "error"')


def stream_agent_events(
    agent: Any,
    messages: list[dict[str, str]],
    config: dict[str, Any],
) -> Iterator[StreamEvent]:
    """Run the agent and yield display events as they occur.

    Args:
        agent: A compiled deep agent.
        messages: Messages to append to the thread. With a checkpointer this is
            normally just the new user turn -- prior turns are restored from the
            checkpoint, so resending them would duplicate the history.
        config: LangGraph config, must include ``configurable.thread_id``.

    Yields:
        :class:`Token`, :class:`ToolStart` and :class:`ToolEnd` events.
    """
    seen_tool_calls: set[str] = set()
    last_message_id: str | None = None
    last_fragment: str | None = None

    for mode, chunk in agent.stream(
        {"messages": messages}, config, stream_mode=["updates", "messages"]
    ):
        if mode == "messages":
            message, _metadata = chunk
            # The "messages" mode emits EVERY message type, not just assistant
            # text -- tool results arrive here too. Without this guard the raw
            # tool JSON is rendered to the user as though it were the answer.
            #
            # Test isinstance, not ``.type``: a streamed chunk reports
            # ``type == "AIMessageChunk"``, not "ai", so a string comparison
            # drops the very tokens this function exists to yield.
            # AIMessageChunk subclasses AIMessage; ToolMessage does not.
            if not isinstance(message, AIMessage):
                continue

            message_id = getattr(message, "id", None)
            fragments = _text_fragments(message)
            if fragments:
                # One turn produces several assistant messages -- a preamble
                # before each tool call, then the answer. They are separate
                # paragraphs, but the stream hands them over with no gap, so
                # without this they render as "...the file.Sign convention...".
                # Chunks of one message share an id; a new id means a new
                # message.
                if (
                    last_fragment is not None
                    and message_id is not None
                    and message_id != last_message_id
                    and not last_fragment.endswith("\n\n")
                ):
                    yield Token("\n\n")
                for fragment in fragments:
                    yield Token(fragment)
                    last_fragment = fragment
                # Only advance on a message that actually produced text.
                # Anthropic emits a content-free chunk carrying the NEW id
                # before the first text delta; advancing on that would consume
                # the boundary before there was anything to separate.
                last_message_id = message_id

        elif mode == "updates":
            for _node, payload in (chunk or {}).items():
                if not isinstance(payload, dict):
                    continue
                for message in payload.get("messages") or []:
                    yield from _events_for_message(message, seen_tool_calls)


def _text_fragments(message: Any) -> list[str]:
    """Extract the displayable text from an assistant message.

    Content arrives in two shapes. Anthropic streams a list of typed blocks --
    text, thinking, tool_use -- of which only ``text`` belongs on screen;
    other providers stream a plain string.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    fragments = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            fragment = block.get("text")
            if fragment:
                fragments.append(fragment)
    return fragments


def _events_for_message(message: Any, seen: set[str]) -> Iterator[StreamEvent]:
    """Emit tool events for a single message from the updates stream."""
    for call in getattr(message, "tool_calls", None) or []:
        call_id = call.get("id") or f"{call.get('name')}:{len(seen)}"
        if call_id in seen:
            continue
        seen.add(call_id)
        yield ToolStart(name=call.get("name", "tool"), args=call.get("args") or {})

    # A ToolMessage carries tool_call_id; ordinary AI messages do not.
    if getattr(message, "tool_call_id", None) is not None:
        yield ToolEnd(
            name=getattr(message, "name", None) or "tool",
            ok=not _is_error_result(getattr(message, "content", "")),
        )
