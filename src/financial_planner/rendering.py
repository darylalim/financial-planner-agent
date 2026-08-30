"""Markdown preparation for the Streamlit layer.

Streamlit's ``st.markdown`` renders ``$...$`` as inline LaTeX. In a financial
planner that is actively destructive: "you'd have $2.26M at 7% and $1.56M at 5%"
has two dollar signs, so everything between them is parsed as math and rendered
as italic gibberish. Nearly every answer this agent writes contains at least two
dollar amounts.

The markdown *source* is correct, which is why neither the unit tests nor the
live check caught this -- both inspect the text the agent produced. It is only
wrong once a browser renders it.

Escaping has to stop at code, because markdown does not honour backslash escapes
inside code: a ``\\$`` written into a code block is shown to the user verbatim,
backslash and all. Recognising code therefore has to cover every form the agent
actually emits, which is why this module scans line by line rather than matching
backtick pairs:

* Fenced blocks (``` or ~~~), including a fence that is still *open*. During
  streaming the closing fence has not arrived yet; if an unclosed fence did not
  count as code, its dollars would be escaped on every token and then silently
  un-escape when the fence finally landed, so the user would watch the block
  rewrite itself. An open fence is treated as running to the end of the text,
  which makes the output for a block's interior identical before and after its
  closing fence arrives.
* Four-space-indented code blocks, which have no backticks at all.
* Inline code spans, within lines that are not already code.

The indented-code rule is deliberately timid. A four-space indent is only code
when the context is unambiguous -- it follows a blank line and is not a
continuation of a list item -- because guessing wrong in that direction merely
leaves a rare ``$`` unescaped inside prose, while guessing wrong in the other
direction brings back the LaTeX gibberish this module exists to prevent.
"""

from __future__ import annotations

import re

__all__ = ["escape_dollars"]

# Code spans inside a single line. Fenced blocks are recognised by the line
# scanner below, so this only has to cope with the inline forms; the longest
# delimiter comes first so a ``` region is consumed whole rather than being
# chopped up by the shorter alternatives.
_CODE_SPAN = re.compile(r"```.*?```|``.*?``|`[^`]*`")

# A "$" that is not already escaped. Markdown escapes do not apply inside code,
# which is why code regions are excluded before this runs.
_UNESCAPED_DOLLAR = re.compile(r"(?<!\\)\$")

# A fence line: up to three leading spaces (CommonMark's allowance before the
# indent would instead mean "code block"), then three or more backticks or
# tildes, then the info string.
_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")

# A list item marker. Anything indented under one of these is a continuation of
# the item rather than a code block, so a list opens a region where the
# four-space rule is switched off.
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)")

# The indent that starts an indented code block.
_CODE_INDENT = re.compile(r"^(?: {4}|\t)")


def escape_dollars(text: str) -> str:
    """Escape ``$`` outside code so Streamlit renders it as a dollar sign.

    Code spans, fenced blocks and four-space-indented blocks are left untouched:
    markdown does not honour backslash escapes inside them, so escaping there
    would render a literal ``\\$`` to the user.

    A fence that is opened but never closed -- the normal state of affairs while
    an answer is streaming -- protects everything after it, so the text emitted
    for a block's interior does not change when its closing fence arrives.

    Args:
        text: Markdown as written by the agent or the user. May be a partial
            prefix of a streaming answer.

    Returns:
        The same markdown with LaTeX-triggering dollar signs neutralised.
    """
    out: list[str] = []
    open_fence: str | None = None
    in_indented_code = False
    in_list = False
    # The start of the text behaves like a blank line: an indented block there
    # is a code block, not a continuation of anything.
    after_blank = True

    for line in text.split("\n"):
        blank = not line.strip()

        if open_fence is not None:
            out.append(line)
            if _closes_fence(line, open_fence):
                open_fence = None
            after_blank = blank
            continue

        marker = _opens_fence(line)
        if marker is not None:
            out.append(line)
            open_fence = marker
            in_indented_code = False
            after_blank = False
            continue

        if blank:
            # A blank line does not end an indented code block: the block picks
            # up again if the next non-blank line is still indented.
            out.append(line)
            after_blank = True
            continue

        if in_indented_code and _CODE_INDENT.match(line):
            out.append(line)
            after_blank = False
            continue
        in_indented_code = False

        if _LIST_ITEM.match(line):
            in_list = True
        elif in_list and after_blank and not line.startswith((" ", "\t")):
            # A flush-left line after a blank line is the earliest point at
            # which the list is certainly over. Anything less definite (a lazy
            # continuation at column 0, say) keeps the list open, which keeps
            # the four-space rule off and so keeps dollars escaped.
            in_list = False

        if after_blank and not in_list and _CODE_INDENT.match(line):
            in_indented_code = True
            out.append(line)
            after_blank = False
            continue

        out.append(_escape_outside_spans(line))
        after_blank = False

    return "\n".join(out)


def _opens_fence(line: str) -> str | None:
    """Return the fence marker this line opens, or ``None`` if it opens none."""
    match = _FENCE.match(line)
    if match is None:
        return None
    marker = match.group("marker")
    # CommonMark forbids backticks in the info string of a backtick fence. That
    # rule is what stops a line such as ``` `` x `` ``` -- a code span that
    # happens to start with three backticks -- from opening a block.
    if marker[0] == "`" and "`" in match.group("info"):
        return None
    return marker


def _closes_fence(line: str, marker: str) -> bool:
    """Report whether ``line`` closes a fence opened with ``marker``."""
    match = _FENCE.match(line)
    if match is None:
        return False
    found = match.group("marker")
    # A closing fence uses the same character, is at least as long as the
    # opening one, and carries no info string.
    if found[0] != marker[0] or len(found) < len(marker):
        return False
    return not match.group("info").strip()


def _escape_outside_spans(line: str) -> str:
    """Escape dollars in a non-code line, skipping its inline code spans."""
    parts: list[str] = []
    position = 0
    for match in _CODE_SPAN.finditer(line):
        parts.append(_UNESCAPED_DOLLAR.sub(r"\\$", line[position : match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_UNESCAPED_DOLLAR.sub(r"\\$", line[position:]))
    return "".join(parts)
