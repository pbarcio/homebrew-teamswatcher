"""Pipeline orchestration: record -> transcribe -> summarize -> output."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import click

from ownscribe.config import Config
from ownscribe.progress import (
    DownloadProgressEvent,
    PipelineProgress,
    download_event_fraction,
    format_download_progress,
)
from ownscribe.summarization import create_summarizer

# A standard WAV file header (RIFF + fmt + data chunk header) is 44 bytes.
# Files at or below this size contain no audio frames.
_WAV_HEADER_SIZE = 44


def _check_audio_silence(audio_path: Path) -> None:
    """Check if the recorded audio is silent and warn the user."""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        return  # Skip check if deps not available

    try:
        # Read up to 5 seconds
        info = sf.info(audio_path)
        frames_to_read = min(int(info.samplerate * 5), info.frames)
        data, _ = sf.read(audio_path, frames=frames_to_read, dtype="float32")
        peak = float(np.max(np.abs(data)))
    except Exception:
        return  # Don't block pipeline on check failure

    if peak < 1e-6:
        click.echo(
            "\nError: Recorded audio is completely silent (peak amplitude ~0).\n"
            "This usually means Screen Recording permission is missing.\n"
            "Fix: System Settings > Privacy & Security > Screen Recording "
            "— enable your terminal app, then restart it.\n",
            err=True,
        )
        raise SystemExit(1)


def _get_output_dir(config: Config) -> Path:
    """Create and return this recording's folder, named "YYYY-MM-DD HHMM" until it gets a title."""
    from ownscribe.output.naming import stamp_for, unique_path

    base = config.output.resolved_dir
    out_dir = unique_path(base / stamp_for(datetime.now()))
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _get_output_audio_dir(config: Config, out_dir: Path) -> Path:
    """Create and return the audio directory for a run, mirroring out_dir's name."""
    audio_dir = config.output.resolved_audio_dir / out_dir.name
    audio_dir.mkdir(parents=True, exist_ok=True)
    return audio_dir


def _rename_output_dir(directory: Path, title_slug: str) -> Path:
    """Append title slug to directory name. Returns the new path, or the original
    directory if the target already exists with content or the rename fails."""
    logger = logging.getLogger(__name__)
    new_dir = directory.parent / f"{directory.name}_{title_slug}"
    try:
        if new_dir.exists() and any(new_dir.iterdir()):
            logger.debug("Not renaming output directory: %s already exists and is not empty", new_dir)
            return directory
        directory.rename(new_dir)
        return new_dir
    except OSError as e:
        # Without exc_info: with no logging configured, a traceback lands on stderr
        # and reads like a crash even though the run itself succeeded.
        logger.warning("Could not rename output directory to %s: %s", new_dir.name, e)
        return directory


def _unsupported_sounddevice_settings(config: Config) -> list[str]:
    """Audio settings the sounddevice backend cannot honour.

    It records one input device and cannot mix in system audio, pick a separate
    mic, or turn the mic off, so these settings would otherwise be accepted and
    then quietly ignored.
    """
    unsupported = []
    if config.audio.mic_device:
        unsupported.append("--mic-device")
    if not config.audio.mic:
        unsupported.append("--no-mic")
    if config.audio.capture_mode != "all":
        unsupported.append(f"capture_mode = {config.audio.capture_mode!r}")
    return unsupported


def _create_recorder(config: Config):
    """Create the appropriate audio recorder based on config."""
    if config.audio.backend == "coreaudio" and not config.audio.device:
        from ownscribe.audio.coreaudio import CoreAudioRecorder

        recorder = CoreAudioRecorder(
            mic=config.audio.mic,
            mic_device=config.audio.mic_device,
            capture_mode=config.audio.capture_mode,
            silence_timeout=config.audio.silence_timeout,
            apps=config.audio.apps,
            split_tracks=config.audio.mic and config.speakers.mode == "channels",
        )
        if recorder.is_available():
            return recorder
        click.echo("Core Audio helper not found, falling back to sounddevice.")

    from ownscribe.audio.sounddevice_recorder import SoundDeviceRecorder

    if ignored := _unsupported_sounddevice_settings(config):
        click.echo(
            f"Warning: the sounddevice backend ignores {', '.join(ignored)}.\n"
            "It records a single input device; choose which one with --device.",
            err=True,
        )

    device = config.audio.device or None
    # Try to parse as int (device index)
    if isinstance(device, str) and device.isdigit():
        device = int(device)
    return SoundDeviceRecorder(device=device, silence_timeout=config.audio.silence_timeout)


