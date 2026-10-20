"""Me-vs-them speaker labels from separately recorded mic and system-audio tracks.

With ``--split-tracks`` the audio helper saves, next to ``recording.wav``, an aligned
``recording.me.wav`` (your microphone) and ``recording.them.wav`` (everyone else, from
system audio). Transcribing each track separately and interleaving the segments by time
gives reliable "who said what" labels without a speaker-diarization model.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Callable
from pathlib import Path

from ownscribe.transcription.models import Segment, TranscriptResult

logger = logging.getLogger(__name__)

# Segments quieter than this (RMS, dBFS) are almost always Whisper hallucinating on
# silence ("Thank you.", "Thanks for watching!") rather than real speech.
QUIET_SEGMENT_DBFS = -50.0

# A mic segment that overlaps a system-audio segment in time and says nearly the same
# thing is the call audio leaking from the speakers into the mic, not you speaking.
ECHO_TEXT_SIMILARITY = 0.6
ECHO_MIN_OVERLAP = 0.5

_WORD_RE = re.compile(r"[a-z0-9']+")


def split_track_paths(audio_path: Path) -> tuple[Path, Path]:
    """(me, them) track paths for a merged recording path."""
    stem = audio_path.with_suffix("")
    return stem.with_name(stem.name + ".me.wav"), stem.with_name(stem.name + ".them.wav")


def has_split_tracks(audio_path: Path) -> bool:
    """True when at least one of the per-source tracks exists for *audio_path*."""
    me, them = split_track_paths(audio_path)
    return me.exists() or them.exists()


def segment_dbfs(samples, sample_rate: int, start: float, end: float) -> float:
    """RMS level in dBFS of samples[start:end] (seconds). -inf for empty/silent spans."""
    import numpy as np

    lo = max(0, int(start * sample_rate))
    hi = min(len(samples), int(end * sample_rate))
    if hi <= lo:
        return float("-inf")
    chunk = np.asarray(samples[lo:hi], dtype=np.float64)
    rms = float(np.sqrt(np.mean(chunk * chunk)))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * float(np.log10(rms))


def drop_quiet_segments(
    segments: list[Segment], track_path: Path, threshold_dbfs: float = QUIET_SEGMENT_DBFS
) -> list[Segment]:
    """Remove segments whose audio in *track_path* is too quiet to be real speech."""
    try:
        import numpy as np
        import soundfile as sf

        samples, sample_rate = sf.read(track_path, dtype="float32", always_2d=False)
        if getattr(samples, "ndim", 1) > 1:
            samples = np.mean(samples, axis=1)
    except Exception:  # never lose a transcript over the filter
        logger.debug("Could not read %s for the quiet-segment filter", track_path, exc_info=True)
        return segments

    kept = []
    for seg in segments:
        level = segment_dbfs(samples, sample_rate, seg.start, seg.end)
        if level >= threshold_dbfs:
            kept.append(seg)
        else:
            logger.debug("Dropping quiet segment (%.1f dBFS): %r", level, seg.text)
    return kept


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _overlap_fraction(a: Segment, b: Segment) -> float:
    """Fraction of segment *a*'s duration that overlaps *b*."""
    duration = a.end - a.start
    if duration <= 0:
        return 0.0
    overlap = min(a.end, b.end) - max(a.start, b.start)
    return max(0.0, overlap) / duration


def drop_echo(
    me_segments: list[Segment],
    them_segments: list[Segment],
    similarity: float = ECHO_TEXT_SIMILARITY,
    min_overlap: float = ECHO_MIN_OVERLAP,
) -> list[Segment]:
    """Remove mic segments that are just the other side of the call leaking into the mic."""
    kept = []
    for mine in me_segments:
        mine_text = _normalize(mine.text)
        is_echo = False
        if mine_text:
            for theirs in them_segments:
                if theirs.start >= mine.end or theirs.end <= mine.start:
                    continue
                if _overlap_fraction(mine, theirs) < min_overlap:
                    continue
                ratio = difflib.SequenceMatcher(None, mine_text, _normalize(theirs.text)).ratio()
                if ratio >= similarity:
                    is_echo = True
                    break
        if is_echo:
            logger.debug("Dropping echoed mic segment: %r", mine.text)
        else:
            kept.append(mine)
    return kept


def label(segments: list[Segment], speaker: str) -> list[Segment]:
    """Set *speaker* on every segment and word."""
    for seg in segments:
        seg.speaker = speaker
        for word in seg.words:
            word.speaker = speaker
    return segments


def merge_tracks(me: TranscriptResult | None, them: TranscriptResult | None) -> TranscriptResult:
    """Interleave labelled segments from both tracks by start time."""
    segments = [*(me.segments if me else []), *(them.segments if them else [])]
    segments.sort(key=lambda seg: (seg.start, seg.end))
    codes: list[str] = []
    for result in (them, me):
        for code in (result.language if result else "").split(","):
            if code.strip() and code.strip() not in codes:
                codes.append(code.strip())
    language = ", ".join(codes)
    duration = max(me.duration if me else 0.0, them.duration if them else 0.0)
    return TranscriptResult(segments=segments, language=language, duration=duration)


def transcribe_tracks(
    transcribe: Callable[[Path], TranscriptResult],
    audio_path: Path,
    me_label: str,
    them_label: str,
) -> TranscriptResult | None:
    """Transcribe the me/them tracks of *audio_path* and return one labelled transcript.

    Returns None when neither track exists, so callers fall back to the merged file.
    """
    me_path, them_path = split_track_paths(audio_path)
    if not me_path.exists() and not them_path.exists():
        return None

    me_result = transcribe(me_path) if me_path.exists() else None
    them_result = transcribe(them_path) if them_path.exists() else None

    if them_result is not None:
        them_result.segments = label(drop_quiet_segments(them_result.segments, them_path), them_label)
    if me_result is not None:
        me_segments = drop_quiet_segments(me_result.segments, me_path)
        if them_result is not None:
            me_segments = drop_echo(me_segments, them_result.segments)
        me_result.segments = label(me_segments, me_label)

    return merge_tracks(me_result, them_result)
