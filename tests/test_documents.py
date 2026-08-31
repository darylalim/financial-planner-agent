"""Tests for document ingestion and the path sandbox.

The sandbox tests matter more than the parsing tests: these tools take paths
straight from model output, and a prompt injection hidden in an uploaded PDF is
a realistic route to a traversal attempt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from financial_planner.config import WORKSPACE_DIR, ensure_directories
from financial_planner.tools.documents import (
    inspect_document,
    summarize_spending,
)

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "agent_home"
    / "skills"
    / "budget-from-statements"
    / "SKILL.md"
)


def _pdf_bytes(text: str) -> bytes:
    """Build a one-page PDF that really carries extractable text.

    `PdfWriter.add_blank_page` produces a PDF with no text in it, which is now a
    *different* case -- a scan -- so an ordinary-statement fixture cannot be
    built that way any more. pypdf cannot draw text and reportlab is not a
    dependency, so the file is assembled here: five objects and a real xref
    table, which pypdf reads without a warning.
    """
    stream = b"BT /F1 12 Tf 20 100 Td (" + text.encode("ascii") + b") Tj ET"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]"
        b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start_xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<</Size " + str(len(objects) + 1).encode() + b"/Root 1 0 R>>\n"
        b"startxref\n" + str(start_xref).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


CSV_CONTENT = """\
Date,Description,Category,Amount
2026-01-03,Paycheck,Income,4200.00
2026-01-04,Rent,Housing,-1850.00
2026-01-05,Groceries,Groceries,-150.00
2026-02-03,Paycheck,Income,4200.00
2026-02-04,Rent,Housing,-1850.00
2026-02-05,Groceries,Groceries,-250.00
"""


@pytest.fixture
def sample_csv():
    """Write a transaction file into the agent's workspace, then remove it."""
    ensure_directories()
    path = WORKSPACE_DIR / "_pytest-transactions.csv"
    path.write_text(CSV_CONTENT, encoding="utf-8")
    yield "/workspace/_pytest-transactions.csv"
    path.unlink(missing_ok=True)


def _call(tool, **kwargs) -> dict:
    return json.loads(tool.invoke(kwargs))


class TestPathSandbox:
    @pytest.mark.parametrize(
        "hostile_path",
        [
            "/../.env",
            "../../../etc/passwd",
            "/workspace/../../.env",
            "/workspace/../../../../../../etc/hosts",
        ],
    )
    def test_traversal_is_refused(self, hostile_path):
        result = _call(inspect_document, path=hostile_path)
        assert "error" in result
        assert "PathOutsideSandbox" in result["error"]

    def test_legitimate_workspace_path_is_allowed(self, sample_csv):
        assert "error" not in _call(inspect_document, path=sample_csv)

    def test_missing_file_reports_clearly_rather_than_raising(self):
        result = _call(inspect_document, path="/workspace/does-not-exist.csv")
        assert "does not exist" in result["error"]

    def test_a_missing_file_is_reported_without_the_host_path(self):
        """Every reading tool goes through the existence check now, because the
        one that did not let pypdf raise instead -- and pypdf's FileNotFoundError
        quotes the *resolved* path, naming the agent's real root on disk to a
        model whose paths arrive from prompt-injectable documents.
        """
        from financial_planner.tools.documents import read_pdf_text

        result = _call(read_pdf_text, path="/workspace/does-not-exist.pdf")
        assert "does not exist" in result["error"]
        assert str(WORKSPACE_DIR) not in result["error"]


class TestInspectDocument:
    def test_reports_row_count_and_columns(self, sample_csv):
        result = _call(inspect_document, path=sample_csv)
        assert result["row_count"] == 6
        assert [c["name"] for c in result["columns"]] == [
            "Date",
            "Description",
            "Category",
            "Amount",
        ]

    def test_preview_is_bounded(self, sample_csv):
        """A preview must never become a full dump of the file."""
        result = _call(inspect_document, path=sample_csv)
        assert len(result["preview"]) <= 5

    def test_unsupported_format_is_rejected(self):
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-notes.txt"
        path.write_text("hello", encoding="utf-8")
        try:
            result = _call(inspect_document, path="/workspace/_pytest-notes.txt")
            assert "unsupported table format" in result["error"]
        finally:
            path.unlink(missing_ok=True)


