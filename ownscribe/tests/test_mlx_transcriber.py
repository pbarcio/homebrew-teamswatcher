"""Tests for the MLX Whisper backend (mlx-whisper itself is faked)."""

from __future__ import annotations

import sys
import types

import pytest

from ownscribe.config import Config, TranscriptionConfig
from ownscribe.transcription.mlx_transcriber import (
    MlxWhisperTranscriber,
    _prompt,
    _to_result,
    resolve_mlx_repo,
)


class TestResolveRepo:
    @pytest.mark.parametrize(
        ("model", "repo"),
        [
            ("medium", "mlx-community/whisper-medium-mlx"),
            ("large-v3", "mlx-community/whisper-large-v3-mlx"),
            ("large-v3-turbo", "mlx-community/whisper-large-v3-turbo"),
            ("turbo", "mlx-community/whisper-large-v3-turbo"),
            ("", "mlx-community/whisper-large-v3-turbo"),
            ("mlx-community/whisper-medium-mlx-8bit", "mlx-community/whisper-medium-mlx-8bit"),
            ("small.en", "mlx-community/whisper-small.en-mlx"),
        ],
    )
    def test_resolve(self, model, repo):
        assert resolve_mlx_repo(model) == repo


def test_prompt_folds_in_hotwords():
    cfg = TranscriptionConfig(initial_prompt="Manhattan Active WM.", hotwords="MAWM, MIF")
    assert _prompt(cfg) == "Manhattan Active WM. MAWM, MIF"
    assert _prompt(TranscriptionConfig()) is None


def test_to_result():
    result = _to_result(
        {
            "language": "en",
            "segments": [
                {"start": 0.0, "end": 1.5, "text": " Hello", "words": [{"word": "Hello", "start": 0.1, "end": 0.5}]},
                {"start": 2.0, "end": 3.0, "text": " there"},
            ],
        },
        duration=4.0,
    )
    assert [s.text for s in result.segments] == [" Hello", " there"]
    assert result.segments[0].words[0].text == "Hello"
    assert result.language == "en"
    assert result.duration == 4.0


def test_transcribe_calls_mlx(monkeypatch, tmp_path):
    calls = {}

    def fake_transcribe(path, **kwargs):
        calls["path"] = path
        calls.update(kwargs)
        return {"language": "en", "segments": [{"start": 0.0, "end": 1.0, "text": "Hi"}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake_transcribe))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    cfg = TranscriptionConfig(backend="mlx", model="large-v3-turbo", language="en")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    result = MlxWhisperTranscriber(cfg).transcribe(audio)

    assert calls["path"] == str(audio)
    assert calls["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    assert calls["language"] == "en"
    assert calls["condition_on_previous_text"] is False
    assert result.segments[0].text == "Hi"


def test_pipeline_picks_mlx_backend():
    from ownscribe.pipeline import _create_transcriber

    config = Config()
    config.transcription.backend = "mlx"
    assert isinstance(_create_transcriber(config), MlxWhisperTranscriber)
