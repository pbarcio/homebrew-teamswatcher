"""One-time cleanup of the output folder into one readable folder per recording.

- Renames recording folders to "YYYY-MM-DD HHMM - Title" (from upstream's
  "YYYY-MM-DD_HHMM_slug" and from untitled "YYYY-MM-DD HHMM").
- Moves notes from the old separate Exports folder into the matching recording folder.
- Removes recording folders that are empty, and the Exports folder once it's empty.

`plan_tidy` only looks; `apply_tidy` makes the changes. Nothing is ever deleted except
empty folders (and macOS .DS_Store files inside them).
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from ownscribe.output.naming import folder_name, parse_folder_name, unique_path

_IGNORED_FILES = {".DS_Store"}


@dataclass
class TidyPlan:
    renames: list[tuple[Path, Path]] = field(default_factory=list)  # folder -> new folder
    moves: list[tuple[Path, Path]] = field(default_factory=list)  # export file -> notes path
    removals: list[Path] = field(default_factory=list)  # empty folders
    unmatched_exports: list[Path] = field(default_factory=list)
    in_use: list[Path] = field(default_factory=list)  # changed recently: may still be recording

    @property
    def empty(self) -> bool:
        return not (self.renames or self.moves or self.removals)


def _is_empty(folder: Path) -> bool:
    return all(p.name in _IGNORED_FILES for p in folder.rglob("*") if p.is_file())


def _last_change(folder: Path) -> float:
    """Newest modification time of the folder or anything in it (a growing recording counts)."""
    newest = folder.stat().st_mtime
    for p in folder.rglob("*"):
        with contextlib.suppress(OSError):
            newest = max(newest, p.stat().st_mtime)
    return newest


def _export_stamp_and_title(path: Path) -> tuple[str, str | None] | None:
    parsed = parse_folder_name(path.stem)  # export files share the folder naming scheme
    if parsed is None or parsed.legacy:
        return None
    return parsed.stamp, parsed.title


def plan_tidy(
    output_dir: Path,
    export_dirs: list[Path],
    default_title: str = "Recording",
    min_age_seconds: float = 600,
) -> TidyPlan:
    """What tidying would change. Folders changed in the last *min_age_seconds* are left
    alone, so a recording (or its processing) in progress is never renamed or removed."""
    plan = TidyPlan()
    cutoff = time.time() - min_age_seconds
    if not output_dir.is_dir():
        return plan

    exports: dict[str, list[Path]] = {}
    for export_dir in export_dirs:
        if export_dir.is_dir():
            for f in sorted(export_dir.glob("*.md")):
                if parsed := _export_stamp_and_title(f):
                    exports.setdefault(parsed[0], []).append(f)
                else:
                    plan.unmatched_exports.append(f)

    export_dir_set = {d.resolve() for d in export_dirs if d.exists()}
    taken = {p.name for p in output_dir.iterdir()}

    for folder in sorted(output_dir.iterdir()):
        if not folder.is_dir() or folder.resolve() in export_dir_set:
            continue
        parsed = parse_folder_name(folder.name)
        if parsed is None:
            continue
        if _last_change(folder) > cutoff:
            plan.in_use.append(folder)
            exports.pop(parsed.stamp, None)  # its notes stay in Exports for now
            continue
        if _is_empty(folder):
            plan.removals.append(folder)
            continue

        matching_exports = exports.pop(parsed.stamp, [])
        export_title = next(
            (t for f in matching_exports if (t := _export_stamp_and_title(f)[1])), None
        )
        title = parsed.title or export_title or default_title
        new_name = folder_name(parsed.stamp, title)
        target = folder
        if new_name != folder.name:
            target = output_dir / new_name
            if new_name in taken:
                target = unique_path(target)
            taken.add(target.name)
            plan.renames.append((folder, target))

        for i, export in enumerate(matching_exports):
            notes_name = f"{target.name}.md" if i == 0 else f"{target.name} ({i + 1}).md"
            if (folder / notes_name).exists():
                notes_name = f"{target.name} (from Exports).md"
            plan.moves.append((export, target / notes_name))

    for leftovers in exports.values():
        plan.unmatched_exports.extend(leftovers)

    for export_dir in export_dirs:
        if export_dir.is_dir():
            moved = {src for src, _ in plan.moves}
            remaining = [p for p in export_dir.rglob("*") if p.is_file() and p.name not in _IGNORED_FILES]
            if all(p in moved for p in remaining):
                plan.removals.append(export_dir)
    return plan


def _remove_empty_folder(folder: Path) -> None:
    for p in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if p.is_file() and p.name in _IGNORED_FILES:
            p.unlink()
        elif p.is_dir():
            p.rmdir()
    folder.rmdir()


def apply_tidy(plan: TidyPlan) -> list[str]:
    """Carry out *plan*. Returns problems (if any) as messages; continues past them."""
    problems = []
    renamed = {}
    for src, dst in plan.renames:
        try:
            src.rename(dst)
            renamed[src] = dst
        except OSError as e:
            problems.append(f"Could not rename {src.name}: {e}")
    for src, dst in plan.moves:
        # The folder may have kept its old name if its rename failed.
        target_dir = dst.parent
        if not target_dir.exists():
            original = next((s for s, d in plan.renames if d == target_dir), None)
            if original is not None and original.exists():
                dst = original / dst.name
        try:
            src.rename(unique_path(dst, ".md"))
        except OSError as e:
            problems.append(f"Could not move {src.name}: {e}")
    for folder in plan.removals:
        try:
            if folder.exists() and _is_empty(folder):
                _remove_empty_folder(folder)
        except OSError as e:
            problems.append(f"Could not remove {folder.name}: {e}")
    return problems