def _create_transcriber(config: Config, progress=None):
    """Create the transcriber for the configured backend (WhisperX or MLX)."""
    if config.transcription.backend == "mlx":
        from ownscribe.transcription.mlx_transcriber import MlxWhisperTranscriber

        if config.diarization.enabled:
            click.echo("Warning: diarization is not supported with the mlx backend; skipping it.", err=True)
        return MlxWhisperTranscriber(config.transcription, progress=progress)

    from ownscribe.transcription.whisperx_transcriber import WhisperXTranscriber

    if config.transcription.mixed_languages:
        click.echo(
            "Warning: switching between languages within a call needs the mlx backend; "
            "detecting one language per recording instead.",
            err=True,
        )
        config.transcription.language = ""

    diar_config = config.diarization if config.diarization.enabled else None
    return WhisperXTranscriber(
        config.transcription,
        diar_config,
        progress=progress,
        # Only the JSON output carries word-level timings; markdown reads
        # nothing but segment start, text and speaker.
        need_word_timestamps=config.output.format == "json",
    )


def _download_summarization_model(
    model_name: str,
    progress: PipelineProgress,
    step_key: str,
) -> Path:
    """Download the summarization GGUF model, feeding progress into the TUI."""
    from ownscribe.summarization.llama_cpp_summarizer import _ensure_model

    def _on_progress(event: DownloadProgressEvent) -> None:
        fraction = download_event_fraction(event)
        if fraction is not None:
            progress.update(step_key, fraction)
        formatted = format_download_progress(event, include_percent=fraction is None)
        if formatted:
            progress.set_detail(step_key, formatted)

    return _ensure_model(model_name, on_progress=_on_progress)


def _format_output(config: Config, transcript_result, summary_text: str | None = None) -> tuple[str, str | None]:
    """Format transcript and optional summary. Returns (transcript_str, summary_str)."""
    if config.output.format == "json":
        from ownscribe.output.json_output import format_summary_json, format_transcript_json

        tx = format_transcript_json(transcript_result)
        sm = format_summary_json(summary_text) if summary_text else None
        return tx, sm
    else:
        from ownscribe.output.markdown import format_summary, format_transcript

        tx = format_transcript(transcript_result)
        sm = format_summary(summary_text) if summary_text else None
        return tx, sm


def _slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length].rstrip("-")


def _generate_title(summary: str, summarizer) -> str:
    """Generate a short meeting title from a summary. Returns empty string on failure."""
    try:
        title = summarizer.generate_title(summary)
        if not isinstance(title, str):
            return ""
        return title.strip().strip('"').strip()
    except Exception:
        logging.getLogger(__name__).warning("Could not generate title", exc_info=True)
        return ""


def _generate_title_slug(summary: str, summarizer) -> str:
    """Generate a title slug from a summary. Returns empty string on failure."""
    return _slugify(_generate_title(summary, summarizer))


def _transcribe(config: Config, transcriber, audio_path: Path):
    """Transcribe, labelling you vs. the other side when per-source tracks exist."""
    if config.speakers.mode == "channels":
        from ownscribe.transcription.channels import transcribe_tracks

        result = transcribe_tracks(
            transcriber.transcribe,
            audio_path,
            me_label=config.speakers.me_label,
            them_label=config.speakers.them_label,
        )
        if result is not None:
            return result

    result = transcriber.transcribe(audio_path)
    if config.transcription.backend == "mlx":
        # MLX Whisper has no voice-activity filter; drop what it "hears" in silence.
        from ownscribe.transcription.channels import drop_quiet_segments

        result.segments = drop_quiet_segments(result.segments, audio_path)
    return result


