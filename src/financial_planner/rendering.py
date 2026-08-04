"""Markdown preparation for the Streamlit layer.

Streamlit's ``st.markdown`` renders ``$...$`` as inline LaTeX. In a financial
planner that is actively destructive: "you'd have $2.26M at 7% and $1.56M at 5%"
has two dollar signs, so everything between them is parsed as math and rendered
as italic gibberish. Nearly every answer this agent writes contains at least two
dollar amounts.

The markdown *source* is correct, which is why neither the unit tests nor the
live check caught this -- both inspect the text the agent produced. It is only
wrong once a browser renders it.
"""

from __future__ import annotations

import re

__all__ = ["escape_dollars"]

# Fenced blocks first so a ``` region is consumed whole rather than being
# chopped up by the inline-span alternative.
_CODE = re.compile(r"```.*?```|``.*?``|`[^`\n]*`", re.DOTALL)

# A "$" that is not already escaped. Markdown escapes do not apply inside code,
# which is why code regions are excluded before this runs.
_UNESCAPED_DOLLAR = re.compile(r"(?<!\\)\$")


def escape_dollars(text: str) -> str:
    """Escape ``$`` outside code so Streamlit renders it as a dollar sign.

    Code spans and fenced blocks are left untouched: markdown does not honour
    backslash escapes inside them, so escaping there would render a literal
    ``\\$`` to the user.

    Args:
        text: Markdown as written by the agent or the user.

    Returns:
        The same markdown with LaTeX-triggering dollar signs neutralised.
    """
    parts: list[str] = []
    position = 0
    for match in _CODE.finditer(text):
        parts.append(_UNESCAPED_DOLLAR.sub(r"\\$", text[position : match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_UNESCAPED_DOLLAR.sub(r"\\$", text[position:]))
    return "".join(parts)
