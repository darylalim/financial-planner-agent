"""Tests for storing user-uploaded documents.

The filename is browser-supplied and therefore attacker-controlled, so both the
traversal defence and the collision behaviour are security/data-loss relevant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from financial_planner.uploads import destination_for, save_uploads


class FakeUpload:
    """Stands in for Streamlit's UploadedFile (only .name and .getvalue used)."""

    def __init__(self, name: str, data: bytes = b"col\n1\n") -> None:
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


class TestTraversalIsContained:
    @pytest.mark.parametrize(
        "hostile",
        ["../../.env", "/etc/passwd", "../../../secrets.csv", "a/b/../../../../x.csv"],
    )
    def test_only_the_final_component_is_used(self, hostile, tmp_path):
        destination = destination_for(tmp_path, hostile)
        assert destination is not None
        assert destination.parent == tmp_path

    @pytest.mark.parametrize("useless", ["", ".", "..", "/"])
    def test_names_with_no_usable_component_are_refused(self, useless, tmp_path):
        assert destination_for(tmp_path, useless) is None


class TestCollisionsDoNotOverwrite:
    """Banks reuse fixed export filenames, so this is the common case, not an
    edge case: February's `statement.pdf` must not destroy January's.
    """

    def test_second_upload_of_the_same_name_is_suffixed(self, tmp_path):
        (tmp_path / "statement.pdf").write_bytes(b"january")
        destination = destination_for(tmp_path, "statement.pdf")
        assert destination == tmp_path / "statement-2.pdf"

    def test_suffix_increments_past_existing_suffixes(self, tmp_path):
        for name in ("statement.pdf", "statement-2.pdf", "statement-3.pdf"):
            (tmp_path / name).write_bytes(b"x")
        assert destination_for(tmp_path, "statement.pdf") == tmp_path / "statement-4.pdf"

    def test_the_original_file_survives(self, tmp_path):
        (tmp_path / "export.csv").write_bytes(b"january data")
        save_uploads([FakeUpload("export.csv", b"february data")], tmp_path)
        assert (tmp_path / "export.csv").read_bytes() == b"january data"
        assert (tmp_path / "export-2.csv").read_bytes() == b"february data"

    def test_saved_names_reflect_what_was_actually_written(self, tmp_path):
        """The agent is told these paths, so they must be the real ones."""
        (tmp_path / "export.csv").write_bytes(b"january")
        saved, _ = save_uploads([FakeUpload("export.csv")], tmp_path)
        assert saved == ["export-2.csv"]


class TestSaveUploads:
    def test_writes_content_and_returns_names(self, tmp_path):
        saved, skipped = save_uploads(
            [FakeUpload("a.csv", b"one"), FakeUpload("b.csv", b"two")], tmp_path
        )
        assert saved == ["a.csv", "b.csv"]
        assert skipped == []
        assert (tmp_path / "a.csv").read_bytes() == b"one"

    def test_unusable_names_are_skipped_not_fatal(self, tmp_path):
        saved, _ = save_uploads([FakeUpload(".."), FakeUpload("good.csv")], tmp_path)
        assert saved == ["good.csv"]

    def test_a_skipped_upload_is_reported_rather_than_swallowed(self, tmp_path):
        """Silently dropping one is data loss the user never hears about.

        The caller can only warn about a file that vanished if it is told which
        one, so the skipped name -- as uploaded, since nothing was written under
        any other -- comes back alongside the saved ones.
        """
        saved, skipped = save_uploads([FakeUpload(".."), FakeUpload("good.csv")], tmp_path)
        assert saved == ["good.csv"]
        assert skipped == [".."]

    def test_a_write_that_fails_is_a_skip_rather_than_a_raise(self, tmp_path, monkeypatch):
        """Regression: a failed write abandoned the batch and reached the UI raw.

        The files already on disk went unnamed -- so the agent was never told
        they existed -- and because this runs outside the turn's try/except, the
        traceback rendered unredacted, absolute host paths and all.
        """
        real_write = Path.write_bytes

        def _refuse_the_second(self, data):
            if self.name == "february.csv":
                raise PermissionError(str(self))
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", _refuse_the_second)
        saved, skipped = save_uploads(
            [FakeUpload("january.csv"), FakeUpload("february.csv")], tmp_path
        )

        assert saved == ["january.csv"]
        assert skipped == ["february.csv"]
        assert (tmp_path / "january.csv").exists()