def _default_title() -> str:
    """Title when there is no summary to generate one from: the app that was recorded."""
    source = os.environ.get("OWNSCRIBE_SOURCE_APP", "").strip()
    return f"{source} call" if source else "Recording"


def _rename_dir_to(directory: Path, target: Path) -> Path:
    """Rename *directory* to *target*; returns the original path if that fails."""
    try:
        directory.rename(target)
        return target
    except OSError as e:
        logging.getLogger(__name__).warning("Could not rename %s to %s: %s", directory.name, target.name, e)
        return directory


def _finalize_folder(config: Config, out_dir: Path, audio_path: Path, title: str) -> tuple[Path, Path]:
    """Rename this recording's folder to "YYYY-MM-DD HHMM - Title" (audio folder too, if separate).

    Only folders ownscribe created in the output directory are renamed, never a folder
    that `ownscribe resume`/`transcribe` was pointed at elsewhere.
    """
    from ownscribe.output.naming import folder_name, parse_folder_name, unique_path

    parsed = parse_folder_name(out_dir.name)
    try:
        in_output_dir = out_dir.parent.resolve() == config.output.resolved_dir.resolve()
    except OSError:
        in_output_dir = False
    if parsed is None or not in_output_dir:
        return out_dir, audio_path

    new_name = folder_name(parsed.stamp, title)
    if new_name == out_dir.name:
        return out_dir, audio_path

    old_out_dir = out_dir
    out_dir = _rename_dir_to(out_dir, unique_path(out_dir.parent / new_name))
    audio_dir = audio_path.parent
    if audio_dir == old_out_dir:
        audio_dir = out_dir
    elif config.output.uses_separate_audio_dir and out_dir != old_out_dir and audio_dir.name == old_out_dir.name:
        audio_dir = _rename_dir_to(audio_dir, unique_path(audio_dir.parent / out_dir.name))
    return out_dir, audio_dir / audio_path.name


def _write_notes(
    config: Config, out_dir: Path, result, summary: str | None, title: str, bookmarks: list[float] | None = None,
) -> Path | None:
    """Save the one-file notes ("<folder name>.md") in the recording's folder, plus an
    optional copy elsewhere. Never fails the run; returns the notes path."""
    from ownscribe.output.export import format_export, resolve_export_dir, started_at, write_export

    if not config.export.notes:
        return None
    source = os.environ.get("OWNSCRIBE_SOURCE_APP", "").strip()
    content = format_export(
        title=title,
        started=started_at(out_dir),
        result=result,
        summary=summary,
        source=source,
        include_transcript=config.export.include_transcript,
        bookmarks=bookmarks,
    )
    filename = f"{out_dir.name}.md"
    notes_path = out_dir / filename
    try:
        notes_path.write_text(content)
    except OSError as exc:
        click.echo(f"Warning: could not write notes to {notes_path}: {exc}", err=True)
        return None

    copy_dir = resolve_export_dir(config.export.copy_target)
    if copy_dir is not None:
        try:
            copied = write_export(copy_dir, filename, content)
            click.echo(f"Copied notes to {copied}")
        except OSError as exc:
            click.echo(f"Warning: could not copy notes to {copy_dir}: {exc}", err=True)
    return notes_path


@dataclass
class RuntimeExtras:
    """What a launcher learned about the call while it was being recorded."""

    title: str = ""  # the meeting's name, e.g. from the meeting app's window
    bookmarks: list[float] = field(default_factory=list)  # Unix times the user marked