class TestSummarizeSpending:
    def test_totals_reconcile(self, sample_csv):
        result = _call(summarize_spending, path=sample_csv, amount_column="Amount")
        assert result["total_inflow"] == pytest.approx(8_400.00)
        assert result["total_outflow"] == pytest.approx(4_100.00)
        assert result["net"] == pytest.approx(4_300.00)

    def test_category_breakdown_reports_spending_as_positive(self, sample_csv):
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            category_column="Category",
        )
        assert result["by_category"]["Housing"] == pytest.approx(3_700.00)
        # Income is inflow, so it must not appear as a spending category.
        assert "Income" not in result["by_category"]

    def test_monthly_series_and_average(self, sample_csv):
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            date_column="Date",
        )
        assert set(result["by_month"]) == {"2026-01", "2026-02"}
        assert result["average_monthly_outflow"] == pytest.approx(2_050.00)
        assert result["average_monthly_inflow"] == pytest.approx(4_200.00)
        assert result["average_monthly_net"] == pytest.approx(2_150.00)
        assert result["months_covered"] == 2

    def test_each_month_reports_inflow_outflow_and_net(self, sample_csv):
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            date_column="Date",
        )
        assert result["by_month"]["2026-01"] == {
            "inflow": pytest.approx(4_200.00),
            "outflow": pytest.approx(2_000.00),
            "net": pytest.approx(2_200.00),
        }
        # February spends $100 more, so the two months must not be identical.
        assert result["by_month"]["2026-02"]["outflow"] == pytest.approx(2_100.00)


class TestDerivedRatios:
    """The arithmetic rule forbids the model dividing, so the tool must not
    force it to. A live run had the agent computing the savings rate and every
    category share itself because these keys did not exist.
    """

    def test_savings_rate_is_returned(self, sample_csv):
        result = _call(summarize_spending, path=sample_csv, amount_column="Amount")
        # (8400 - 4100) / 8400
        assert result["savings_rate"] == pytest.approx(0.5119, abs=1e-4)

    def test_savings_rate_is_negative_when_spending_exceeds_income(self):
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-overspend.csv"
        path.write_text(
            "Date,Amount\n2026-01-03,1000.00\n2026-01-04,-1500.00\n",
            encoding="utf-8",
        )
        try:
            result = _call(
                summarize_spending, path="/workspace/_pytest-overspend.csv", amount_column="Amount"
            )
            assert result["savings_rate"] == pytest.approx(-0.5)
        finally:
            path.unlink(missing_ok=True)

    def test_savings_rate_is_omitted_rather_than_dividing_by_zero(self):
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-noincome.csv"
        path.write_text("Date,Amount\n2026-01-04,-1500.00\n", encoding="utf-8")
        try:
            result = _call(
                summarize_spending, path="/workspace/_pytest-noincome.csv", amount_column="Amount"
            )
            assert "error" not in result
            assert "savings_rate" not in result
        finally:
            path.unlink(missing_ok=True)

    def test_category_monthly_average_and_income_share(self, sample_csv):
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            category_column="Category",
            date_column="Date",
        )
        # Housing is $3,700 over two months against $8,400 of income.
        assert result["by_category_monthly_average"]["Housing"] == pytest.approx(1_850.00)
        assert result["by_category_share_of_income"]["Housing"] == pytest.approx(0.4405, abs=1e-4)

    def test_period_span_is_reported_so_partial_months_are_visible(self, sample_csv):
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            date_column="Date",
        )
        assert result["period"]["first_transaction"] == "2026-01-03"
        assert result["period"]["last_transaction"] == "2026-02-05"

    def test_unknown_column_error_lists_the_real_columns(self, sample_csv):
        """The agent recovers from this by reading the available names."""
        result = _call(summarize_spending, path=sample_csv, amount_column="Nope")
        assert "not found" in result["error"]
        assert "Amount" in result["error"]

    def test_non_numeric_amount_column_is_rejected(self, sample_csv):
        result = _call(summarize_spending, path=sample_csv, amount_column="Description")
        assert "no parseable numbers" in result["error"]

    def test_no_raw_transactions_are_returned(self, sample_csv):
        """Summarize, never dump -- individual rows must stay out of context."""
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            category_column="Category",
        )
        assert "Paycheck" not in json.dumps(result)


