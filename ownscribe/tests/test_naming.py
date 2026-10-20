"""Tests for recording folder names."""

from __future__ import annotations

from datetime import datetime

from ownscribe.output.naming import (
    folder_name,
    parse_folder_name,
    safe_title,
    slug_to_title,
    stamp_for,
    unique_path,
)
from ownscribe.search import _parse_folder_name


class TestParse:
    def test_new_style(self):
        parsed = parse_folder_name("2026-10-19 1405 - Spec handoff")
        assert (parsed.date, parsed.hour, parsed.minute, parsed.title, parsed.legacy) == (
            "2026-10-19", "14", "05", "Spec handoff", False
        )
        assert parsed.started == datetime(2026, 10, 19, 14, 5)

    def test_untitled_and_duplicates(self):
        assert parse_folder_name("2026-10-19 1405").title is None
        assert parse_folder_name("2026-10-19 1405 (2)").title is None
        assert parse_folder_name("2026-10-19 1405 - Sync (2)").title == "Sync"

    def test_title_with_dashes(self):
        assert parse_folder_name("2026-10-19 1405 - MAWM - MIF cutover").title == "MAWM - MIF cutover"

    def test_legacy_style(self):
        parsed = parse_folder_name("2026-02-13_1501_quarterly-planning")
        assert parsed.legacy and parsed.title == "Quarterly planning"
        assert parse_folder_name("2026-02-13_1501").title is None

    def test_not_a_recording_folder(self):
        for name in ("Exports", "notes", "2026-10-19", ".DS_Store"):
            assert parse_folder_name(name) is None


class TestFormat:
    def test_folder_name(self):
        assert folder_name("2026-10-19 1405", "Spec handoff") == "2026-10-19 1405 - Spec handoff"
        assert folder_name("2026-10-19 1405", "") == "2026-10-19 1405"
        assert folder_name("2026-10-19 1405", 'MAWM / MIF: "cutover"?') == "2026-10-19 1405 - MAWM MIF cutover"

    def test_stamp_and_titles(self):
        assert stamp_for(datetime(2026, 1, 2, 3, 4)) == "2026-01-02 0304"
        assert safe_title("  a  b.  ") == "a b"
        assert slug_to_title("budget-review") == "Budget review"


class TestUniquePath:
    def test_folders_and_files(self, tmp_path):
        (tmp_path / "2026-10-19 1405 - v1.2 review").mkdir()
        assert unique_path(tmp_path / "2026-10-19 1405 - v1.2 review").name == "2026-10-19 1405 - v1.2 review (2)"
        (tmp_path / "a.md").write_text("x")
        assert unique_path(tmp_path / "a.md", ".md").name == "a (2).md"
        assert unique_path(tmp_path / "b.md", ".md").name == "b.md"


def test_meeting_search_understands_new_names():
    assert _parse_folder_name("2026-02-13 1501 - Quarterly planning") == (
        "2026-02-13", "2026-02-13 15:01 — Quarterly planning"
    )
    assert _parse_folder_name("2026-02-13 1501") == ("2026-02-13", "2026-02-13 15:01")
    assert _parse_folder_name("2026-02-13_1501_quarterly-planning") == (
        "2026-02-13", "2026-02-13 15:01 — Quarterly Planning"
    )