def _apply_runtime_options(config: Config) -> RuntimeExtras:
    """Apply choices a launcher may change while recording (OWNSCRIBE_OPTIONS_FILE).

    The file is JSON with optional booleans "summarize" and "keep_recording", plus an
    optional "title" (string) and "bookmarks" (Unix timestamps). It is read once
    recording stops, so e.g. TeamsWatcher's menu toggles count for the call in progress.
    """
    extras = RuntimeExtras()
    target = os.environ.get("OWNSCRIBE_OPTIONS_FILE")
    if not target:
        return extras
    try:
        data = json.loads(Path(target).expanduser().read_text())
    except (OSError, ValueError):
        return extras
    if not isinstance(data, dict):
        return extras
    if isinstance(data.get("summarize"), bool):
        config.summarization.enabled = data["summarize"]
    if isinstance(data.get("keep_recording"), bool):
        config.output.keep_recording = data["keep_recording"]
    if isinstance(data.get("title"), str):
        extras.title = data["title"].strip()
    if isinstance(data.get("bookmarks"), list):
        extras.bookmarks = [
            float(v) for v in data["bookmarks"] if isinstance(v, int | float) and not isinstance(v, bool)
        ]
    return extras


def _system_audio_was_silent(audio_path: Path) -> bool:
    """True when the system-audio (them) track exists and is digital silence throughout.

    Exact zeros for a whole recording mean no app audio reached the recorder at all,
    which usually points at a missing Screen & System Audio Recording permission.
    """
    from ownscribe.transcription.channels import split_track_paths

    _, them = split_track_paths(audio_path)
    if not them.exists():
        return False
    try:
        import numpy as np
        import soundfile as sf

        samples, _ = sf.read(them, dtype="float32", always_2d=False)
    except Exception:
        return False
    return samples.size > 0 and float(np.max(np.abs(samples))) == 0.0


def _write_result_file(**paths: Path | str | None) -> None:
    """Tell a launcher (e.g. TeamsWatcher) where the outputs ended up.

    Written only when OWNSCRIBE_RESULT_FILE is set. Values are paths, plus
    "speech": "none" when nothing was transcribed.
    """
    target = os.environ.get("OWNSCRIBE_RESULT_FILE")
    if not target:
        return
    data = {key: str(path) for key, path in paths.items() if path is not None}
    try:
        Path(target).expanduser().write_text(json.dumps(data, indent=2))
    except OSError:
        logging.getLogger(__name__).warning("Could not write result file %s", target)