class TestMonthlyAveragesFoundByReview:
    """Both bugs here produced plausible numbers that fed straight into a budget
    and, through the retirement skill, into retirement spending estimates.
    """

    def test_a_gap_month_does_not_inflate_the_average(self):
        """Denominator is the calendar span, not the count of active months.

        January and March at $1,200 each is $800/month over the quarter, not
        $1,200 -- February had no activity but it still happened.
        """
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-gap.csv"
        path.write_text(
            "Date,Amount\n2026-01-10,-1200.00\n2026-03-10,-1200.00\n",
            encoding="utf-8",
        )
        try:
            result = _call(
                summarize_spending,
                path="/workspace/_pytest-gap.csv",
                amount_column="Amount",
                date_column="Date",
            )
            assert result["months_covered"] == 3
            assert result["average_monthly_outflow"] == pytest.approx(800.00)
        finally:
            path.unlink(missing_ok=True)

    def test_undated_rows_do_not_inflate_the_average(self):
        """Averages and by_month must agree; they are read from one payload."""
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-undated.csv"
        path.write_text(
            "Date,Amount\n2026-01-03,3000.00\nUNKNOWN,3000.00\n2026-01-10,-1000.00\n",
            encoding="utf-8",
        )
        try:
            result = _call(
                summarize_spending,
                path="/workspace/_pytest-undated.csv",
                amount_column="Amount",
                date_column="Date",
            )
            # The total keeps every parseable amount...
            assert result["total_inflow"] == pytest.approx(6_000.00)
            # ...but the average must match what by_month actually shows.
            assert result["average_monthly_inflow"] == pytest.approx(3_000.00)
            assert result["by_month"]["2026-01"]["inflow"] == pytest.approx(3_000.00)
            assert result["undated_transactions"] == 1
            assert "averages_basis" in result
        finally:
            path.unlink(missing_ok=True)

    def test_no_undated_note_when_every_row_has_a_date(self, sample_csv):
        result = _call(
            summarize_spending, path=sample_csv, amount_column="Amount", date_column="Date"
        )
        assert "undated_transactions" not in result
        assert "averages_basis" not in result

    def test_category_monthly_average_uses_the_same_span(self):
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-catgap.csv"
        path.write_text(
            "Date,Category,Amount\n2026-01-10,Rent,-900.00\n2026-03-10,Rent,-900.00\n",
            encoding="utf-8",
        )
        try:
            result = _call(
                summarize_spending,
                path="/workspace/_pytest-catgap.csv",
                amount_column="Amount",
                category_column="Category",
                date_column="Date",
            )
            assert result["by_category_monthly_average"]["Rent"] == pytest.approx(600.00)
        finally:
            path.unlink(missing_ok=True)


class TestPdfPageRange:
    """A backwards range used to return an empty success envelope, which reads
    to the agent as "these pages are blank" rather than "fix your arguments".
    """

    @pytest.fixture
    def blank_pdf(self):
        from pypdf import PdfWriter

        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-doc.pdf"
        writer = PdfWriter()
        for _ in range(10):
            writer.add_blank_page(width=200, height=200)
        with path.open("wb") as handle:
            writer.write(handle)
        yield "/workspace/_pytest-doc.pdf"
        path.unlink(missing_ok=True)

    def test_end_before_start_is_an_error_not_empty_text(self, blank_pdf):
        from financial_planner.tools.documents import read_pdf_text

        result = _call(read_pdf_text, path=blank_pdf, start_page=5, end_page=2)
        assert "error" in result
        assert "before start_page" in result["error"]

    def test_start_beyond_the_document_is_still_an_error(self, blank_pdf):
        from financial_planner.tools.documents import read_pdf_text

        result = _call(read_pdf_text, path=blank_pdf, start_page=99)
        assert "exceeds page count" in result["error"]

    def test_a_valid_range_succeeds(self, blank_pdf):
        from financial_planner.tools.documents import read_pdf_text

        result = _call(read_pdf_text, path=blank_pdf, start_page=2, end_page=4)
        assert "error" not in result
        assert result["pages_read"] == "2-4"
        assert result["page_count"] == 10


@pytest.fixture
def csv_file():
    """Write arbitrary CSV content into the workspace and clean it up."""
    written = []

    def _write(name: str, content: str) -> str:
        ensure_directories()
        path = WORKSPACE_DIR / f"_pytest-{name}.csv"
        path.write_text(content, encoding="utf-8")
        written.append(path)
        return f"/workspace/{path.name}"

    yield _write
    for path in written:
        path.unlink(missing_ok=True)


