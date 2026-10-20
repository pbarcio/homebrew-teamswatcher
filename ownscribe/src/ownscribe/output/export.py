"""One-file Markdown export (title, metadata, summary, transcript) for sharing or indexing.

The file starts with YAML front matter so search/knowledge tools (Glean, Obsidian, ...)
can pick up the title, date and participants.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ownscribe.output.markdown import _format_time, format_transcript
from ownscribe.transcription.models import TranscriptResult

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_OUTPUT_DIR_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})")


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
    """Recording start time from an output folder named YYYY-MM-DD_HHMM[...]; else now."""
    if m := _OUTPUT_DIR_STAMP.match(out_dir.name):
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H%M")
        except ValueError:
            pass
    return datetime.now()


def export_filename(started: datetime, title: str) -> str:
    safe_title = _UNSAFE_FILENAME_CHARS.sub(" ", title).strip(" .") or "Meeting"
    safe_title = re.sub(r"\s+", " ", safe_title)[:80].rstrip(" .")
    return f"{started:%Y-%m-%d %H%M} - {safe_title}.md"


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
) -> str:
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
    front.append("tags: [meeting-notes]")
    front.append("---")

    body = [f"# {title}", ""]
    if summary and summary.strip():
        body += [summary.strip(), ""]
    if include_transcript and result.segments:
        transcript = format_transcript(result).replace("# Transcript", "## Transcript", 1)
        body += [transcript.strip(), ""]

    return "\n".join(front) + "\n\n" + "\n".join(body).rstrip() + "\n"


def write_export(export_dir: Path, filename: str, content: str) -> Path:
    """Write *content* to export_dir/filename, never overwriting an existing file."""
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / filename
    n = 2
    while path.exists():
        path = export_dir / f"{Path(filename).stem} ({n}).md"
        n += 1
    path.write_text(content)
    return path