def run_pipeline(config: Config) -> None:
    """Run the full pipeline: record, transcribe, summarize, output."""
    out_dir = _get_output_dir(config)
    audio_dir = _get_output_audio_dir(config, out_dir)
    audio_path = audio_dir / "recording.wav"

    # 1. Record
    recorder = _create_recorder(config)

    from ownscribe.audio.coreaudio import CoreAudioRecorder

    can_mute = isinstance(recorder, CoreAudioRecorder) and config.audio.mic
    is_tty = sys.stdin.isatty()

    hints = []
    if can_mute and is_tty:
        hints.append("Press 'm' to mute/unmute mic.")
    silence_timeout = config.audio.silence_timeout
    if silence_timeout > 0:
        mins, secs = divmod(int(silence_timeout), 60)
        if mins > 0 and secs > 0:
            hints.append(f"Auto-stops after {mins}m {secs}s of silence.")
        elif mins > 0:
            hints.append(f"Auto-stops after {mins}m of silence.")
        else:
            hints.append(f"Auto-stops after {silence_timeout}s of silence.")
    hints.append("Press Ctrl+C to stop.")
    click.echo(f"Starting recording... {' '.join(hints)}\n")
    recorder.start(audio_path)

    start_time = time.time()
    stop_event = False

    def on_interrupt(sig, frame):
        nonlocal stop_event
        stop_event = True

    original_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, on_interrupt)

    old_termios = None
    if can_mute and is_tty:
        old_termios = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    warned_no_data = False
    try:
        while not stop_event and recorder.is_recording:
            elapsed = time.time() - start_time
            mins, secs = divmod(int(elapsed), 60)
            mute_indicator = "  [MIC MUTED]" if recorder.is_muted else ""
            click.echo(
                f"\r  Recording: {mins:02d}:{secs:02d}{mute_indicator}\033[K",
                nl=False,
            )
            if (
                not warned_no_data
                and elapsed >= 3
                and audio_path.exists()
                and audio_path.stat().st_size <= _WAV_HEADER_SIZE
            ):
                click.echo(
                    "\n\n  Warning: No audio data received yet.\n",
                    err=True,
                )
                warned_no_data = True
            if old_termios is not None:
                readable, _, _ = select.select([sys.stdin], [], [], 0.5)
                if readable:
                    ch = sys.stdin.read(1)
                    if ch in ("m", "M"):
                        recorder.toggle_mute()
                    elif ch == "\x03":
                        stop_event = True
            else:
                time.sleep(0.5)
    finally:
        if old_termios is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_termios)
        signal.signal(signal.SIGINT, original_handler)

    recorder.stop()
    extras = _apply_runtime_options(config)
    bookmarks = [stamp - start_time for stamp in extras.bookmarks]
    if getattr(recorder, "silence_timed_out", False):
        click.echo("\n\nRecording auto-stopped after silence timeout.")
    else:
        click.echo("\n\nStopping recording...")

    if not audio_path.exists() or audio_path.stat().st_size <= _WAV_HEADER_SIZE:
        message = (
            "Error: No audio was captured. Make sure audio is playing on your system, "
            "or use --device to capture mic-only."
        )
        # The mic is captured by default, and the helper stops the whole recording
        # when it cannot open the microphone (no input device, permission denied).
        if config.audio.mic:
            message += "\nIf the microphone is unavailable, use --no-mic to record system audio only."
        click.echo(message, err=True)
        raise SystemExit(1)

    click.echo(f"Audio saved to {audio_path}\n")

    # Check for silent audio before spending time on transcription
    # Skip if the recorder already reported a silence warning (CoreAudio helper)
    if not getattr(recorder, "silence_warning", False):
        _check_audio_silence(audio_path)

    # 2. Transcribe
    _do_transcribe_and_summarize(config, audio_path, out_dir, meeting_title=extras.title, bookmarks=bookmarks)


def run_transcribe(config: Config, audio_file: str) -> None:
    """Transcribe an audio file and save the transcript alongside the input."""
    audio_path = Path(audio_file).resolve()
    _check_audio_silence(audio_path)
    out_dir = audio_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    _do_transcribe_and_summarize(config, audio_path, out_dir, summarize=False)


def run_warmup(config: Config) -> None:
    """Prefetch transcription/diarization models without processing audio."""
    whisperx = config.transcription.backend != "mlx"
    diar_enabled = whisperx and config.diarization.enabled and bool(config.diarization.hf_token)
    hf_token_warning = whisperx and config.diarization.enabled and not config.diarization.hf_token
    local_sum = config.summarization.enabled and config.summarization.backend == "local"

    with PipelineProgress(
        diarize=False,
        summarize=False,
        transcribe=False,
        include_prepare=True,
        download_summarizer=local_sum,
    ) as progress:
        try:
            transcriber = _create_transcriber(config, progress=progress)
        except ImportError:
            click.echo(
                "Error: WhisperX is not installed. Install with:\n  uv pip install 'ownscribe[transcription]'",
                err=True,
            )
            raise SystemExit(1) from None

        transcriber.prepare_models(language=config.transcription.language or None)

        if local_sum:
            progress.begin("downloading_model")
            try:
                _download_summarization_model(config.summarization.model, progress, "downloading_model")
                progress.complete("downloading_model")
            except Exception as exc:
                progress.fail("downloading_model")
                click.echo(f"Error: {exc}", err=True)
                raise SystemExit(1) from None

    if not whisperx:
        from ownscribe.transcription.mlx_transcriber import resolve_mlx_repo

        click.echo(f"MLX Whisper model ready: {resolve_mlx_repo(config.transcription.model)}")
    else:
        click.echo(f"Whisper model ready: {config.transcription.model}")
        if config.transcription.language:
            click.echo(f"Alignment model ready: {config.transcription.language}")
        else:
            click.echo("Alignment model not preloaded (language auto-detect).")

    if diar_enabled:
        click.echo("Diarization pipeline ready.")
    elif hf_token_warning:
        click.echo(
            "Warning: Diarization enabled but no HF token configured. Skipping diarization warmup.",
            err=True,
        )

    if local_sum:
        click.echo(f"Summarization model ready: {config.summarization.model}")


