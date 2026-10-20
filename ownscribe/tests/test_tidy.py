"""Tests for `ownscribe tidy`."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from ownscribe.cli import cli
from ownscribe.config import Config
from ownscribe.tidy import apply_tidy, plan_tidy


def _messy_tree(root: Path) -> tuple[Path, Path]:
    out = root / "ownscribe"
    exports = out / "Exports"
    exports.mkdir(parents=True)
    # upstream style with a slug
    (out / "2026-10-18_0900_budget-review").mkdir()
    (out / "2026-10-18_0900_budget-review" / "transcript.md").write_text("t")
    # untitled upstream style, with a matching export
    (out / "2026-10-19_1405").mkdir()
    (out / "2026-10-19_1405" / "transcript.md").write_text("t")
    (exports / "2026-10-19 1405 - Microsoft Teams call.md").write_text("notes")
    # empty folder from a failed run (only a .DS_Store)
    (out / "2026-10-19_1500").mkdir()
    (out / "2026-10-19_1500" / ".DS_Store").write_text("")
    # already tidy
    (out / "2026-10-20 1000 - Standup").mkdir()
    (out / "2026-10-20 1000 - Standup" / "transcript.md").write_text("t")
    # unrelated things stay put
    (out / "My notes").mkdir()
    (out / "My notes" / "keep.txt").write_text("x")
    (exports / ".DS_Store").write_text("")
    return out, exports


def test_plan_and_apply(tmp_path):
    out, exports = _messy_tree(tmp_path)
    plan = plan_tidy(out, [exports], min_age_seconds=0)

    assert [(s.name, d.name) for s, d in plan.renames] == [
        ("2026-10-18_0900_budget-review", "2026-10-18 0900 - Budget review"),
        ("2026-10-19_1405", "2026-10-19 1405 - Microsoft Teams call"),
    ]
    assert [(s.name, d.parent.name, d.name) for s, d in plan.moves] == [
        (
            "2026-10-19 1405 - Microsoft Teams call.md",
            "2026-10-19 1405 - Microsoft Teams call",
            "2026-10-19 1405 - Microsoft Teams call.md",
        )
    ]
    assert sorted(p.name for p in plan.removals) == ["2026-10-19_1500", "Exports"]
    # Planning changes nothing.
    assert (out / "2026-10-19_1405").exists()

    assert apply_tidy(plan) == []
    assert sorted(p.name for p in out.iterdir()) == [
        "2026-10-18 0900 - Budget review",
        "2026-10-19 1405 - Microsoft Teams call",
        "2026-10-20 1000 - Standup",
        "My notes",
    ]
    teams = out / "2026-10-19 1405 - Microsoft Teams call"
    assert sorted(p.name for p in teams.iterdir()) == ["2026-10-19 1405 - Microsoft Teams call.md", "transcript.md"]
    assert (out / "My notes" / "keep.txt").exists()

    # Running again finds nothing to do.
    assert plan_tidy(out, [exports], min_age_seconds=0).empty


def test_unmatched_exports_keep_the_exports_folder(tmp_path):
    out, exports = _messy_tree(tmp_path)
    (exports / "2026-09-01 0800 - Old meeting.md").write_text("orphan")
    plan = plan_tidy(out, [exports], min_age_seconds=0)
    assert [p.name for p in plan.unmatched_exports] == ["2026-09-01 0800 - Old meeting.md"]
    assert exports not in plan.removals
    apply_tidy(plan)
    assert (exports / "2026-09-01 0800 - Old meeting.md").exists()


def test_name_clash_gets_a_suffix(tmp_path):
    out = tmp_path / "ownscribe"
    (out / "2026-10-19_1405_sync").mkdir(parents=True)
    (out / "2026-10-19_1405_sync" / "transcript.md").write_text("t")
    (out / "2026-10-19 1405 - Sync").mkdir()
    (out / "2026-10-19 1405 - Sync" / "transcript.md").write_text("t")
    plan = plan_tidy(out, [], min_age_seconds=0)
    assert [d.name for _, d in plan.renames] == ["2026-10-19 1405 - Sync (2)"]


def _age(root: Path, seconds: float = 3600) -> None:
    import os
    import time

    old = time.time() - seconds
    for p in [root, *root.rglob("*")]:
        os.utime(p, (old, old))


def test_recent_folders_are_left_alone(tmp_path):
    out, exports = _messy_tree(tmp_path)
    _age(out)
    (out / "2026-10-19_1405" / "recording.me.wav.tmp").write_text("growing")  # recording in progress
    plan = plan_tidy(out, [exports])
    assert [p.name for p in plan.in_use] == ["2026-10-19_1405"]
    assert "2026-10-19_1405" not in [s.name for s, _ in plan.renames]
    assert plan.moves == []
    assert exports not in plan.removals


def test_cli_previews_then_applies(tmp_path):
    out, _ = _messy_tree(tmp_path)
    _age(out)
    config = Config()
    config.output.dir = str(out)
    runner = CliRunner()
    with (
        mock.patch("ownscribe.cli.Config.load", return_value=config),
        mock.patch("ownscribe.config._LEGACY_EXPORT_DIR", str(out / "Exports")),
    ):
        preview = runner.invoke(cli, ["tidy"])
        assert preview.exit_code == 0
        assert "2026-10-19_1405  ->  2026-10-19 1405 - Microsoft Teams call" in preview.output
        assert "Nothing has been changed" in preview.output
        assert (out / "2026-10-19_1405").exists()

        applied = runner.invoke(cli, ["tidy", "--apply"])
        assert applied.exit_code == 0
        assert "Done." in applied.output
        assert not (out / "Exports").exists()

        again = runner.invoke(cli, ["tidy"])
        assert "already tidy" in again.output