class TestSignConventions:
    """Exports disagree about what a sign means and reading one wrong inverts
    the whole budget. A card export with positive charges used to report a 100%
    savings rate, an empty category breakdown and every charge as income.
    """

    CARD = (
        "Date,Category,Amount\n"
        "2026-01-05,Groceries,150.00\n"
        "2026-01-06,Fuel,50.00\n"
        "2026-01-20,Payment,-200.00\n"
    )

    SPLIT = (
        "Date,Category,Debit,Credit\n"
        "2026-01-03,Income,,4200.00\n"
        "2026-01-04,Housing,1850.00,\n"
        "2026-01-05,Groceries,150.00,\n"
    )

    def test_a_signed_export_is_detected_without_being_told(self, sample_csv):
        result = _call(summarize_spending, path=sample_csv, amount_column="Amount")
        assert result["sign_convention"] == "negative_outflow"

    def test_an_all_positive_column_is_refused_rather_than_guessed(self, csv_file):
        """The reported defect: this used to return savings_rate 1.0 silently."""
        path = csv_file("cardonly", "Date,Amount\n2026-01-05,150.00\n2026-01-06,50.00\n")
        result = _call(summarize_spending, path=path, amount_column="Amount")
        assert "AmbiguousSignConvention" in result["error"]
        assert "positive_outflow" in result["error"]

    def test_positive_outflow_reads_charges_as_spending(self, csv_file):
        path = csv_file("card", self.CARD)
        result = _call(
            summarize_spending,
            path=path,
            amount_column="Amount",
            category_column="Category",
            sign_convention="positive_outflow",
        )
        assert result["sign_convention"] == "positive_outflow"
        assert result["total_outflow"] == pytest.approx(200.00)
        assert result["total_inflow"] == pytest.approx(200.00)
        assert result["by_category"]["Groceries"] == pytest.approx(150.00)
        assert "Payment" not in result["by_category"]

    def test_the_two_readings_are_genuine_inverses(self, csv_file):
        """Guards the clip() branches against being written the same way twice."""
        path = csv_file("card", self.CARD)
        forced = _call(
            summarize_spending,
            path=path,
            amount_column="Amount",
            sign_convention="negative_outflow",
        )
        card = _call(
            summarize_spending,
            path=path,
            amount_column="Amount",
            sign_convention="positive_outflow",
        )
        assert forced["total_outflow"] == pytest.approx(card["total_inflow"])
        assert forced["total_inflow"] == pytest.approx(card["total_outflow"])

    def test_split_debit_and_credit_columns_are_read_as_magnitudes(self, csv_file):
        path = csv_file("split", self.SPLIT)
        result = _call(
            summarize_spending,
            path=path,
            amount_column="Debit",
            inflow_column="Credit",
            category_column="Category",
        )
        assert result["sign_convention"] == "split_columns"
        assert result["total_outflow"] == pytest.approx(2_000.00)
        assert result["total_inflow"] == pytest.approx(4_200.00)
        assert result["by_category"]["Housing"] == pytest.approx(1_850.00)

    def test_a_blank_side_of_a_split_row_is_kept_not_dropped(self, csv_file):
        """Blanks are how these exports encode direction; dropping them loses
        every transaction in the file."""
        path = csv_file("split", self.SPLIT)
        result = _call(summarize_spending, path=path, amount_column="Debit", inflow_column="Credit")
        assert result["transaction_count"] == 3

    def test_an_all_negative_export_is_read_as_pure_spending(self, csv_file):
        """The inverse reading would make it pure income, which no export is."""
        path = csv_file("allneg", "Date,Amount\n2026-01-05,-150.00\n2026-01-06,-50.00\n")
        result = _call(summarize_spending, path=path, amount_column="Amount")
        assert result["total_outflow"] == pytest.approx(200.00)
        assert result["total_inflow"] == pytest.approx(0.0)

    def test_an_unknown_convention_is_rejected(self, sample_csv):
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            sign_convention="whatever",
        )
        assert "sign_convention must be one of" in result["error"]

    def test_the_convention_applied_is_always_reported(self, sample_csv):
        """The skill checks this field to catch a reversed budget before writing one."""
        result = _call(summarize_spending, path=sample_csv, amount_column="Amount")
        assert result["sign_convention"] in (
            "negative_outflow",
            "positive_outflow",
            "split_columns",
        )

    def test_a_missing_inflow_column_is_named_in_the_error(self, sample_csv):
        result = _call(
            summarize_spending, path=sample_csv, amount_column="Amount", inflow_column="Credit"
        )
        assert "inflow_column" in result["error"]
        assert "Amount" in result["error"]


class TestBlankCategories:
    """`groupby` drops NaN keys by default, so uncategorized rows vanished from
    the breakdown while staying in total_outflow -- a breakdown that silently
    did not add up to the total printed beside it.
    """

    CONTENT = (
        "Date,Category,Amount\n"
        "2026-01-03,Income,4000.00\n"
        "2026-01-04,Housing,-1000.00\n"
        "2026-01-05,,-250.00\n"
        "2026-01-06,   ,-150.00\n"
    )

    def test_blank_categories_are_bucketed_not_dropped(self, csv_file):
        path = csv_file("blankcat", self.CONTENT)
        result = _call(
            summarize_spending, path=path, amount_column="Amount", category_column="Category"
        )
        assert result["by_category"]["Uncategorized"] == pytest.approx(400.00)

    def test_the_breakdown_reconciles_with_the_total(self, csv_file):
        path = csv_file("blankcat", self.CONTENT)
        result = _call(
            summarize_spending, path=path, amount_column="Amount", category_column="Category"
        )
        assert sum(result["by_category"].values()) == pytest.approx(result["total_outflow"])

    def test_whitespace_only_categories_join_the_same_bucket(self, csv_file):
        path = csv_file("blankcat", self.CONTENT)
        result = _call(
            summarize_spending, path=path, amount_column="Amount", category_column="Category"
        )
        assert "   " not in result["by_category"]
        assert set(result["by_category"]) == {"Housing", "Uncategorized"}

    def test_monthly_averages_include_the_bucket_too(self, csv_file):
        path = csv_file("blankcat", self.CONTENT)
        result = _call(
            summarize_spending,
            path=path,
            amount_column="Amount",
            category_column="Category",
            date_column="Date",
        )
        assert result["by_category_monthly_average"]["Uncategorized"] == pytest.approx(400.00)


