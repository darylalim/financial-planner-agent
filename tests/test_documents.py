"""Tests for document ingestion and the path sandbox.

The sandbox tests matter more than the parsing tests: these tools take paths
straight from model output, and a prompt injection hidden in an uploaded PDF is
a realistic route to a traversal attempt.
"""

from __future__ import annotations

import json

import pytest

from financial_planner.config import WORKSPACE_DIR, ensure_directories
from financial_planner.tools.documents import (
    inspect_document,
    summarize_spending,
)

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
