from __future__ import annotations

import json
from datetime import datetime

from ownscribe.output.bookmarks import (
    anchor_index,
    annotated_speaker_text,
    clean_offsets,
    format_bookmarks_section,
)
from ownscribe.output.export import format_export
from ownscribe.output.markdown import format_transcript, parse_transcript
from ownscribe.transcription.models import Segment, TranscriptResult


def _result() -> TranscriptResult:
    return TranscriptResult(
        segments=[
            Segment("Morning, everyone.", 0.0, 3.0, speaker="Pierce"),
            Segment("The cutover moves to March 15th.", 10.0, 14.0, speaker="Them"),
            Segment("And the vendor confirms on Friday.", 14.5, 18.0, speaker="Them"),
            Segment("Got it, I'll update the plan.", 30.0, 33.0, speaker="Pierce"),
        ],
        duration=40.0,
    )


def test_clean_offsets_sorts_dedupes_and_clamps():
    assert clean_offsets([20.0, -5, 21.0, 100.0, 50.0], duration=40.0) == [0.0, 20.0, 40.0]


def test_anchor_reaches_back_for_reaction_time():
    segments = _result().segments
    # Pressed 1 s into the cutover line: still that line.
    assert anchor_index(segments, 11.0) == 0  # 11 - 2 s = 9 s, before the line started
    assert anchor_index(segments, 13.0) == 1
    assert anchor_index(segments, 31.0) == 2  # 29 s: the vendor line was the last one spoken
    assert anchor_index([], 5.0) is None


def test_transcript_marker_round_trips():
    result = _result()
    text = format_transcript(result, [13.0])
    assert "> ★ Bookmark [00:13]" in text
    lines = text.splitlines()
    marker = lines.index("> ★ Bookmark [00:13]")
    following = next(line for line in lines[marker + 1 :] if line.strip())
    assert following.startswith("**Them** [00:10]")
    parsed = parse_transcript(text)
    assert [s.text for s in parsed.segments] == [s.text for s in result.segments]


def test_transcript_without_bookmarks_is_unchanged():
    result = _result()
    assert format_transcript(result) == format_transcript(result, [])


def test_bookmarks_section_quotes_the_moment():
    section = format_bookmarks_section(_result(), [13.0])
    assert section.startswith("## Bookmarks")
    # Short line: the speaker's next line is included for context.
    assert "- **[00:10]** Them: The cutover moves to March 15th. And the vendor confirms on Friday." in section
    assert format_bookmarks_section(_result(), []) == ""


def test_summary_input_stars_bookmarked_lines():
    text = annotated_speaker_text(_result(), [15.5], me="Pierce")
    assert "bookmarked by Pierce" in text
    lines = text.split("\n\n", 1)[1].splitlines()
    assert lines == [
        "Pierce: Morning, everyone.",
        "★ Them: The cutover moves to March 15th. And the vendor confirms on Friday.",
        "Pierce: Got it, I'll update the plan.",
    ]
    # Without bookmarks it's the plain speaker text.
    assert annotated_speaker_text(_result(), []) == _result().speaker_text


def test_bookmark_mid_turn_starts_a_new_starred_line():
    text = annotated_speaker_text(_result(), [16.6])
    body = text.split("\n\n", 1)[1].splitlines()
    assert body[1] == "Them: The cutover moves to March 15th."
    assert body[2] == "★ Them: And the vendor confirms on Friday."


def test_export_includes_bookmarks():
    content = format_export(
        title="Cutover sync",
        started=datetime(2026, 10, 20, 9, 30),
        result=_result(),
        summary=None,
        bookmarks=[13.0, 31.0],
    )
    assert "bookmarks: 2" in content
    assert content.index("## Bookmarks") < content.index("## Transcript")
    assert content.count("> ★ Bookmark") == 2


def test_runtime_options_carry_title_and_bookmarks(tmp_path, monkeypatch):
    from ownscribe.config import Config
    from ownscribe.pipeline import _apply_runtime_options

    options = tmp_path / "options.json"
    options.write_text(json.dumps({"title": " Weekly cutover sync ", "bookmarks": [1700000000.5, "x", True]}))
    monkeypatch.setenv("OWNSCRIBE_OPTIONS_FILE", str(options))
    extras = _apply_runtime_options(Config())
    assert extras.title == "Weekly cutover sync"
    assert extras.bookmarks == [1700000000.5]


def test_runtime_options_without_file(monkeypatch):
    from ownscribe.config import Config
    from ownscribe.pipeline import _apply_runtime_options

    monkeypatch.delenv("OWNSCRIBE_OPTIONS_FILE", raising=False)
    extras = _apply_runtime_options(Config())
    assert extras.title == "" and extras.bookmarks == []


def test_pipeline_uses_window_title_and_bookmarks(tmp_path):
    from unittest import mock

    from ownscribe.config import Config
    from ownscribe.pipeline import _do_transcribe_and_summarize

    config = Config()
    config.output.dir = str(tmp_path / "out")
    config.output.keep_recording = True
    config.summarization.enabled = True
    config.speakers.mode = "none"
    config.speakers.me = "Pierce"
    out_dir = tmp_path / "out" / "2026-10-20 0930"
    out_dir.mkdir(parents=True)
    audio_path = out_dir / "recording.wav"
    audio_path.touch()

    transcriber = mock.MagicMock()
    transcriber.transcribe.return_value = _result()
    summarizer = mock.MagicMock()
    summarizer.is_available.return_value = True
    summarizer.summarize.return_value = "- Cutover moves to March 15th."
    summarizer.generate_title.return_value = "AI title"

    with (
        mock.patch("ownscribe.pipeline._create_transcriber", return_value=transcriber),
        mock.patch("ownscribe.pipeline.create_summarizer", return_value=summarizer),
        mock.patch("ownscribe.pipeline._download_summarization_model"),
    ):
        _do_transcribe_and_summarize(
            config, audio_path, out_dir, meeting_title="Weekly cutover sync", bookmarks=[13.0]
        )

    final_dir = tmp_path / "out" / "2026-10-20 0930 - Weekly cutover sync"
    assert final_dir.is_dir()
    summarizer.generate_title.assert_not_called()
    assert "★ Them: The cutover moves to March 15th." in summarizer.summarize.call_args[0][0]
    assert "> ★ Bookmark [00:13]" in (final_dir / "transcript.md").read_text()
    notes = (final_dir / "2026-10-20 0930 - Weekly cutover sync.md").read_text()
    assert "# Weekly cutover sync" in notes and "## Bookmarks" in notes