class TestUnparseableDateColumn:
    def test_a_column_with_no_dates_is_reported_rather_than_returning_nothing(self, csv_file):
        """An empty by_month reads as 'no monthly pattern', not 'wrong column'."""
        path = csv_file("baddate", "Ref,Amount\nINV-001,4000.00\nINV-002,-1000.00\n")
        result = _call(summarize_spending, path=path, amount_column="Amount", date_column="Ref")
        assert "no parseable dates" in result["error"]


class TestSignConventionDefectsFoundByReview:
    """Every case here silently inverted or emptied a budget.

    They all reached the same failure the sign handling was written to stop --
    a plausible-looking payload with the spending and income sides swapped, or
    with spending missing entirely -- through routes the first version left open.
    """

    CARD = (
        "Date,Category,Amount\n"
        "2026-01-05,Groceries,300\n"
        "2026-01-06,Dining,200\n"
        "2026-01-07,Travel,500\n"
        "2026-01-20,Payment,-50\n"
    )

    SPLIT = (
        "Date,Description,Debit,Credit\n"
        "2026-01-03,Paycheck,,4200.00\n"
        "2026-01-04,Rent,1850.00,\n"
        "2026-01-05,Groceries,150.00,\n"
    )

    def test_split_columns_is_a_result_not_an_argument(self, sample_csv):
        """It was accepted, fell through to positive_outflow, and reported
        "split_columns" -- the label SKILL.md tells the agent to trust as its
        reversal check, on a budget that was exactly backwards."""
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            sign_convention="split_columns",
        )
        assert "error" in result
        assert "inflow_column" in result["error"]

    def test_a_wrong_debit_column_errors_instead_of_reporting_no_spending(self, csv_file):
        """The guard required *both* columns to be unparseable, so naming a text
        column as the debit side gave total_outflow 0 and savings_rate 1.0."""
        path = csv_file("splitwrong", self.SPLIT)
        result = _call(
            summarize_spending,
            path=path,
            amount_column="Description",
            inflow_column="Credit",
        )
        assert "no parseable numbers" in result["error"]

    def test_an_empty_credit_column_is_still_allowed(self, csv_file):
        """A statement period with no deposits is real; a wrong column is not."""
        path = csv_file(
            "nodeposits", "Date,Debit,Credit\n2026-01-04,1850.00,\n2026-01-05,150.00,\n"
        )
        result = _call(summarize_spending, path=path, amount_column="Debit", inflow_column="Credit")
        assert result["total_outflow"] == pytest.approx(2_000.00)
        assert result["total_inflow"] == pytest.approx(0.0)

    def test_the_same_column_twice_is_refused(self, csv_file):
        """It counted every transaction as spending and income at once, which
        reports a break-even household."""
        path = csv_file("dup", "Date,Amount\n2026-01-03,200\n2026-01-04,100\n")
        result = _call(
            summarize_spending, path=path, amount_column="Amount", inflow_column="Amount"
        )
        assert "at once" in result["error"]

    def test_a_card_export_with_a_payment_row_is_refused_not_inverted(self, csv_file):
        """The real hole in the first version. Both layouts produce mixed signs
        -- a checking export is few deposits against many payments, a card
        export is many charges against few payments -- so "any negative means
        the ordinary reading" read a normal Amex month backwards and reported
        $1,000 of charges as income against $50 of spending."""
        path = csv_file("card", self.CARD)
        result = _call(
            summarize_spending, path=path, amount_column="Amount", category_column="Category"
        )
        assert "AmbiguousSignConvention" in result["error"]
        assert "positive_outflow" in result["error"]

    def test_the_refusal_reports_the_counts_that_triggered_it(self, csv_file):
        path = csv_file("card", self.CARD)
        error = _call(summarize_spending, path=path, amount_column="Amount")["error"]
        assert "3 positive" in error
        assert "1 negative" in error

    def test_a_checking_export_with_irregular_income_still_passes(self, csv_file):
        """The threshold has to clear a real household. Two deposits against one
        large payment is 2:1 positive and must not trip the card heuristic."""
        path = csv_file(
            "irregular", "Date,Amount\n2026-01-03,3000\n2026-01-15,3000\n2026-01-20,-1000\n"
        )
        result = _call(summarize_spending, path=path, amount_column="Amount")
        assert result["sign_convention"] == "negative_outflow"
        assert result["total_outflow"] == pytest.approx(1_000.00)

    def test_an_inferred_convention_says_so(self, sample_csv):
        """The label read as a determination; it is an assumption."""
        result = _call(summarize_spending, path=sample_csv, amount_column="Amount")
        assert result["sign_convention_inferred"] is True
        assert "Assumed" in result["sign_convention_note"]

    def test_an_explicit_convention_is_not_flagged_as_inferred(self, sample_csv):
        result = _call(
            summarize_spending,
            path=sample_csv,
            amount_column="Amount",
            sign_convention="negative_outflow",
        )
        assert "sign_convention_inferred" not in result

    def test_split_columns_are_never_inferred(self, csv_file):
        path = csv_file("split", self.SPLIT)
        result = _call(summarize_spending, path=path, amount_column="Debit", inflow_column="Credit")
        assert "sign_convention_inferred" not in result


