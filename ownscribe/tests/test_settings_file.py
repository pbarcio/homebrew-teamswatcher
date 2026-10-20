"""Tests for comment-preserving edits to config.toml (`ownscribe settings`)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from ownscribe.cli import cli
from ownscribe.settings_file import SettingsError, apply_changes, current_values, set_value, toml_value

TEMPLATE = Path(__file__).resolve().parents[2] / "ownscribe-config.toml"

SAMPLE = """\
[audio]
mic = true

[transcription]
model = "large-v3-turbo"   # fast on the GPU
languages = ["en", "es"]   # switch freely
initial_prompt = "Contoso # not a comment, ERP."

[speakers]
me = ""                    # empty = account name
them = "Them"
"""


def test_changes_only_the_value_and_keeps_comments():
    out = set_value(SAMPLE, "transcription.model", "medium")
    assert 'model = "medium"   # fast on the GPU\n' in out
    assert out.replace('"medium"', '"large-v3-turbo"') == SAMPLE


def test_hash_inside_strings_is_not_a_comment():
    out = set_value(SAMPLE, "transcription.initial_prompt", "New # prompt")
    assert 'initial_prompt = "New # prompt"\n' in out
    assert tomllib.loads(out)["transcription"]["initial_prompt"] == "New # prompt"


def test_lists_quotes_and_unicode():
    out = set_value(SAMPLE, "transcription.languages", ["en", "es", "pt"])
    assert 'languages = ["en", "es", "pt"]   # switch freely' in out
    out = set_value(out, "speakers.me", 'José "Pepe" Núñez')
    assert tomllib.loads(out)["speakers"]["me"] == 'José "Pepe" Núñez'


def test_multiline_array_is_replaced_whole():
    text = '[transcription]\nlanguages = [\n  "en",\n  "es",\n]  # both\nmodel = "x"\n'
    out = set_value(text, "transcription.languages", ["en"])
    assert out == '[transcription]\nlanguages = ["en"]  # both\nmodel = "x"\n'


def test_missing_key_and_missing_section_are_added():
    out = set_value(SAMPLE, "export.copy_to", "auto")
    assert out.endswith('\n[export]\ncopy_to = "auto"\n')
    out = set_value(SAMPLE, "transcription.hotwords", "Contoso")
    lines = out.splitlines()
    assert lines[lines.index('initial_prompt = "Contoso # not a comment, ERP."') + 1] == 'hotwords = "Contoso"'
    assert tomllib.loads(out)["transcription"]["hotwords"] == "Contoso"


def test_same_key_in_other_sections_is_left_alone():
    text = '[output]\ndir = "~/a"\n\n[export]\ndir = ""\n'
    out = set_value(text, "output.dir", "~/b")
    assert out == '[output]\ndir = "~/b"\n\n[export]\ndir = ""\n'


def test_toml_value():
    assert toml_value(True) == "true"
    assert toml_value(["en", "es"]) == '["en", "es"]'
    assert toml_value('a"b\\c') == '"a\\"b\\\\c"'


def test_apply_changes_on_the_real_template(tmp_path):
    path = tmp_path / "config.toml"
    original = TEMPLATE.read_text()
    path.write_text(original)
    apply_changes(
        path,
        {
            "speakers.me": "Pierce",
            "transcription.languages": ["en"],
            "transcription.language": "en",
            "transcription.model": "medium",
            "output.dir": "~/Meetings",
            "export.copy_to": "auto",
            "export.include_transcript": False,
        },
    )
    text = path.read_text()
    data = tomllib.loads(text)
    assert data["speakers"]["me"] == "Pierce"
    assert data["transcription"]["languages"] == ["en"]
    assert data["transcription"]["model"] == "medium"
    assert data["output"]["dir"] == "~/Meetings"
    assert data["export"]["copy_to"] == "auto"
    assert data["export"]["include_transcript"] is False
    # Nothing else changed: same number of lines, and every comment line is still there.
    assert len(text.splitlines()) == len(original.splitlines())
    assert [line for line in text.splitlines() if line.lstrip().startswith("#")] == [
        line for line in original.splitlines() if line.lstrip().startswith("#")
    ]
    assert data["summarization"] == tomllib.loads(original)["summarization"]


def test_apply_changes_rejects_bad_input(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE)
    with pytest.raises(SettingsError):
        apply_changes(path, {"summarization.backend": "openai"})  # not editable from the GUI
    with pytest.raises(SettingsError):
        apply_changes(path, {"transcription.languages": "en"})
    with pytest.raises(SettingsError):
        apply_changes(path, {"export.notes": "yes"})
    assert path.read_text() == SAMPLE


def test_current_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE)
    values = current_values(path)
    assert values["transcription.languages"] == ["en", "es"]
    assert values["speakers.them"] == "Them"
    assert values["export.include_transcript"] is True  # default
    assert values["speakers.default_me"]  # the macOS account name, shown as a placeholder


def test_cli_show_and_set(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE)
    runner = CliRunner()
    with mock.patch("ownscribe.config.CONFIG_PATH", path):
        shown = json.loads(runner.invoke(cli, ["settings", "show"]).output)
        assert shown["transcription.model"] == "large-v3-turbo"

        result = runner.invoke(cli, ["settings", "set", "--json", '{"speakers.them": "Client"}'])
        assert result.exit_code == 0
        assert tomllib.loads(path.read_text())["speakers"]["them"] == "Client"

        bad = runner.invoke(cli, ["settings", "set", "--json", '{"speakers.them": 3}'])
        assert bad.exit_code == 1
        assert "must be a str" in bad.output
