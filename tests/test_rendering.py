"""Tests for markdown preparation.

Regression suite for a bug that a green test suite and a clean live check both
missed: the markdown source was correct, and only the browser render was wrong.
Streamlit parses ``$...$`` as LaTeX, so two dollar amounts in one sentence --
the normal case for this agent -- turned the text between them into math.
"""

from __future__ import annotations

import pytest

from financial_planner.rendering import escape_dollars


class TestDollarAmounts:
    def test_a_pair_of_amounts_is_neutralised(self):
        """The exact shape seen in the browser: the middle became italic math."""
        text = "At 65 you'd have $2.26M at 7% and $1.56M at 5%."
        assert escape_dollars(text) == r"At 65 you'd have \$2.26M at 7% and \$1.56M at 5%."

    def test_a_single_amount_is_escaped_too(self):
        """One "$" is harmless alone but pairs with the next assistant message."""
        assert escape_dollars("about $700k nominal") == r"about \$700k nominal"

    def test_amounts_in_a_table_are_escaped(self):
        text = "| Housing | $1,850.00 |"
        assert escape_dollars(text) == r"| Housing | \$1,850.00 |"

    def test_text_without_dollars_is_unchanged(self):
        text = "Your savings rate is 43.08% of income."
        assert escape_dollars(text) == text

    def test_already_escaped_dollars_are_not_double_escaped(self):
        assert escape_dollars(r"costs \$5") == r"costs \$5"


class TestCodeIsLeftAlone:
    """Markdown ignores backslash escapes inside code, so escaping there would
    render a literal backslash to the user.
    """

    def test_inline_code_is_untouched(self):
        text = "Pass `--budget $1200` to the tool."
        assert escape_dollars(text) == r"Pass `--budget $1200` to the tool."

    def test_fenced_block_is_untouched(self):
        text = "```\nexport RATE=$0.07\n```"
        assert escape_dollars(text) == text

    def test_prose_around_a_code_span_is_still_escaped(self):
        text = "Spending $2,390 — see `summarize_spending` — leaves $1,809."
        expected = r"Spending \$2,390 — see `summarize_spending` — leaves \$1,809."
        assert escape_dollars(text) == expected

    def test_fenced_block_containing_backticks_and_dollars(self):
        text = "Total $5.\n\n```python\nprint(f'{total}$')\n```\n\nRemainder $2."
        result = escape_dollars(text)
        assert r"Total \$5." in result
        assert "print(f'{total}$')" in result  # inside the fence, untouched
        assert r"Remainder \$2." in result


class TestStreamingSafety:
    """The streamed answer is re-rendered on every token, so partial text is
    passed through constantly and must never raise.
    """

    @pytest.mark.parametrize(
        "partial",
        ["", "$", "You'd have $", "You'd have $2.2", "`unclosed code $500", "```\npartial"],
    )
    def test_partial_text_does_not_raise(self, partial):
        escape_dollars(partial)

    def test_every_prefix_of_a_realistic_answer_is_safe(self):
        answer = "At 65: $2.26M at 7%, $1.56M at 5%. From `project_savings`."
        for i in range(len(answer) + 1):
            escape_dollars(answer[:i])