class TestCardExportsHaveNoIncome:
    """Under positive_outflow the inflow side is card payments, not earnings.

    A savings rate against it divides unrelated quantities, and SKILL.md names
    savings_rate as one of the three headline numbers to report -- so offering a
    meaningless one gets it stated to the user with confidence.
    """

    CARD = (
        "Date,Category,Amount\n"
        "2026-01-05,Groceries,1500\n"
        "2026-01-06,Dining,500\n"
        "2026-01-20,Payment,-500\n"
    )

    def test_no_savings_rate_is_offered(self, csv_file):
        path = csv_file("card", self.CARD)
        result = _call(
            summarize_spending,
            path=path,
            amount_column="Amount",
            sign_convention="positive_outflow",
        )
        assert "savings_rate" not in result

    def test_no_share_of_income_is_offered(self, csv_file):
        path = csv_file("card", self.CARD)
        result = _call(
            summarize_spending,
            path=path,
            amount_column="Amount",
            category_column="Category",
            sign_convention="positive_outflow",
        )
        assert "by_category_share_of_income" not in result
        assert result["by_category"]["Groceries"] == pytest.approx(1_500.00)

    def test_the_omission_is_explained_rather_than_silent(self, csv_file):
        path = csv_file("card", self.CARD)
        result = _call(
            summarize_spending,
            path=path,
            amount_column="Amount",
            sign_convention="positive_outflow",
        )
        assert "not income" in result["income_basis"]

    def test_a_checking_export_still_gets_its_savings_rate(self, sample_csv):
        assert "savings_rate" in _call(summarize_spending, path=sample_csv, amount_column="Amount")


class TestDateColumnDefectsFoundByReview:
    def test_a_numeric_column_is_refused_not_parsed_as_epoch(self, csv_file):
        """pandas reads plain integers as epoch nanoseconds, so invoice numbers
        parsed "successfully" into 1970-01-01 -- every row in one month, and the
        monthly averages quietly became the whole-file totals."""
        path = csv_file("epoch", "Ref,Amount\n1001,4000\n1002,-1000\n1003,-500\n")
        result = _call(summarize_spending, path=path, amount_column="Amount", date_column="Ref")
        assert "1970" in result["error"]

    def test_excel_date_serials_are_refused_with_advice(self, csv_file):
        path = csv_file("serial", "Date,Amount\n46023,4000\n46024,-1000\n46025,-500\n")
        result = _call(summarize_spending, path=path, amount_column="Amount", date_column="Date")
        assert "Excel date serials" in result["error"]

    def test_day_first_dates_surface_as_undated_rather_than_wrong_months(self, csv_file):
        """All three rows are February. Parsing each value on its own read the
        first as month-first and filed it under January, splitting one month's
        spending across two and halving the monthly average. A single inferred
        format leaves the non-matching rows visible instead."""
        path = csv_file(
            "dayfirst", "Date,Amount\n01/02/2026,-100\n13/02/2026,-200\n28/02/2026,-300\n"
        )
        result = _call(summarize_spending, path=path, amount_column="Amount", date_column="Date")
        assert result["undated_transactions"] == 2
        assert set(result["by_month"]) == {"2026-01"}
        assert result["averages_basis"]

    def test_a_per_element_fallback_is_reported(self, csv_file):
        """pandas still falls back on its own when it can infer nothing, so the
        fallback is detected and flagged rather than left to run silently."""
        path = csv_file("fallback", "Date,Amount\npending,-100\n2026-01-05,-200\n2026-02-05,-300\n")
        result = _call(summarize_spending, path=path, amount_column="Amount", date_column="Date")
        assert "may be unreliable" in result["date_parsing"]

    def test_a_clean_date_column_carries_no_parsing_note(self, sample_csv):
        result = _call(
            summarize_spending, path=sample_csv, amount_column="Amount", date_column="Date"
        )
        assert "date_parsing" not in result