def _load_transcript_text(transcript_path: Path) -> str:
    """Read a saved transcript back as the text the summarizer expects.

    transcript.md carries a [MM:SS] stamp on every segment and transcript.json
    carries every word with timings and scores; neither belongs in an LLM
    prompt. Parse the file back to a TranscriptResult so the summarizer sees the
    same normalised, speaker-aware text as the inline path. Falls back to the
    raw file contents for anything that is not a transcript ownscribe wrote.
    """
    raw = transcript_path.read_text()

    if transcript_path.suffix.lower() == ".json":
        from ownscribe.output.json_output import parse_transcript_json

        result = parse_transcript_json(raw)
    else:
        from ownscribe.output.markdown import parse_transcript

        result = parse_transcript(raw)

    if result is None or not result.segments:
        return raw
    return result.speaker_text


def run_summarize(config: Config, transcript_file: str) -> None:
    """Summarize a transcript file and save the summary alongside the input."""
    transcript_path = Path(transcript_file).resolve()
    transcript_text = _load_transcript_text(transcript_path)

    try:
        summarizer = create_summarizer(config)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from None
    if not summarizer.is_available():
        if config.summarization.backend == "local":
            click.echo(
                f"Error: Local summarization model '{config.summarization.model}' is not available.",
                err=True,
            )
        else:
            click.echo(
                f"Error: {config.summarization.backend} is not reachable at {config.summarization.host}. "
                "Is the server running?",
                err=True,
            )
        raise SystemExit(1)

    from ownscribe.output.markdown import format_summary

    out_dir = transcript_path.parent
    local_sum = config.summarization.backend == "local"

    try:
        with PipelineProgress(
            transcribe=False,
            diarize=False,
            summarize=True,
            download_summarizer=local_sum,
        ) as progress:
            progress.begin("summarizing")
            if local_sum:
                progress.begin("downloading_model")
                try:
                    _download_summarization_model(
                        config.summarization.model,
                        progress,
                        "downloading_model",
                    )
                    progress.complete("downloading_model")
                except Exception:
                    progress.fail("downloading_model")
                    click.echo(
                        f"Error: Failed to download summarization model '{config.summarization.model}'.\n"
                        "Check your internet connection and try again.",
                        err=True,
                    )
                    raise SystemExit(1) from None
            summary = summarizer.summarize(transcript_text)
            title = _generate_title(summary, summarizer)
            progress.complete("summarizing")
    finally:
        summarizer.close()

    summary_md = format_summary(summary)
    summary_path = out_dir / "summary.md"
    summary_path.write_text(summary_md)

    if title:
        # Retitle the recording's folder ("YYYY-MM-DD HHMM - Title"), and a separate
        # audio folder of the same name if one exists. _finalize_folder leaves folders
        # outside the output tree alone.
        audio_dir = out_dir
        if config.output.uses_separate_audio_dir:
            candidate = config.output.resolved_audio_dir / out_dir.name
            if candidate.is_dir():
                audio_dir = candidate
        out_dir, _ = _finalize_folder(config, out_dir, audio_dir / "recording.wav", title)

    summary_path = out_dir / "summary.md"

    click.echo(f"\n{summary_md}")
    click.echo(f"Summary saved to {summary_path}")


