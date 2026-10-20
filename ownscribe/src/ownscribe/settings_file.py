"""Read and change individual settings in config.toml without disturbing the rest of it.

Used by `ownscribe settings show/set`, which TeamsWatcher's Settings window calls. Only
the line holding a changed key is rewritten, keeping its trailing comment; every other
line, comment and blank line stays exactly as it was. The result is checked with tomllib
before anything is written.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from ownscribe.config import Config, _merge_toml

# Settings the GUI may read and change: "section.key" -> type
EDITABLE: dict[str, type] = {
    "speakers.me": str,
    "speakers.them": str,
    "transcription.language": str,
    "transcription.languages": list,
    "transcription.model": str,
    "transcription.initial_prompt": str,
    "transcription.hotwords": str,
    "output.dir": str,
    "export.notes": bool,
    "export.include_transcript": bool,
    "export.copy_to": str,
}

_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


class SettingsError(ValueError):
    pass


def current_values(config_path: Path) -> dict[str, Any]:
    """Effective values (file merged over defaults) of the editable settings."""
    config = Config()
    if config_path.exists():
        with open(config_path, "rb") as f:
            config = _merge_toml(config, tomllib.load(f))
    values: dict[str, Any] = {}
    for dotted in EDITABLE:
        section, key = dotted.split(".")
        values[dotted] = getattr(getattr(config, section), key)
    values["speakers.default_me"] = config.speakers.me_label if not config.speakers.me.strip() else ""
    return values


def toml_value(value: Any) -> str:
    """A TOML literal for a str, bool, int, float or list of those."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are valid TOML basic-string escapes.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise SettingsError(f"Unsupported value: {value!r}")


def _validate(dotted: str, value: Any) -> None:
    expected = EDITABLE.get(dotted)
    if expected is None:
        raise SettingsError(f"{dotted} can't be changed here")
    if expected is list:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{dotted} must be a list of strings")
    elif expected is bool:
        if not isinstance(value, bool):
            raise SettingsError(f"{dotted} must be true or false")
    elif not isinstance(value, expected):
        raise SettingsError(f"{dotted} must be a {expected.__name__}")


def _comment_start(line: str, start: int) -> int | None:
    """Index of a trailing '#' comment in *line* after *start*, ignoring '#' inside strings."""
    quote = None
    i = start
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "#":
            return i
        i += 1
    return None


def _value_spans_lines(lines: list[str], index: int, value_start: int) -> int:
    """Index of the last line of a value that starts at lines[index][value_start:].

    Arrays may continue over several lines until their brackets balance.
    """
    depth = 0
    for j in range(index, len(lines)):
        line = lines[j]
        start = value_start if j == index else 0
        end = _comment_start(line, start)
        segment = line[start:end]
        quote = None
        k = 0
        while k < len(segment):
            ch = segment[k]
            if quote:
                if ch == "\\" and quote == '"':
                    k += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            k += 1
        if depth <= 0:
            return j
    return len(lines) - 1


def set_value(text: str, dotted: str, value: Any) -> str:
    """Return *text* with section.key set to *value*, changing as little as possible."""
    section, key = dotted.split(".")
    lines = text.splitlines(keepends=True)
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*)=(\s*)")
    new_value = toml_value(value)

    current = None
    section_start = None
    last_setting = None
    for i, line in enumerate(lines):
        if m := _SECTION_RE.match(line):
            current = m.group(1).strip()
            if current == section:
                section_start = i
                last_setting = i
            continue
        if current != section:
            continue
        if m := key_re.match(line):
            value_start = m.end()
            last = _value_spans_lines(lines, i, value_start)
            tail_line = lines[last]
            comment_at = _comment_start(tail_line, value_start if last == i else 0)
            comment = ""
            if comment_at is not None:
                # Keep the comment, roughly where it was.
                gap = re.search(r"\s*$", tail_line[:comment_at]).group(0)
                comment = (gap or " ") + tail_line[comment_at:].rstrip("\r\n")
            newline = "\n" if tail_line.endswith("\n") else ""
            lines[i : last + 1] = [line[:value_start] + new_value + comment + newline]
            return "".join(lines)
        if line.strip() and not line.lstrip().startswith("#"):
            last_setting = i

    entry = f"{key} = {new_value}\n"
    if section_start is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        return text + prefix + f"\n[{section}]\n" + entry
    insert_at = (last_setting if last_setting is not None else section_start) + 1
    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    lines.insert(insert_at, entry)
    return "".join(lines)


def apply_changes(config_path: Path, changes: dict[str, Any]) -> None:
    """Validate and write *changes* ({"section.key": value}) to *config_path*."""
    for dotted, value in changes.items():
        _validate(dotted, value)
    text = config_path.read_text() if config_path.exists() else ""
    for dotted, value in changes.items():
        text = set_value(text, dotted, value)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"The change would make config.toml invalid: {exc}") from exc
    for dotted, value in changes.items():
        section, key = dotted.split(".")
        if parsed.get(section, {}).get(key) != value:
            raise SettingsError(f"Couldn't set {dotted}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(config_path)