class TestPdfIsNotATable:
    """The upload box invites a PDF, so the model reaches summarize_spending
    with one. The generic "unsupported table format" refusal it used to get was
    accurate and useless: it named the formats it wanted without naming a route
    forward, so the model retried with different column names, failed the same
    way, and answered without the numbers.
    """

    @pytest.fixture
    def statement_pdf(self):
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-statement.pdf"
        path.write_bytes(_pdf_bytes("Total monthly spending 412.00"))
        yield "/workspace/_pytest-statement.pdf"
        path.unlink(missing_ok=True)

    def test_the_refusal_names_its_own_type(self, statement_pdf):
        """`envelope.err` serializes the class name and the model picks its
        recovery off it, so a PDF must not arrive as a bare ValueError.
        """
        result = _call(summarize_spending, path=statement_pdf, amount_column="Amount")
        assert "NotTabular" in result["error"]

    def test_the_refusal_carries_both_routes_forward(self, statement_pdf):
        """Read the printed totals, or get a real export. A refusal that states
        neither is what sent the model back round the same failing call.
        """
        result = _call(summarize_spending, path=statement_pdf, amount_column="Amount")
        assert "read_pdf_text" in result["error"]
        assert "CSV or XLSX" in result["error"]

    def test_inspect_document_says_so_before_the_call_is_made(self, statement_pdf):
        """The skill tells the agent to run inspect_document before any other
        document tool, which makes this the earliest point the dead end is
        visible -- and it is a success payload, not an error.
        """
        result = _call(inspect_document, path=statement_pdf)
        assert "error" not in result
        assert "summarize_spending will refuse" in result["aggregation"]
        assert "read_pdf_text" in result["aggregation"]

    def test_a_pdf_is_still_readable(self, statement_pdf):
        """The fix must not turn a readable format into a rejected one."""
        from financial_planner.tools.documents import read_pdf_text

        result = _call(read_pdf_text, path=statement_pdf)
        assert "error" not in result
        assert result["page_count"] == 1

    def test_a_genuinely_unknown_format_keeps_the_generic_message(self):
        """Only .pdf earns a bespoke refusal; .txt has no route forward to name."""
        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-notes.txt"
        path.write_text("hello", encoding="utf-8")
        try:
            result = _call(
                summarize_spending, path="/workspace/_pytest-notes.txt", amount_column="Amount"
            )
            assert "unsupported table format" in result["error"]
            assert "NotTabular" not in result["error"]
        finally:
            path.unlink(missing_ok=True)

    def test_both_statements_carry_the_arithmetic_prohibition(self, statement_pdf):
        """The clause that had already gone missing from one copy of two.

        `inspect_document` is called first -- the system prompt and the skill
        both order it there -- so on the ordinary path the refusal's wording is
        never emitted at all, and "use read_pdf_text for the figures" with no
        prohibition beside it reads as "pull the lines out and total them".
        """
        refusal = _call(summarize_spending, path=statement_pdf, amount_column="Amount")["error"]
        note = _call(inspect_document, path=statement_pdf)["aggregation"]
        assert "doing the arithmetic yourself" in refusal
        assert "doing the arithmetic yourself" in note

    def test_both_statements_keep_the_pdf_untrusted(self, statement_pdf):
        """These routes send the agent to read figures off a PDF and report them
        as the household's numbers. A tampered statement printing "Total monthly
        spending: $412.00" is on that path, and provenance alone -- "say where it
        came from" -- makes the figure sourced, not doubted.
        """
        refusal = _call(summarize_spending, path=statement_pdf, amount_column="Amount")["error"]
        note = _call(inspect_document, path=statement_pdf)["aggregation"]
        assert "untrusted user data" in refusal
        assert "untrusted user data" in note

    def test_every_statement_ends_in_the_route_that_always_survives(self, statement_pdf):
        """`PDF_EXPORT_ROUTE` is live in every case -- rendered, scanned, locked
        -- which is why every statement of the dead end ends in it.

        `endswith` rather than `in`: containment leaves each site free to append
        a clause of its own, which is the divergence this exists to stop.
        """
        from financial_planner.tools.documents import PDF_EXPORT_ROUTE

        refusal = _call(summarize_spending, path=statement_pdf, amount_column="Amount")["error"]
        note = _call(inspect_document, path=statement_pdf)["aggregation"]
        assert refusal.endswith(PDF_EXPORT_ROUTE)
        assert note.endswith(PDF_EXPORT_ROUTE)

    def test_the_read_route_cannot_be_offered_without_its_caveats(self):
        """The point of splitting the text into clauses. Both caveats live
        inside the route they qualify, so a site that offers the route carries
        them by construction -- dropping one means dropping the whole route,
        which is a decision rather than an omission.
        """
        from financial_planner.tools.documents import PDF_READ_ROUTE

        assert "doing the arithmetic yourself" in PDF_READ_ROUTE
        assert "untrusted user data" in PDF_READ_ROUTE

    def test_a_rendered_statement_still_gets_the_read_route(self, statement_pdf):
        """The scanned and locked cases must not cost the ordinary one its
        route: text came out, so reading the printed totals is still on.
        """
        from financial_planner.tools.documents import PDF_READ_ROUTE

        assert PDF_READ_ROUTE in _call(inspect_document, path=statement_pdf)["aggregation"]

    def test_a_pdf_that_is_not_there_is_not_refused_as_a_pdf(self):
        """Nothing touched the disk before the suffix check, so a path that did
        not exist but ended in ".pdf" got the refusal -- a confident factual
        claim about a file that is not there, and then a route to read_pdf_text
        that could only fail on it.
        """
        ensure_directories()
        result = _call(
            summarize_spending, path="/workspace/_pytest-absent.pdf", amount_column="Amount"
        )
        assert "does not exist" in result["error"]
        assert "NotTabular" not in result["error"]
        assert str(WORKSPACE_DIR) not in result["error"]

    def test_the_skill_states_both_caveats_too(self):
        """The skill is the one copy no Python constant can reach, and it is read
        before either tool runs. Nothing but this fails when it drifts.
        """
        # Unwrapped first: the file is hard-wrapped prose, so a clause that is
        # present still straddles a newline. Rewrapping a paragraph is not drift.
        text = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())
        assert "arithmetic rule forbids" in text
        assert "untrusted user data" in text