def _do_transcribe_and_summarize(
    config: Config,
    audio_path: Path,
    out_dir: Path,
    summarize: bool = True,
    meeting_title: str = "",
    bookmarks: list[float] | None = None,
) -> None:
    """Shared logic for transcribe + optional summarize.

    *meeting_title* (e.g. from the meeting app's window) names the folder and notes in
    place of a generated title. *bookmarks* are offsets in seconds into the recording.
    """
    diar_enabled = (
        config.transcription.backend != "mlx"
        and config.diarization.enabled
        and bool(config.diarization.hf_token)
    )
    sum_enabled = summarize and config.summarization.enabled

    summary = None
    summary_str = None
    title = ""
    sum_unavailable = False
    sum_failed = False

    local_sum = sum_enabled and config.summarization.backend == "local"

    with PipelineProgress(
        diarize=diar_enabled,
        summarize=sum_enabled,
        download_summarizer=local_sum,
    ) as progress:
        try:
            transcriber = _create_transcriber(config, progress=progress)
        except ImportError:
            click.echo(
                "Error: WhisperX is not installed. Install with:\n  uv pip install 'ownscribe[transcription]'",
                err=True,
            )
            raise SystemExit(1) from None

        result = _transcribe(config, transcriber, audio_path)
        no_speech = not any(seg.text.strip() for seg in result.segments)
        system_silent = config.speakers.mode == "channels" and _system_audio_was_silent(audio_path)

        # Save transcript — silent, no echo
        transcript_str, _ = _format_output(config, result)
        if bookmarks and config.output.format != "json":
            from ownscribe.output.markdown import format_transcript

            transcript_str = format_transcript(result, bookmarks)
        ext = "json" if config.output.format == "json" else "md"
        transcript_path = out_dir / f"transcript.{ext}"
        transcript_path.write_text(transcript_str)

        # Nothing to summarize: an LLM given an empty transcript just asks for one.
        if sum_enabled and no_speech:
            progress.complete("summarizing")
        elif sum_enabled:
            try:
                summarizer = create_summarizer(config)
            except ImportError as exc:
                click.echo(f"Error: {exc}", err=True)
                raise SystemExit(1) from None
            try:
                if not summarizer.is_available():
                    sum_unavailable = True
                else:
                    try:
                        progress.begin("summarizing")
                        if local_sum:
                            progress.begin("downloading_model")
                            _download_summarization_model(
                                config.summarization.model,
                                progress,
                                "downloading_model",
                            )
                            progress.complete("downloading_model")
                        if bookmarks:
                            from ownscribe.output.bookmarks import annotated_speaker_text

                            summary_input = annotated_speaker_text(result, bookmarks, config.speakers.me_label)
                        else:
                            summary_input = result.speaker_text
                        summary = summarizer.summarize(summary_input)
                        _, summary_str = _format_output(config, result, summary)
                        summary_path = out_dir / f"summary.{ext}"
                        summary_path.write_text(summary_str or summary)
                        if not meeting_title:
                            title = _generate_title(summary, summarizer)
                        progress.complete("summarizing")
                    except Exception:
                        progress.fail("summarizing")
                        sum_failed = True
            finally:
                summarizer.close()

    # --- All user-facing output after TUI exits ---
    click.echo(f"Transcript saved to {transcript_path}")

    if no_speech:
        click.echo("\nWarning: no speech was detected, so there is nothing to summarize or export.", err=True)
    if system_silent:
        click.echo(
            "Warning: the system-audio track is completely silent; no call or app audio was captured.\n"
            "If audio was playing, check System Settings > Privacy & Security > Screen & System Audio "
            "Recording for the app that started ownscribe (TeamsWatcher or your terminal), then restart it.",
            err=True,
        )

    if sum_unavailable:
        if config.summarization.backend == "local":
            click.echo(
                f"\nWarning: Local summarization model '{config.summarization.model}' is not available. "
                "Skipping summarization.",
                err=True,
            )
        else:
            click.echo(
                f"\nWarning: {config.summarization.backend} is not reachable "
                f"at {config.summarization.host}. "
                "Skipping summarization. Is the server running?",
                err=True,
            )
    elif sum_failed:
        click.echo(
            f"\nWarning: Summarization failed. "
            f"Transcript is saved at {transcript_path}\n"
            f"Resume with: ownscribe resume {out_dir}",
            err=True,
        )

    if summary is not None:
        click.echo(f"Summary saved to {out_dir / f'summary.{ext}'}")
        # summary.json holds machine-readable JSON; the console still shows the text.
        displayed = summary if config.output.format == "json" else (summary_str or summary)
        click.echo(f"\n{displayed}")
    elif not summarize:
        click.echo(f"\n{transcript_str}")

    # One folder per recording: "YYYY-MM-DD HHMM - Title", with the notes inside.
    notes_path = None
    if summarize:
        title = meeting_title or title or _default_title()
        out_dir, audio_path = _finalize_folder(config, out_dir, audio_path, title)
        if not no_speech:
            notes_path = _write_notes(config, out_dir, result, summary, title, bookmarks)
        click.echo(f"Saved to {out_dir}")

    transcript_path = out_dir / transcript_path.name
    summary_path = out_dir / f"summary.{ext}" if summary is not None else None

    _write_result_file(
        output_dir=out_dir,
        transcript=transcript_path,
        summary=summary_path,
        notes=notes_path,
        speech="none" if no_speech else None,
        system_audio="silent" if system_silent else None,
    )

    # Delete recording if configured — use the (possibly renamed) audio_path
    if not config.output.keep_recording:
        from ownscribe.transcription.channels import split_track_paths

        for track in split_track_paths(audio_path):
            track.unlink(missing_ok=True)
    if not config.output.keep_recording and audio_path.exists():
        audio_path.unlink()
        # Also remove the parent directory if it is now empty, which is expected
        # when a separate audio_dir is configured.
        if not any(audio_path.parent.iterdir()):
            audio_path.parent.rmdir()
        click.echo(f"Recording deleted (keep_recording=false): {audio_path}")


