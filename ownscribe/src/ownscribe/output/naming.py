"""Recording folder names.

Each recording lives in one folder named ``YYYY-MM-DD HHMM - Title`` (for example
``2026-10-19 1405 - Spec handoff``). While recording, before a title exists, the
folder is just ``YYYY-MM-DD HHMM``. Folders from upstream ownscribe
(``YYYY-MM-DD_HHMM`` or ``YYYY-MM-DD_HHMM_some-slug``) are still recognised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_NEW_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<h>\d{2})(?P<m>\d{2})(?: \((?P<n>\d+)\))?(?: - (?P<title>.+))?$")
_OLD_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<h>\d{2})(?P<m>\d{2})(?:_(?P<slug>.+))?$")
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_DUPLICATE_SUFFIX = re.compile(r" \(\d+\)$")


@dataclass(frozen=True)
class FolderName:
    date: str  # YYYY-MM-DD
    hour: str
    minute: str
    title: str | None  # None until the recording has been titled
    legacy: bool  # upstream style (underscores)

    @property
    def started(self) -> datetime:
        return datetime.strptime(f"{self.date} {self.hour}{self.minute}", "%Y-%m-%d %H%M")

    @property
    def stamp(self) -> str:
        return f"{self.date} {self.hour}{self.minute}"


def stamp_for(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H%M")


def safe_title(title: str, max_length: int = 80) -> str:
    """A title usable in file and folder names on macOS (and in Finder)."""
    cleaned = _UNSAFE_CHARS.sub(" ", title or "").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)[:max_length].rstrip(" .")
    return cleaned


def slug_to_title(slug: str) -> str:
    """'quarterly-planning' -> 'Quarterly planning' (for folders named by upstream ownscribe)."""
    words = slug.replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def parse_folder_name(name: str) -> FolderName | None:
    if m := _NEW_RE.match(name):
        title = m.group("title")
        if title:
            title = _DUPLICATE_SUFFIX.sub("", title)
        return FolderName(m.group("date"), m.group("h"), m.group("m"), title or None, legacy=False)
    if m := _OLD_RE.match(name):
        slug = m.group("slug")
        return FolderName(m.group("date"), m.group("h"), m.group("m"), slug_to_title(slug) if slug else None, True)
    return None


def folder_name(stamp: str, title: str | None) -> str:
    title = safe_title(title or "")
    return f"{stamp} - {title}" if title else stamp


def unique_path(path: Path, extension: str = "") -> Path:
    """*path*, or the first free 'name (2)', 'name (3)', ... next to it.

    Pass the file *extension* (e.g. ".md") for files; folder names may contain dots.
    """
    if not path.exists():
        return path
    stem = path.name[: -len(extension)] if extension and path.name.endswith(extension) else path.name
    suffix = extension if extension and path.name.endswith(extension) else ""
    n = 2
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1
