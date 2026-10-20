"""Bookmarks: moments the user marked as important while recording (TeamsWatcher's
"Bookmark This Moment").

A bookmark is an offset in seconds from the start of the recording. People press the
shortcut just after hearing something, so each bookmark points at the segment that was
being spoken a couple of seconds earlier.
"""

from __future__ import annotations

from ownscribe.transcription.models import Segment, TranscriptResult

# How far back from the key press a bookmark reaches.
REACTION_SECONDS = 2.0

SUMMARY_HINT = (
    "Lines starting with ★ were bookmarked by {me} during the call as important. "
    "Make sure the summary covers each of them."
)


def _format_time(seconds: float) -> str:
    m, s = divmod(int(max(0.0, seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def clean_offsets(offsets: list[float] | None, duration: float = 0.0) -> list[float]:
    """Sorted, de-duplicated (within 3 s), non-negative offsets, clamped to the duration."""
    result: list[float] = []
    for value in sorted(float(v) for v in (offsets or []) if isinstance(v, int | float)):
        value = max(0.0, value)
        if duration > 0:
            value = min(value, duration)
        if result and value - result[-1] < 3.0:
            continue
        result.append(value)
    return result


def anchor_index(segments: list[Segment], offset: float) -> int | None:
    """Index of the segment a bookmark at *offset* points at, or None without segments."""
    spoken = [i for i, seg in enumerate(segments) if seg.text.strip()]
    if not spoken:
        return None
    target = offset - REACTION_SECONDS
    best = spoken[0]
    for i in spoken:
        if segments[i].start <= target:
            best = i
        else:
            break
    return best


def anchors(segments: list[Segment], offsets: list[float]) -> dict[int, list[float]]:
    """Segment index -> the bookmark offsets pointing at it."""
    found: dict[int, list[float]] = {}
    for offset in offsets:
        index = anchor_index(segments, offset)
        if index is not None:
            found.setdefault(index, []).append(offset)
    return found


def marker_line(offset: float) -> str:
    return f"> ★ Bookmark [{_format_time(offset)}]"


def is_marker_line(line: str) -> bool:
    return line.lstrip().startswith("> ★")


def _excerpt(segments: list[Segment], index: int, max_chars: int = 240) -> str:
    """The anchor segment's text, plus the next one from the same speaker if it is short."""
    seg = segments[index]
    text = seg.text.strip()
    if len(text.split()) < 15 and index + 1 < len(segments):
        nxt = segments[index + 1]
        if nxt.speaker == seg.speaker and nxt.text.strip():
            text = f"{text} {nxt.text.strip()}"
    if len(text) > max_chars:
        text = text[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return text


def format_bookmarks_section(result: TranscriptResult, offsets: list[float]) -> str:
    """A '## Bookmarks' list for the notes file; '' when there are none."""
    offsets = clean_offsets(offsets)
    if not offsets:
        return ""
    lines = ["## Bookmarks", ""]
    for offset in offsets:
        index = anchor_index(result.segments, offset)
        if index is None:
            lines.append(f"- **[{_format_time(offset)}]**")
            continue
        seg = result.segments[index]
        who = f"{seg.speaker}: " if seg.speaker else ""
        lines.append(f"- **[{_format_time(seg.start)}]** {who}{_excerpt(result.segments, index)}")
    return "\n".join(lines)


def annotated_speaker_text(result: TranscriptResult, offsets: list[float], me: str = "the user") -> str:
    """result.speaker_text, with bookmarked lines starting with ★ and a note explaining them."""
    offsets = clean_offsets(offsets)
    if not offsets:
        return result.speaker_text
    marked = anchors(result.segments, offsets)
    if not result.has_speakers:
        parts = [("★ " if i in marked else "") + seg.text.strip() for i, seg in enumerate(result.segments)]
        body = " ".join(p for p in parts if p.strip("★ "))
    else:
        lines: list[str] = []
        current: str | None = None
        parts: list[str] = []
        starred = False
        for i, seg in enumerate(result.segments):
            text = seg.text.strip()
            if not text:
                continue
            # A bookmark starts a new line so the ★ sits right before what was marked.
            if parts and (seg.speaker != current or i in marked):
                lines.append(f"{'★ ' if starred else ''}{current or 'Unknown'}: {' '.join(parts)}")
                parts, starred = [], False
            current = seg.speaker
            starred = starred or i in marked
            parts.append(text)
        if parts:
            lines.append(f"{'★ ' if starred else ''}{current or 'Unknown'}: {' '.join(parts)}")
        body = "\n".join(lines)
    return SUMMARY_HINT.format(me=me) + "\n\n" + body
