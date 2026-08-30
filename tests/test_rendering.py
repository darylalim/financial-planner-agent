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


class TestIndentedCodeBlocks:
    """A four-space-indented block is code with no backticks anywhere in it.
    Escaping inside one puts a literal backslash on the user's screen, which is
    exactly the outcome the code exclusion exists to prevent.
    """

    def test_indented_block_after_a_blank_line_is_untouched(self):
        text = "Run this:\n\n    export RATE=$0.07\n\nDone."
        assert escape_dollars(text) == "Run this:\n\n    export RATE=$0.07\n\nDone."

    def test_indented_block_at_the_start_of_the_text_is_untouched(self):
        text = "    total = $5\n"
        assert escape_dollars(text) == text

    def test_a_blank_line_does_not_end_an_indented_block(self):
        text = "Setup:\n\n    a = $1\n\n    b = $2\n"
        assert escape_dollars(text) == text

    def test_prose_after_an_indented_block_is_escaped_again(self):
        text = "Setup:\n\n    a = $1\n\nThat costs $2."
        result = escape_dollars(text)
        assert "    a = $1" in result
        assert result.endswith(r"That costs \$2.")

    def test_tab_indented_block_is_untouched(self):
        text = "Setup:\n\n\tfee = $30\n"
        assert escape_dollars(text) == text

    def test_indented_continuation_of_a_bullet_is_still_escaped(self):
        """Ambiguous indentation must escape: a list continuation is prose, and
        an unescaped pair of amounts there is the LaTeX bug all over again.
        """
        text = "- Retirement:\n\n    $2.26M at 7% and $1.56M at 5%.\n"
        expected = "- Retirement:\n\n" + r"    \$2.26M at 7% and \$1.56M at 5%." + "\n"
        assert escape_dollars(text) == expected

    def test_indented_continuation_after_a_lazy_list_line_is_still_escaped(self):
        """A line at column 0 can lazily continue a list item, so it is not
        enough to end the list -- the indented block below is still prose.
        """
        text = "- Retirement:\nprojected at 7%\n\n    $2.26M by 65.\n"
        assert escape_dollars(text).endswith("    " + r"\$2.26M by 65." + "\n")

    def test_indented_block_after_the_list_ends_is_untouched(self):
        text = "- one\n- two\n\nThen run:\n\n    RATE=$0.07\n"
        result = escape_dollars(text)
        assert result.endswith("    RATE=$0.07\n")

    def test_numbered_list_continuation_is_escaped(self):
        text = "1. Step one\n\n    Costs $40 a month.\n"
        assert escape_dollars(text).endswith("    " + r"Costs \$40 a month." + "\n")


class TestFencedBlocks:
    def test_unclosed_fence_protects_its_content(self):
        """Mid-stream the closing fence has not arrived; its interior is still
        code and must not be escaped.
        """
        text = "Total $5.\n\n```bash\necho $100\necho $200\n"
        result = escape_dollars(text)
        assert result.startswith(r"Total \$5.")
        assert "echo $100\necho $200" in result

    def test_interior_is_unchanged_when_the_closing_fence_arrives(self):
        """The UI re-renders on every token, so a block whose interior changes
        when the fence closes is a rewrite the user watches happen.
        """
        opened = "Total $5.\n\n```bash\necho $100\n"
        closed = opened + "```\n\nRemainder $2."
        assert escape_dollars(closed).startswith(escape_dollars(opened))

    def test_closed_fence_still_behaves_as_before(self):
        text = "```python\nprint('$1')\n```"
        assert escape_dollars(text) == text

    def test_tilde_fence_is_untouched(self):
        text = "~~~\nfee = $30\n~~~"
        assert escape_dollars(text) == text

    def test_a_tilde_line_does_not_close_a_backtick_fence(self):
        text = "```\nfee = $30\n~~~\nstill code $40\n```"
        assert escape_dollars(text) == text

    def test_fence_indented_up_to_three_spaces_is_recognised(self):
        text = "   ```\n   fee = $30\n   ```"
        assert escape_dollars(text) == text

    def test_a_shorter_run_of_backticks_does_not_close_a_longer_fence(self):
        text = "````\n```\nfee = $30\n````"
        assert escape_dollars(text) == text

    def test_prose_after_a_closed_fence_is_escaped(self):
        text = "```\nfee = $30\n```\n\nThat is $360 a year."
        result = escape_dollars(text)
        assert "fee = $30" in result
        assert result.endswith(r"That is \$360 a year.")


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

    def test_a_fenced_answer_only_ever_grows(self):
        """No prefix of a fenced answer renders text that a later token then
        rewrites -- every partial render is a prefix of the finished one.
        """
        answer = "At 65: $2.26M.\n\n```bash\nplan --rate $0.07\n```\n\nLeaves $1,809."
        full = escape_dollars(answer)
        for i in range(len(answer) + 1):
            assert full.startswith(escape_dollars(answer[:i]))
