"""One-file Markdown export (title, metadata, summary, transcript) for sharing or indexing.

The file starts with YAML front matter so search/knowledge tools (Glean, Obsidian, ...)
can pick up the title, date and participants.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ownscribe.output.markdown import _format_time, format_transcript
from ownscribe.output.naming import folder_name, parse_folder_name, safe_title, stamp_for, unique_path
from ownscribe.transcription.models import TranscriptResult


def resolve_export_dir(setting: str, home: Path | None = None) -> Path | None:
    """Where exports go. "" = disabled; "auto" = a "Meeting Notes" folder in the work OneDrive."""
    setting = (setting or "").strip()
    if not setting:
        return None
    home = home or Path.home()
    if setting.lower() == "auto":
        cloud = home / "Library" / "CloudStorage"
        work_onedrives = (
            sorted(p for p in cloud.glob("OneDrive-*") if p.is_dir() and p.name != "OneDrive-Personal")
            if cloud.is_dir()
            else []
        )
        base = work_onedrives[0] if work_onedrives else home / "Documents"
        return base / "Meeting Notes"
    return Path(setting).expanduser()


def started_at(out_dir: Path) -> datetime:
    """Recording start time from the recording folder's name; else now."""
    parsed = parse_folder_name(out_dir.name)
    if parsed is not None:
        try:
            return parsed.started
        except ValueError:
            pass
    return datetime.now()


def export_filename(started: datetime, title: str) -> str:
    return folder_name(stamp_for(started), safe_title(title) or "Meeting") + ".md"


def _yaml(value: str) -> str:
    # JSON strings are valid YAML scalars and handle quoting/escaping for us.
    return json.dumps(value, ensure_ascii=False)


def format_export(
    *,
    title: str,
    started: datetime,
    result: TranscriptResult,
    summary: str | None,
    source: str = "",
    include_transcript: bool = True,
    bookmarks: list[float] | None = None,
) -> str:
    from ownscribe.output.bookmarks import clean_offsets, format_bookmarks_section

    bookmarks = clean_offsets(bookmarks, result.duration)
    speakers = []
    for seg in result.segments:
        if seg.speaker and seg.speaker not in speakers:
            speakers.append(seg.speaker)

    front = ["---", f"title: {_yaml(title)}", f"date: {started:%Y-%m-%d}", f"time: {_yaml(f'{started:%H:%M}')}"]
    if result.duration > 0:
        front.append(f"duration: {_yaml(_format_time(result.duration))}")
    if source:
        front.append(f"source: {_yaml(source)}")
    if speakers:
        front.append("speakers: [" + ", ".join(_yaml(s) for s in speakers) + "]")
    languages = list(dict.fromkeys(seg.language for seg in result.segments if seg.language))
    if len(languages) > 1:
        front.append("languages: [" + ", ".join(_yaml(code) for code in languages) + "]")
    if bookmarks:
        front.append(f"bookmarks: {len(bookmarks)}")
    front.append("tags: [meeting-notes]")
    front.append("---")

    body = [f"# {title}", ""]
    if summary and summary.strip():
        body += [summary.strip(), ""]
    if bookmarks:
        body += [format_bookmarks_section(result, bookmarks), ""]
    if include_transcript and result.segments:
        transcript = format_transcript(result, bookmarks).replace("# Transcript", "## Transcript", 1)
        body += [transcript.strip(), ""]

    return "\n".join(front) + "\n\n" + "\n".join(body).rstrip() + "\n"


def write_export(export_dir: Path, filename: str, content: str) -> Path:
    """Write *content* to export_dir/filename, never overwriting an existing file."""
    export_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(export_dir / filename, ".md")
    path.write_text(content)
    return path