class TestPdfThatCannotBeRead:
    """Two statements the tool set claims to support and could not act on: a
    scan, which extracts to nothing, and a password-protected file, which is how
    banks email them. Both used to end the same way -- no numbers, and nothing
    saying what to do about it.
    """

    @pytest.fixture
    def scanned_pdf(self):
        from pypdf import PdfWriter

        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-scanned.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with path.open("wb") as handle:
            writer.write(handle)
        yield "/workspace/_pytest-scanned.pdf"
        path.unlink(missing_ok=True)

    @pytest.fixture
    def locked_pdf(self):
        from pypdf import PdfWriter

        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-locked.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("hunter2")
        with path.open("wb") as handle:
            writer.write(handle)
        yield "/workspace/_pytest-locked.pdf"
        path.unlink(missing_ok=True)

    def test_a_scan_is_not_offered_the_read_route(self, scanned_pdf):
        """Naming a route that cannot work is the dead end one step along: the
        agent reads "report the totals it prints", finds none, and answers
        without the numbers.
        """
        from financial_planner.tools.documents import PDF_EXPORT_ROUTE, PDF_READ_ROUTE

        note = _call(inspect_document, path=scanned_pdf)["aggregation"]
        assert PDF_READ_ROUTE not in note
        assert "scan or a photo" in note
        assert note.endswith(PDF_EXPORT_ROUTE)

    def test_reading_a_scan_says_so_instead_of_returning_a_blank(self, scanned_pdf):
        """`text: ""` inside a success envelope is indistinguishable from a page
        that is genuinely blank, and `_is_error_result` reports the call as ok.
        """
        from financial_planner.tools.documents import read_pdf_text

        result = _call(read_pdf_text, path=scanned_pdf)
        assert "error" not in result
        assert result["text"] == ""
        assert "nothing here to quote" in result["note"]

    def test_a_locked_pdf_names_its_own_type_and_a_route(self, locked_pdf):
        """`envelope.err` serializes the class name and the model keys recovery
        off it, so this cannot arrive as pypdf's FileNotDecryptedError -- which
        says "File has not been decrypted" and nothing a model can act on.
        """
        from financial_planner.tools.documents import PDF_EXPORT_ROUTE

        result = _call(inspect_document, path=locked_pdf)
        assert "PdfLocked" in result["error"]
        assert "re-save it without the password" in result["error"]
        assert result["error"].endswith(PDF_EXPORT_ROUTE)

    def test_a_pdf_locked_only_against_editing_still_reads(self):
        """The regression the obvious guard would have caused. Banks lock the
        emailed statement against editing, which sets an owner password and
        leaves the user password empty; `is_encrypted` is True for those, but
        pypdf opens them with the empty password and the text reads out fine.
        Refusing on the flag would reject statements that already worked.
        """
        from pypdf import PdfWriter

        ensure_directories()
        path = WORKSPACE_DIR / "_pytest-noedit.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt(user_password="", owner_password="secret")
        with path.open("wb") as handle:
            writer.write(handle)
        try:
            result = _call(inspect_document, path="/workspace/_pytest-noedit.pdf")
            assert "error" not in result
            assert result["page_count"] == 1
        finally:
            path.unlink(missing_ok=True)

    def test_reading_a_locked_pdf_fails_the_same_way(self, locked_pdf):
        """Both readers open PDFs through one place, so the route cannot be
        stated at one of them and missing at the other.
        """
        from financial_planner.tools.documents import read_pdf_text

        result = _call(read_pdf_text, path=locked_pdf)
        assert "PdfLocked" in result["error"]
        assert "FileNotDecryptedError" not in result["error"]
