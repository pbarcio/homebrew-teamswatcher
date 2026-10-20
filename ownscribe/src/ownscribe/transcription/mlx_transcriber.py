"""Whisper on the Apple Silicon GPU via mlx-whisper."""

from __future__ import annotations

import contextlib
import io
import logging
import shutil
import warnings
from pathlib import Path

import click

from ownscribe.config import TranscriptionConfig
from ownscribe.progress import NullProgress
from ownscribe.transcription.base import Transcriber
from ownscribe.transcription.models import Segment, TranscriptResult, Word

# Model names accepted by the WhisperX backend, mapped to MLX conversions on the Hub.
MLX_MODELS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large": "mlx-community/whisper-large-v3-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
}

_INSTALL_HINT = (
    "The 'mlx' transcription backend needs mlx-whisper (Apple Silicon only). Install with:\n"
    "  uv tool install --force 'ownscribe[mlx]'"
)


def resolve_mlx_repo(model: str) -> str:
    """Hub repo (or local path) for a model name. Anything containing '/' passes through."""
    name = model.strip()
    if not name:
        return MLX_MODELS["large-v3-turbo"]
    if "/" in name:
        return name
    return MLX_MODELS.get(name, f"mlx-community/whisper-{name}-mlx")


def _prompt(config: TranscriptionConfig) -> str | None:
    """mlx-whisper has no hotwords option, so fold them into the initial prompt."""
    parts = [config.initial_prompt.strip()]
    if config.hotwords.strip():
        parts.append(config.hotwords.strip())
    text = " ".join(p for p in parts if p)
    return text or None


class MlxWhisperTranscriber(Transcriber):
    """Transcribes audio with mlx-whisper on the GPU of Apple Silicon Macs."""

    def __init__(self, transcription_config: TranscriptionConfig, progress: NullProgress | None = None) -> None:
        self._tx_config = transcription_config
        self._progress = progress or NullProgress()
        self._repo = resolve_mlx_repo(transcription_config.model)

    def _set_detail(self, key: str, text: str | None) -> None:
        set_detail = getattr(self._progress, "set_detail", None)
        if callable(set_detail):
            set_detail(key, text)

    def prepare_models(self, language: str | None = None) -> None:
        _ = language
        progress = self._progress
        progress.begin("preparing_models")
        try:
            from huggingface_hub import snapshot_download

            self._set_detail("preparing_models", f"Downloading MLX Whisper model ({self._repo})")
            if not Path(self._repo).exists():
                with contextlib.redirect_stderr(io.StringIO()):
                    snapshot_download(self._repo)
            progress.complete("preparing_models")
        except Exception:
            progress.fail("preparing_models")
            raise

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if self._tx_config.mixed_languages:
            return self._transcribe_mixed(audio_path)
        if not shutil.which("ffmpeg"):
            click.echo("Error: ffmpeg is not installed. Install with: brew install ffmpeg", err=True)
            raise SystemExit(1)
        try:
            import mlx_whisper
        except ImportError:
            click.echo(f"Error: {_INSTALL_HINT}", err=True)
            raise SystemExit(1) from None

        progress = self._progress
        progress.begin("transcribing")
        self._set_detail("transcribing", f"{audio_path.name} on the GPU ({self._repo.split('/')[-1]})")
        try:
            with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
                warnings.simplefilter("ignore")
                logging.getLogger("mlx_whisper").setLevel(logging.WARNING)
                result = mlx_whisper.transcribe(
                    str(audio_path),
                    path_or_hf_repo=self._repo,
                    verbose=None,
                    language=self._tx_config.language or None,
                    initial_prompt=_prompt(self._tx_config),
                    # Carrying text between windows makes Whisper loop on repeated phrases
                    # during long silences, which meetings have plenty of.
                    condition_on_previous_text=False,
                    word_timestamps=False,
                )
        except Exception:
            progress.fail("transcribing")
            raise
        progress.complete("transcribing")
        return _to_result(result, _duration(audio_path))

    def _transcribe_mixed(self, audio_path: Path) -> TranscriptResult:
        """Transcribe a recording that switches languages, detecting the language per stretch."""
        if not shutil.which("ffmpeg"):
            click.echo("Error: ffmpeg is not installed. Install with: brew install ffmpeg", err=True)
            raise SystemExit(1)
        try:
            import mlx.core as mx
            import mlx_whisper
            from mlx_whisper.audio import N_FRAMES, N_SAMPLES, SAMPLE_RATE, load_audio, log_mel_spectrogram, pad_or_trim
            from mlx_whisper.transcribe import ModelHolder
        except ImportError:
            click.echo(f"Error: {_INSTALL_HINT}", err=True)
            raise SystemExit(1) from None

        from ownscribe.transcription.multilingual import transcribe_multilingual

        languages = self._tx_config.mixed_languages
        prompt = _prompt(self._tx_config)
        progress = self._progress
        progress.begin("transcribing")
        self._set_detail("transcribing", f"{audio_path.name}: {' + '.join(languages)} on the GPU")

        def detect(clip):
            model = ModelHolder.get_model(self._repo, mx.float16)
            mel = log_mel_spectrogram(clip, n_mels=model.dims.n_mels, padding=N_SAMPLES)
            _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
            return probs

        def transcribe_window(clip, language):
            result = mlx_whisper.transcribe(
                clip,
                path_or_hf_repo=self._repo,
                verbose=None,
                language=language,
                initial_prompt=prompt,
                condition_on_previous_text=False,
                word_timestamps=False,
            )
            return _to_result(result).segments

        try:
            with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
                warnings.simplefilter("ignore")
                logging.getLogger("mlx_whisper").setLevel(logging.WARNING)
                samples = load_audio(str(audio_path))
                result = transcribe_multilingual(
                    samples,
                    SAMPLE_RATE,
                    languages,
                    detect,
                    transcribe_window,
                    on_progress=lambda fraction: progress.update("transcribing", fraction),
                )
        except Exception:
            progress.fail("transcribing")
            raise
        progress.complete("transcribing")
        return result


def _duration(audio_path: Path) -> float | None:
    try:
        import soundfile as sf

        return float(sf.info(str(audio_path)).duration)
    except Exception:
        return None


def _to_result(result: dict, duration: float | None = None) -> TranscriptResult:
    segments = []
    for seg in result.get("segments", []):
        words = [
            Word(
                text=w.get("word", ""),
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
                score=float(w.get("probability", 0.0)),
            )
            for w in seg.get("words", []) or []
        ]
        segments.append(
            Segment(
                text=seg.get("text", ""),
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                words=words,
            )
        )
    if duration is None:
        duration = segments[-1].end if segments else 0.0
    return TranscriptResult(segments=segments, language=result.get("language", "") or "", duration=duration)
