"""Tests for mixed-language (e.g. English + Spanish) transcription, with a fake Whisper."""

from __future__ import annotations

import numpy as np

from ownscribe.config import TranscriptionConfig
from ownscribe.transcription.models import Segment
from ownscribe.transcription.multilingual import (
    Chunk,
    choose_language,
    group_chunks,
    speech_chunks,
    transcribe_multilingual,
)

RATE = 16000
EN_HZ, ES_HZ = 300.0, 700.0  # the fake detector "hears" English as 300 Hz and Spanish as 700 Hz


def _audio(parts: list[tuple[float, float | None]]) -> np.ndarray:
    """Concatenate (seconds, tone Hz or None for silence) parts."""
    out = []
    for seconds, hz in parts:
        n = int(seconds * RATE)
        if hz is None:
            out.append(np.zeros(n, dtype=np.float32))
        else:
            out.append((0.3 * np.sin(2 * np.pi * hz * np.arange(n) / RATE)).astype(np.float32))
    return np.concatenate(out)


def _fake_detect(clip: np.ndarray) -> dict[str, float]:
    spectrum = np.abs(np.fft.rfft(clip))
    peak = np.fft.rfftfreq(len(clip), 1 / RATE)[int(np.argmax(spectrum))]
    en = 0.9 if abs(peak - EN_HZ) < abs(peak - ES_HZ) else 0.05
    # Some probability on a language that isn't allowed, which must be ignored.
    return {"en": en, "es": 0.95 - en, "pt": 0.05}


def test_speech_chunks_split_at_pauses():
    samples = _audio([(1, None), (3, EN_HZ), (1.5, None), (2, ES_HZ), (1, None)])
    chunks = speech_chunks(samples, RATE)
    assert len(chunks) == 2
    assert abs(chunks[0].start - 0.75) < 0.1 and abs(chunks[0].end - 4.25) < 0.1
    assert abs(chunks[1].start - 5.25) < 0.1 and abs(chunks[1].end - 7.75) < 0.1


def test_short_pauses_do_not_split_and_long_speech_is_capped():
    samples = _audio([(10, EN_HZ), (0.3, None), (50, EN_HZ)])
    chunks = speech_chunks(samples, RATE)
    assert all(c.duration <= 28.0 + 0.5 for c in chunks)
    assert chunks[0].start < 0.1 and abs(chunks[-1].end - 60.3) < 0.1
    assert len(chunks) == 3  # 60 s of near-continuous speech in <=28 s pieces


def test_silence_and_room_noise_are_not_speech():
    rng = np.random.default_rng(0)
    noise = (0.0015 * rng.standard_normal(RATE * 10)).astype(np.float32)  # about -56 dBFS
    assert speech_chunks(np.zeros(RATE * 5, dtype=np.float32), RATE) == []
    assert speech_chunks(noise, RATE) == []


def test_choose_language():
    assert choose_language({"en": 0.2, "es": 0.7, "pt": 0.1}, ["en", "es"], None, 5.0) == "es"
    # Not allowed languages are ignored, even when most likely.
    assert choose_language({"pt": 0.9, "es": 0.06, "en": 0.04}, ["en", "es"], None, 5.0) == "es"
    # A short, uncertain "sí"/"OK" keeps the language of the conversation...
    assert choose_language({"en": 0.4, "es": 0.6}, ["en", "es"], "en", 0.8) == "en"
    # ...but a short, clear switch is honoured.
    assert choose_language({"en": 0.05, "es": 0.95}, ["en", "es"], "en", 0.8) == "es"
    assert choose_language({}, ["en", "es"], None, 1.0) == "en"


def test_group_chunks():
    chunks = [
        Chunk(0, 5, "en"),
        Chunk(5.5, 10, "en"),
        Chunk(11, 14, "es"),
        Chunk(20, 22, "es"),  # gap too long to share a window
        Chunk(22.5, 40, "es"),
        Chunk(41, 50, "es"),  # would exceed 28 s
    ]
    assert [(c.start, c.end, c.language) for c in group_chunks(chunks)] == [
        (0, 10, "en"),
        (11, 14, "es"),
        (20, 40, "es"),
        (41, 50, "es"),
    ]


