"""Transcription for calls that switch between languages (e.g. English and Spanish).

Whisper normally detects one language per file (from its first 30 seconds) and then
transcribes everything as that language, so later speech in the other language comes
out translated or garbled. Here each track is instead:

1. split into stretches of speech at natural pauses,
2. checked for its language stretch by stretch (only among the allowed languages),
3. regrouped into windows of up to ~28 seconds of consecutive same-language speech,
4. transcribed window by window, each in its own language.

Everything here is plain numpy plus two injected callables, so it can be tested without
a Whisper model; the MLX backend supplies the real detector and transcriber.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from ownscribe.transcription.models import Segment, TranscriptResult

# Speech detection
_FRAME_SECONDS = 0.03
_SPEECH_FLOOR_DBFS = -50.0  # never treat quieter audio as speech (matches the quiet-segment filter)
_ABOVE_NOISE_DB = 12.0  # speech must be this much louder than the track's noise floor
_BELOW_TYPICAL_DB = 6.0  # ...or at most this much below the median level (nonstop talking)
_MERGE_GAP_SECONDS = 0.5  # pauses shorter than this don't split speech
_PAD_SECONDS = 0.25
_MIN_CHUNK_SECONDS = 0.4

# Whisper's window is 30 s; stay under it so nothing is cut off.
MAX_WINDOW_SECONDS = 28.0
# Don't bridge long silences inside one window (they invite hallucinated text).
_MAX_GAP_IN_WINDOW_SECONDS = 3.0

# Language choice for short, ambiguous stretches ("OK", "sí", a name)
_SHORT_CHUNK_SECONDS = 2.0
_CONFIDENT_SHARE = 0.75


@dataclass
class Chunk:
    start: float
    end: float
    language: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


def _frame_dbfs(samples: np.ndarray, frame_len: int) -> np.ndarray:
    n = len(samples) // frame_len
    if n == 0:
        return np.zeros(0)
    frames = samples[: n * frame_len].astype(np.float64).reshape(n, frame_len)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return 20.0 * np.log10(rms + 1e-10)


def speech_chunks(samples: np.ndarray, sample_rate: int, max_seconds: float = MAX_WINDOW_SECONDS) -> list[Chunk]:
    """Stretches of speech separated by pauses, none longer than *max_seconds*."""
    frame_len = max(1, int(sample_rate * _FRAME_SECONDS))
    db = _frame_dbfs(np.asarray(samples), frame_len)
    if db.size == 0:
        return []
    # Speech must clear the noise floor, but when someone talks almost nonstop the "floor"
    # is speech itself, so never require more than a bit below the median level.
    noise_floor = float(np.percentile(db, 10))
    typical = float(np.percentile(db, 50))
    threshold = max(_SPEECH_FLOOR_DBFS, min(noise_floor + _ABOVE_NOISE_DB, typical - _BELOW_TYPICAL_DB))
    speech = db > threshold

    # Runs of speech frames, as [start_frame, end_frame)
    runs: list[list[int]] = []
    start = None
    for i, is_speech in enumerate(speech):
        if is_speech and start is None:
            start = i
        elif not is_speech and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(speech)])

    # Merge across short pauses
    merge_gap = int(_MERGE_GAP_SECONDS / _FRAME_SECONDS)
    merged: list[list[int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    # Split runs that are too long at their quietest frame
    max_frames = int(max_seconds / _FRAME_SECONDS)
    min_split = max_frames // 3
    pieces: list[list[int]] = []
    for lo, hi in merged:
        while hi - lo > max_frames:
            window = db[lo + min_split : lo + max_frames]
            # Quietest point, preferring the latest one so pieces stay as long as allowed.
            cut = lo + min_split + (len(window) - 1 - int(np.argmin(window[::-1])))
            pieces.append([lo, cut])
            lo = cut
        pieces.append([lo, hi])

    duration = len(samples) / sample_rate
    chunks = []
    for lo, hi in pieces:
        start_s = max(0.0, lo * _FRAME_SECONDS - _PAD_SECONDS)
        end_s = min(duration, hi * _FRAME_SECONDS + _PAD_SECONDS)
        if end_s - start_s >= _MIN_CHUNK_SECONDS:
            chunks.append(Chunk(start_s, end_s))
    # Padding can make neighbours overlap slightly; keep them in order without overlap.
    for prev, cur in pairwise(chunks):
        if cur.start < prev.end:
            mid = (prev.end + cur.start) / 2
            prev.end, cur.start = mid, mid
    return chunks


def choose_language(probs: dict[str, float], allowed: list[str], previous: str | None, duration: float) -> str:
    """Most likely allowed language; short, uncertain stretches keep the previous language."""
    restricted = {code: float(probs.get(code, 0.0)) for code in allowed}
    total = sum(restricted.values())
    if total <= 0:
        return previous or allowed[0]
    best = max(restricted, key=lambda code: restricted[code])
    share = restricted[best] / total
    if previous and best != previous and duration < _SHORT_CHUNK_SECONDS and share < _CONFIDENT_SHARE:
        return previous
    return best


def group_chunks(chunks: list[Chunk], max_seconds: float = MAX_WINDOW_SECONDS) -> list[Chunk]:
    """Join consecutive same-language chunks into windows Whisper can take in one pass."""
    windows: list[Chunk] = []
    for chunk in chunks:
        last = windows[-1] if windows else None
        if (
            last is not None
            and last.language == chunk.language
            and chunk.end - last.start <= max_seconds
            and chunk.start - last.end <= _MAX_GAP_IN_WINDOW_SECONDS
        ):
            last.end = chunk.end
        else:
            windows.append(Chunk(chunk.start, chunk.end, chunk.language))
    return windows


def transcribe_multilingual(
    samples: np.ndarray,
    sample_rate: int,
    languages: list[str],
    detect: Callable[[np.ndarray], dict[str, float]],
    transcribe_window: Callable[[np.ndarray, str], list[Segment]],
    on_progress: Callable[[float], None] | None = None,
) -> TranscriptResult:
    """Transcribe *samples*, choosing among *languages* separately for each stretch of speech.

    *detect* returns language probabilities for a clip; *transcribe_window* transcribes a
    clip in the given language with timestamps relative to the clip's start.
    """
    samples = np.asarray(samples, dtype=np.float32)
    duration = len(samples) / sample_rate
    chunks = speech_chunks(samples, sample_rate)

    def clip(chunk: Chunk) -> np.ndarray:
        return samples[int(chunk.start * sample_rate) : int(chunk.end * sample_rate)]

    previous: str | None = None
    for i, chunk in enumerate(chunks):
        chunk.language = choose_language(detect(clip(chunk)), languages, previous, chunk.duration)
        previous = chunk.language
        if on_progress:
            on_progress(0.2 * (i + 1) / len(chunks))

    windows = group_chunks(chunks)
    segments: list[Segment] = []
    for i, window in enumerate(windows):
        for seg in transcribe_window(clip(window), window.language):
            if not seg.text.strip():
                continue
            seg.start = min(window.start + seg.start, window.end)
            seg.end = min(window.start + seg.end, window.end)
            for word in seg.words:
                word.start += window.start
                word.end += window.start
            seg.language = window.language
            segments.append(seg)
        if on_progress:
            on_progress(0.2 + 0.8 * (i + 1) / len(windows))

    used = [code for code in languages if any(seg.language == code for seg in segments)]
    return TranscriptResult(segments=segments, language=", ".join(used), duration=duration)
