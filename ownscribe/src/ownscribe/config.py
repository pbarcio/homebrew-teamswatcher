"""Configuration management with TOML loading and defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path("~/.config/ownscribe").expanduser()
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG_TOML = """\
[audio]
backend = "coreaudio"     # "coreaudio" (default) or "sounddevice"
device = ""               # empty = system audio; or device name/index for sounddevice
mic = true                # also capture microphone input
mic_device = ""           # specific mic device name (empty = default)
capture_mode = "all"      # "all" = capture all system audio directly; "picker" = show source picker
silence_timeout = 300     # seconds of silence before auto-stop; 0 = disabled
# apps = []               # bundle-ID prefixes to capture, e.g. ["com.microsoft.teams"]; empty = all audio

[transcription]
backend = "whisperx"      # "whisperx" (CPU) or "mlx" (Apple Silicon GPU; needs ownscribe[mlx])
model = "base"            # whisper model: tiny, base, small, medium, large-v3, large-v3-turbo (mlx)
language = ""             # empty = auto-detect
threads = 0               # CPU threads for transcription; 0 = auto-detect from core count
# initial_prompt = ""     # prime Whisper with context: domain vocab, speaker names, expected phrases
# hotwords = ""           # comma-separated words to boost recognition (softer hint than initial_prompt)

[diarization]
enabled = false           # set to true + provide hf_token to enable
hf_token = ""             # HuggingFace token for pyannote models
min_speakers = 0          # 0 = auto-detect
max_speakers = 0
telemetry = false         # set to true to allow HuggingFace Hub + pyannote metrics telemetry
device = "auto"           # "auto" (mps if available), "mps", or "cpu"

[speakers]
mode = "channels"         # "channels" = label your mic as you and system audio as "them"; "none" = no labels
me = ""                   # your label; empty = your macOS account name
them = "Them"             # label for everyone on the other side of the call

[summarization]
enabled = true
backend = "local"         # "local" (built-in, no server needed), "ollama", or "openai"
model = "phi-4-mini"      # local: "phi-4-mini", path to GGUF, or hf:owner/repo/file.gguf; ollama/openai: model name
# host = "http://localhost:11434"  # only for ollama/openai backends
# api_key = ""            # only for openai backend; required by servers like oMLX (or set OPENAI_API_KEY)
# template = "meeting"    # built-in: "meeting", "lecture", or "brief"
# context_size = 0        # context window in tokens; 0 = auto-detect (8192 for local). Longer
                          # transcripts are summarized in chunks and merged, whatever the size.

# Custom templates (optional):
# [templates.my-notes]
# system_prompt = "You are a helpful assistant."
# prompt = "Summarize:\\n{transcript}"

[output]
dir = "~/ownscribe"       # base output directory
audio_dir = ""            # directory for audio recordings; empty = same as dir
format = "markdown"       # "markdown" or "json"
keep_recording = true     # keep WAV files after transcription; false = auto-delete

[export]
dir = ""                  # also save title + summary + transcript as one Markdown file here;
                          # "auto" = a "Meeting Notes" folder in your work OneDrive; empty = off