def test_switching_languages_end_to_end():
    samples = _audio([(0.5, None), (4, EN_HZ), (1, None), (3, ES_HZ), (1, None), (2, EN_HZ), (0.5, None)])
    calls = []

    def fake_transcribe(clip, language):
        calls.append((round(len(clip) / RATE, 1), language))
        text = "Let's review the cutover." if language == "en" else "Sí, mañana lo revisamos."
        return [Segment(text=text, start=0.25, end=len(clip) / RATE - 0.25)]

    progress = []
    result = transcribe_multilingual(samples, RATE, ["en", "es"], _fake_detect, fake_transcribe, progress.append)

    assert [(s.language, s.text) for s in result.segments] == [
        ("en", "Let's review the cutover."),
        ("es", "Sí, mañana lo revisamos."),
        ("en", "Let's review the cutover."),
    ]
    assert [lang for _, lang in calls] == ["en", "es", "en"]
    # Timestamps are on the recording's timeline, not the clip's.
    assert abs(result.segments[1].start - 5.5) < 0.2
    assert result.language == "en, es"
    assert abs(result.duration - 12.0) < 0.01
    assert progress and abs(progress[-1] - 1.0) < 1e-9


def test_mixed_languages_setting():
    assert TranscriptionConfig(languages=["en", "es"]).mixed_languages == ["en", "es"]
    assert TranscriptionConfig(languages=["EN", " es ", "en"]).mixed_languages == ["en", "es"]
    assert TranscriptionConfig(languages=["en"]).mixed_languages == []
    assert TranscriptionConfig().mixed_languages == []


def test_mlx_backend_uses_mixed_mode(monkeypatch, tmp_path):
    """The MLX transcriber wires real-looking mlx_whisper calls into the mixed-language path."""
    import sys
    import types

    from ownscribe.transcription.mlx_transcriber import MlxWhisperTranscriber

    samples = _audio([(0.5, None), (3, EN_HZ), (1, None), (3, ES_HZ), (0.5, None)])
    transcribe_calls = []

    class FakeModel:
        dims = types.SimpleNamespace(n_mels=128)

        def detect_language(self, mel):
            return None, _fake_detect(mel)

    def fake_transcribe(clip, **kwargs):
        transcribe_calls.append(kwargs)
        segment = {"start": 0.2, "end": 1.0, "text": f" {kwargs['language']}"}
        return {"language": kwargs["language"], "segments": [segment]}

    mx = types.SimpleNamespace(float16=np.float32)
    audio = types.SimpleNamespace(
        N_FRAMES=3000,
        N_SAMPLES=480000,
        SAMPLE_RATE=RATE,
        load_audio=lambda path: samples,
        log_mel_spectrogram=lambda clip, n_mels, padding: clip,
        pad_or_trim=lambda mel, length, axis: mel,
    )
    holder = types.SimpleNamespace(get_model=lambda repo, dtype: FakeModel())
    monkeypatch.setitem(sys.modules, "mlx", types.SimpleNamespace(core=mx))
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake_transcribe))
    monkeypatch.setitem(sys.modules, "mlx_whisper.audio", audio)
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", types.SimpleNamespace(ModelHolder=holder))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    cfg = TranscriptionConfig(backend="mlx", model="large-v3-turbo", languages=["en", "es"], initial_prompt="MAWM.")
    result = MlxWhisperTranscriber(cfg).transcribe(tmp_path / "recording.them.wav")

    assert [(s.language, s.text.strip()) for s in result.segments] == [("en", "en"), ("es", "es")]
    assert [c["language"] for c in transcribe_calls] == ["en", "es"]
    assert all(c["initial_prompt"] == "MAWM." and c["condition_on_previous_text"] is False for c in transcribe_calls)
    assert result.language == "en, es"
