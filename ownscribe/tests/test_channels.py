"""Tests for me-vs-them labelling from split tracks."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from ownscribe.transcription.channels import (
    drop_echo,
    drop_quiet_segments,
    has_split_tracks,
    merge_tracks,
    segment_dbfs,
    split_track_paths,
    transcribe_tracks,
)
from ownscribe.transcription.models import Segment, TranscriptResult, Word

RATE = 24000


def _write_track(path: Path, loud_spans: list[tuple[float, float]], total: float = 10.0) -> Path:
    """A WAV that is silent except for a 440 Hz tone (about -9 dBFS) in each span."""
    samples = np.zeros(int(total * RATE), dtype=np.float32)
    t = np.arange(samples.size) / RATE
    tone = 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    for start, end in loud_spans:
        lo, hi = int(start * RATE), int(end * RATE)
        samples[lo:hi] = tone[lo:hi]
    sf.write(path, samples, RATE)
    return path


class TestPaths:
    def test_split_track_paths(self):
        me, them = split_track_paths(Path("/x/2026-10-19_1400/recording.wav"))
        assert me == Path("/x/2026-10-19_1400/recording.me.wav")
        assert them == Path("/x/2026-10-19_1400/recording.them.wav")

    def test_has_split_tracks(self, tmp_path):
        audio = tmp_path / "recording.wav"
        assert not has_split_tracks(audio)
        (tmp_path / "recording.them.wav").write_bytes(b"x")
        assert has_split_tracks(audio)


class TestQuietFilter:
    def test_segment_dbfs(self):
        samples = np.full(RATE, 0.1, dtype=np.float32)
        assert abs(segment_dbfs(samples, RATE, 0.0, 1.0) - (-20.0)) < 0.01
        assert segment_dbfs(np.zeros(RATE, dtype=np.float32), RATE, 0.0, 1.0) == float("-inf")
        assert segment_dbfs(samples, RATE, 2.0, 3.0) == float("-inf")  # past the end

    def test_drops_hallucinations_in_silence(self, tmp_path):
        track = _write_track(tmp_path / "recording.me.wav", [(1.0, 3.0)])
        segments = [
            Segment(text="Real speech", start=1.0, end=3.0),
            Segment(text="Thank you.", start=6.0, end=7.0),  # silence
        ]
        kept = drop_quiet_segments(segments, track)
        assert [s.text for s in kept] == ["Real speech"]

    def test_unreadable_track_keeps_everything(self, tmp_path):
        segments = [Segment(text="Hello", start=0.0, end=1.0)]
        assert drop_quiet_segments(segments, tmp_path / "missing.wav") == segments


class TestEcho:
    def test_drops_mic_copy_of_remote_speech(self):
        them = [Segment(text="Can everyone see my screen?", start=5.0, end=7.0)]
        me = [
            Segment(text="can everyone see my screen", start=5.1, end=7.1),  # speaker bleed
            Segment(text="Yes, I can see it.", start=7.5, end=8.5),
        ]
        assert [s.text for s in drop_echo(me, them)] == ["Yes, I can see it."]

    def test_keeps_different_words_at_the_same_time(self):
        them = [Segment(text="Let's look at the outbound file transfers.", start=5.0, end=8.0)]
        me = [Segment(text="Sorry, which environment?", start=6.0, end=7.0)]
        assert drop_echo(me, them) == me

    def test_keeps_same_words_at_different_times(self):
        them = [Segment(text="Sounds good", start=1.0, end=2.0)]
        me = [Segment(text="Sounds good", start=30.0, end=31.0)]
        assert drop_echo(me, them) == me


class TestMerge:
    def test_interleaves_by_time(self):
        me = TranscriptResult(segments=[Segment(text="b", start=2.0, end=3.0, speaker="Pierce")], duration=10)
        them = TranscriptResult(
            segments=[
                Segment(text="a", start=0.0, end=1.0, speaker="Them"),
                Segment(text="c", start=4.0, end=5.0, speaker="Them"),
            ],
            language="en",
            duration=12,
        )
        merged = merge_tracks(me, them)
        assert [s.text for s in merged.segments] == ["a", "b", "c"]
        assert merged.language == "en"
        assert merged.duration == 12


class TestTranscribeTracks:
    def test_returns_none_without_tracks(self, tmp_path):
        assert transcribe_tracks(lambda p: None, tmp_path / "recording.wav", "Me", "Them") is None

    def test_labels_filters_and_merges(self, tmp_path):
        audio = tmp_path / "recording.wav"
        _write_track(tmp_path / "recording.me.wav", [(2.0, 4.0), (5.0, 7.0)])
        _write_track(tmp_path / "recording.them.wav", [(0.0, 2.0), (5.0, 7.0)])

        fake = {
            "recording.me.wav": TranscriptResult(
                segments=[
                    Segment(text="I'll take that action.", start=2.0, end=4.0, words=[Word("I'll", 2.0, 2.3)]),
                    Segment(text="Can you hear me now", start=5.0, end=7.0),  # bleed of remote speech
                    Segment(text="Thanks for watching!", start=8.0, end=9.0),  # silence hallucination
                ],
                language="en",
                duration=10.0,
            ),
            "recording.them.wav": TranscriptResult(
                segments=[
                    Segment(text="Who owns the cutover?", start=0.0, end=2.0),
                    Segment(text="Can you hear me now?", start=5.0, end=7.0),
                ],
                language="en",
                duration=10.0,
            ),
        }
        result = transcribe_tracks(lambda p: fake[p.name], audio, "Pierce", "Them")

        assert [(s.speaker, s.text) for s in result.segments] == [
            ("Them", "Who owns the cutover?"),
            ("Pierce", "I'll take that action."),
            ("Them", "Can you hear me now?"),
        ]
        assert result.segments[1].words[0].speaker == "Pierce"
        assert "Pierce: I'll take that action." in result.speaker_text

    def test_them_only(self, tmp_path):
        audio = tmp_path / "recording.wav"
        _write_track(tmp_path / "recording.them.wav", [(0.0, 2.0)])
        them = TranscriptResult(segments=[Segment(text="Hello", start=0.0, end=2.0)], duration=10.0)
        result = transcribe_tracks(lambda p: them, audio, "Pierce", "Them")
        assert [(s.speaker, s.text) for s in result.segments] == [("Them", "Hello")]