_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"}
# Video containers whose audio track ffmpeg/WhisperX can decode (e.g. meeting recordings).
_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv"}
_MEDIA_EXTENSIONS = _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS


def _find_audio(directory: Path) -> Path | None:
    """Find an audio or video file in directory, preferring 'recording.wav'."""
    recording = directory / "recording.wav"
    if recording.exists():
        return recording
    for f in directory.iterdir():
        if f.is_file() and f.suffix.lower() in _MEDIA_EXTENSIONS:
            return f
    return None


def _find_transcript(directory: Path) -> Path | None:
    """Find a transcript file in directory."""
    for ext in ("md", "json"):
        path = directory / f"transcript.{ext}"
        if path.exists():
            return path
    return None


def _find_summary(directory: Path) -> Path | None:
    """Find a summary file in directory."""
    for ext in ("md", "json"):
        path = directory / f"summary.{ext}"
        if path.exists():
            return path
    return None


def run_resume(config: Config, directory: str) -> None:
    """Resume a partially-completed pipeline in the given directory."""
    dir_path = Path(directory).resolve()
    if not dir_path.is_dir():
        click.echo(f"Error: {dir_path} is not a directory.", err=True)
        raise SystemExit(1)

    # First look for an audio file in the target directory, since the user is
    # explicitly resuming there. If none is found and a separate directory is
    # configured for audio, search that directory as well.
    audio = _find_audio(dir_path)
    if audio is None and config.output.uses_separate_audio_dir:
        candidate = config.output.resolved_audio_dir / dir_path.name
        if candidate != dir_path and candidate.is_dir():
            audio = _find_audio(candidate)
    transcript = _find_transcript(dir_path)
    summary = _find_summary(dir_path)

    if transcript and summary:
        click.echo("Nothing to resume — transcript and summary already exist.")
        return

    if not audio and not transcript:
        click.echo(
            f"Error: No audio or transcript found in {dir_path}.\nA recording or transcript is needed to resume.",
            err=True,
        )
        raise SystemExit(1)

    if transcript:
        # Have transcript, missing summary — summarize only
        click.echo(f"Found transcript: {transcript}")
        click.echo("Resuming: summarize only.\n")
        run_summarize(config, str(transcript))
    else:
        # Have audio, missing transcript (and summary) — full transcribe + summarize
        click.echo(f"Found audio: {audio}")
        click.echo("Resuming: transcribe + summarize.\n")
        _do_transcribe_and_summarize(config, audio, dir_path)
