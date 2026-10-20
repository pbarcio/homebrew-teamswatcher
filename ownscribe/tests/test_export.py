"""Tests for the one-file Markdown export."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ownscribe.output.export import (
    export_filename,
    format_export,
    resolve_export_dir,
    started_at,
    write_export,
)
from ownscribe.transcription.models import Segment, TranscriptResult


def _result() -> TranscriptResult:
    return TranscriptResult(
        segments=[
            Segment(text="Who owns the cutover?", start=0.0, end=2.0, speaker="Them"),
            Segment(text="I do.", start=2.5, end=3.0, speaker="Pierce Barcio"),
        ],
        language="en",
        duration=2832.0,
    )


class TestResolveExportDir:
    def test_disabled(self, tmp_path):
        assert resolve_export_dir("", home=tmp_path) is None

    def test_explicit_path(self, tmp_path):
        assert resolve_export_dir(str(tmp_path / "notes"), home=tmp_path) == tmp_path / "notes"

    def test_auto_prefers_work_onedrive(self, tmp_path):
        cloud = tmp_path / "Library" / "CloudStorage"
        (cloud / "OneDrive-Personal").mkdir(parents=True)
        (cloud / "OneDrive-Acme Corp").mkdir()
        assert resolve_export_dir("auto", home=tmp_path) == cloud / "OneDrive-Acme Corp" / "Meeting Notes"

    def test_auto_falls_back_to_documents(self, tmp_path):
        assert resolve_export_dir("auto", home=tmp_path) == tmp_path / "Documents" / "Meeting Notes"


class TestNaming:
    def test_started_at_from_output_dir(self):
        assert started_at(Path("2026-10-19 1405 - ERP sync")) == datetime(2026, 10, 19, 14, 5)
        assert started_at(Path("2026-10-19_1405_erp-sync")) == datetime(2026, 10, 19, 14, 5)

    def test_filename_is_safe(self):
        name = export_filename(datetime(2026, 10, 19, 14, 5), 'ERP / CRM: "cutover" plan?')
        assert name == "2026-10-19 1405 - ERP CRM cutover plan.md"

    def test_empty_title(self):
        assert export_filename(datetime(2026, 10, 19, 14, 5), "  ") == "2026-10-19 1405 - Meeting.md"


class TestFormat:
    def test_front_matter_summary_and_transcript(self):
        text = format_export(
            title='ERP "cutover" sync',
            started=datetime(2026, 10, 19, 14, 5),
            result=_result(),
            summary="## Summary\nWe planned the cutover.",
            source="Microsoft Teams",
        )
        lines = text.splitlines()
        assert lines[0] == "---"
        assert 'title: "ERP \\"cutover\\" sync"' in lines
        assert "date: 2026-10-19" in lines
        assert 'time: "14:05"' in lines
        assert 'duration: "47:12"' in lines
        assert 'source: "Microsoft Teams"' in lines
        assert 'speakers: ["Them", "Pierce Barcio"]' in lines
        assert '# ERP "cutover" sync' in lines
        assert "## Summary" in lines
        assert "## Transcript" in lines
        assert "# Transcript" not in lines
        assert "**Pierce Barcio** [00:02]" in text

    def test_without_transcript(self):
        text = format_export(
            title="Sync",
            started=datetime(2026, 10, 19, 14, 5),
            result=_result(),
            summary="## Summary\nShort.",
            include_transcript=False,
        )
        assert "## Transcript" not in text
        assert "source:" not in text


class TestWrite:
    def test_never_overwrites(self, tmp_path):
        first = write_export(tmp_path / "out", "a.md", "one")
        second = write_export(tmp_path / "out", "a.md", "two")
        assert first.read_text() == "one"
        assert second.name == "a (2).md"
        assert second.read_text() == "two"


def test_languages_in_front_matter_for_mixed_calls():
    result = TranscriptResult(
        segments=[
            Segment(text="Hello.", start=0.0, end=1.0, speaker="Them", language="en"),
            Segment(text="Hola.", start=1.0, end=2.0, speaker="Them", language="es"),
        ],
        duration=2.0,
    )
    text = format_export(title="Sync", started=datetime(2026, 10, 19, 14, 5), result=result, summary=None)
    assert 'languages: ["en", "es"]' in text.splitlines()
    single = format_export(title="Sync", started=datetime(2026, 10, 19, 14, 5), result=_result(), summary=None)
    assert "languages:" not in single