include_transcript = true
"""


@dataclass
class AudioConfig:
    backend: str = "coreaudio"
    device: str = ""
    mic: bool = True
    mic_device: str = ""
    capture_mode: str = "all"  # "all" = all system audio; "picker" = show source picker
    silence_timeout: int = 300  # seconds of silence before auto-stop; 0 = disabled
    apps: list[str] = field(default_factory=list)  # bundle-ID prefixes to capture; empty = all


@dataclass
class TranscriptionConfig:
    backend: str = "whisperx"  # "whisperx" or "mlx"
    model: str = "base"
    language: str = ""
    initial_prompt: str = ""
    hotwords: str = ""
    threads: int = 0  # CPU threads for transcription; 0 = auto-detect from core count


@dataclass
class DiarizationConfig:
    enabled: bool = False
    hf_token: str = ""
    min_speakers: int = 0
    max_speakers: int = 0
    telemetry: bool = False
    device: str = "auto"


@dataclass
class SpeakersConfig:
    mode: str = "channels"  # "channels" = me (mic) vs them (system audio); "none"
    me: str = ""  # empty = macOS account name
    them: str = "Them"

    @property
    def me_label(self) -> str:
        if self.me.strip():
            return self.me.strip()
        return _account_name() or "Me"

    @property
    def them_label(self) -> str:
        return self.them.strip() or "Them"


def _account_name() -> str:
    """Full name of the logged-in account (e.g. "Pierce Barcio"), or "" if unknown."""
    try:
        import pwd

        gecos = pwd.getpwuid(os.getuid()).pw_gecos
    except (ImportError, KeyError):
        return ""
    return gecos.split(",")[0].strip()


@dataclass
class ExportConfig:
    dir: str = ""  # "" = off; "auto" = work OneDrive "Meeting Notes"; or a path
    include_transcript: bool = True


@dataclass
class SummarizationConfig:
    enabled: bool = True
    backend: str = "local"
    model: str = "phi-4-mini"
    host: str = "http://localhost:11434"
    api_key: str = ""
    template: str = ""
    context_size: int = 0


@dataclass
class TemplateConfig:
    system_prompt: str = ""
    prompt: str = ""


@dataclass
class OutputConfig:
    dir: str = "~/ownscribe"
    audio_dir: str = ""
    format: str = "markdown"
    keep_recording: bool = True

    @property
    def resolved_dir(self) -> Path:
        return Path(self.dir).expanduser()

    @property
    def resolved_audio_dir(self) -> Path:
        return Path(self.audio_dir).expanduser() if self.audio_dir else self.resolved_dir

    @property
    def uses_separate_audio_dir(self) -> bool:
        return self.resolved_audio_dir != self.resolved_dir

@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    diarization: DiarizationConfig = field(default_factory=DiarizationConfig)
    summarization: SummarizationConfig = field(default_factory=SummarizationConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    speakers: SpeakersConfig = field(default_factory=SpeakersConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    templates: dict[str, TemplateConfig] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        """Load config from TOML file, falling back to defaults."""
        config = cls()

        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
            config = _merge_toml(config, data)

        # Env var overrides
        if hf_token := os.environ.get("HF_TOKEN"):
            config.diarization.hf_token = hf_token
        if ollama_host := os.environ.get("OLLAMA_HOST"):
            config.summarization.host = ollama_host
        if api_key := os.environ.get("OPENAI_API_KEY"):
            config.summarization.api_key = api_key

        return config


def _merge_toml(config: Config, data: dict) -> Config:
    """Merge TOML data into config dataclass."""
    if "audio" in data:
        for k, v in data["audio"].items():
            if hasattr(config.audio, k):
                setattr(config.audio, k, v)

    if "transcription" in data:
        for k, v in data["transcription"].items():
            if hasattr(config.transcription, k):
                setattr(config.transcription, k, v)

    if "diarization" in data:
        for k, v in data["diarization"].items():
            if hasattr(config.diarization, k):
                setattr(config.diarization, k, v)

    if "summarization" in data:
        for k, v in data["summarization"].items():
            if hasattr(config.summarization, k):
                setattr(config.summarization, k, v)

    if "output" in data:
        for k, v in data["output"].items():
            if hasattr(config.output, k):
                setattr(config.output, k, v)

    if "speakers" in data:
        for k, v in data["speakers"].items():
            if hasattr(config.speakers, k):
                setattr(config.speakers, k, v)

    if "export" in data:
        for k, v in data["export"].items():
            if hasattr(config.export, k):
                setattr(config.export, k, v)

    if "templates" in data:
        for name, t_data in data["templates"].items():
            config.templates[name] = TemplateConfig(
                system_prompt=t_data.get("system_prompt", ""),
                prompt=t_data.get("prompt", ""),
            )

    return config


def ensure_config_file() -> Path:
    """Create default config file if it doesn't exist. Returns the path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
    return CONFIG_PATH
