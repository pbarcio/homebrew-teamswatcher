from __future__ import annotations

import json
from datetime import date

from ownscribe.config import Config
from ownscribe.meeting_qa import (
    ask,
    build_passages,
    cited_numbers,
    discover_meetings,
    parse_date_range,
    prepare,
    rank_passages,
    tokenize,
)

TODAY = date(2026, 10, 21)  # a Wednesday


def _meeting(root, name, lines, summary=None):
    folder = root / name
    folder.mkdir()
    body = ["# Transcript", "", "**Duration:** 10:00  ", ""]
    for speaker, stamp, text in lines:
        body += [f"\n**{speaker}** [{stamp}]", text]
    (folder / "transcript.md").write_text("\n".join(body) + "\n")
    (folder / f"{name}.md").write_text("---\ntitle: x\n---\n")
    if summary:
        (folder / "summary.md").write_text("# Meeting Summary\n\n" + summary + "\n")
    return folder


def _config(root) -> Config:
    config = Config()
    config.output.dir = str(root)
    config.speakers.me = "Pierce"
    return config


class FakeModel:
    def __init__(self, answer: str):
        self.answer = answer
        self.prompts: list[tuple[str, str]] = []

    def chat(self, system, prompt, json_mode=False, json_schema=None):
        self.prompts.append((system, prompt))
        return self.answer

    def close(self):
        pass


def _setup(tmp_path):
    _meeting(
        tmp_path,
        "2026-10-13 0930 - Cutover planning",
        [
            ("Them", "00:05", "We need to pick a cutover date for the warehouse go-live."),
            ("Pierce", "00:20", "I'd propose November 2nd, after the freeze."),
        ],
    )
    _meeting(
        tmp_path,
        "2026-10-20 1400 - Vendor sync",
        [
            ("Them", "00:10", "The cutover is now moving to November 9th because of testing."),
            ("Them", "03:00", "Pierce, can you send the updated runbook by Thursday?"),
        ],
        summary="- Cutover moved to November 9th.",
    )
    _meeting(tmp_path, "2026-10-21 0900 - Standup", [("Them", "00:03", "Lunch is pizza today.")])


def test_discover_finds_titled_and_untitled_folders(tmp_path):
    _setup(tmp_path)
    (tmp_path / "2026-10-21 1100").mkdir()  # still recording: nothing to read
    (tmp_path / "2026-10-21 1100" / "recording.wav").write_bytes(b"")
    (tmp_path / "Exports").mkdir()
    docs = discover_meetings(tmp_path)
    assert [d.title for d in docs] == ["Standup", "Vendor sync", "Cutover planning"]
    assert docs[1].summary is not None and docs[1].notes.name == "2026-10-20 1400 - Vendor sync.md"


def test_passages_keep_speakers_and_times(tmp_path):
    _setup(tmp_path)
    doc = next(d for d in discover_meetings(tmp_path) if d.title == "Vendor sync")
    passages = build_passages(doc, words_per_passage=8)
    assert passages[0].kind == "summary"
    assert passages[1].start == 10.0 and passages[1].text.startswith("Them: The cutover")
    assert passages[2].start == 180.0


def test_tokenize_matches_word_forms():
    assert tokenize("decided deadlines") == tokenize("decide deadline")
    assert tokenize("What did we discuss about the cutover?") == ["cutover"]


def test_ranking_prefers_passages_matching_more_words(tmp_path):
    _setup(tmp_path)
    passages = [p for d in discover_meetings(tmp_path) for p in build_passages(d)]
    ranked = rank_passages("updated runbook Thursday", passages)
    assert ranked and "runbook" in ranked[0].text
    assert rank_passages("pizza", passages)[0].meeting.title == "Standup"
    assert rank_passages("kubernetes", passages) == []


def test_date_ranges():
    assert parse_date_range("what happened yesterday?", TODAY).start == date(2026, 10, 20)
    last_week = parse_date_range("decisions from last week", TODAY)
    assert (last_week.start, last_week.end) == (date(2026, 10, 12), date(2026, 10, 18))
    this_week = parse_date_range("this week", TODAY)
    assert (this_week.start, this_week.end) == (date(2026, 10, 19), TODAY)
    assert parse_date_range("on Monday", TODAY).start == date(2026, 10, 19)
    assert parse_date_range("last Wednesday", TODAY).start == date(2026, 10, 14)
    assert parse_date_range("past 3 days", TODAY).start == date(2026, 10, 19)
    assert parse_date_range("the Friday deadline", TODAY) is None
    assert parse_date_range("cutover date", TODAY) is None


def test_prepare_filters_by_date_and_orders_oldest_first(tmp_path):
    _setup(tmp_path)
    config = _config(tmp_path)
    everything = prepare(config, "cutover date", 5000, today=TODAY)
    titles = [p.meeting.title for p in everything.passages]
    assert titles[0] == "Cutover planning" and "Vendor sync" in titles

    yesterday = prepare(config, "cutover yesterday", 5000, today=TODAY)
    assert {p.meeting.title for p in yesterday.passages} == {"Vendor sync"}


def test_prepare_overview_when_question_has_no_search_words(tmp_path):
    _setup(tmp_path)
    prepared = prepare(_config(tmp_path), "what happened yesterday?", 5000, today=TODAY)
    assert prepared.passages and prepared.passages[0].kind == "summary"


def test_cited_numbers():
    assert cited_numbers("Moved [2]. Runbook due [1][3], see [2, 4] and [9].", 4) == [2, 1, 3, 4]


def test_ask_json_streams_answer_and_cited_sources(tmp_path, capsys):
    _setup(tmp_path)
    model = FakeModel("The cutover moved to November 9th [2], from November 2nd [1].")
    status = ask(_config(tmp_path), "When is the cutover?", as_json=True, summarizer=model)
    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "status" and "token" in kinds and kinds[-2:] == ["answer", "sources"]
    sources = events[-1]["sources"]
    assert [s["ref"] for s in sources] == [2, 1]
    assert {s["title"] for s in sources} <= {"Cutover planning", "Vendor sync"}
    assert all(s["notes"].endswith(".md") for s in sources)

    system, prompt = model.prompts[0]
    assert "Pierce" in system
    assert "[1] " in prompt and "When is the cutover?" in prompt


def test_ask_without_matches_skips_the_model(tmp_path, capsys):
    _setup(tmp_path)
    model = FakeModel("should not be used")
    assert ask(_config(tmp_path), "kubernetes migration", as_json=True, summarizer=model) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    answer = next(e for e in events if e["event"] == "answer")
    assert "couldn't find" in answer["text"] and "3 meetings" in answer["text"]
    assert model.prompts == []


def test_ask_plain_text(tmp_path, capsys):
    _setup(tmp_path)
    ask(_config(tmp_path), "runbook", summarizer=FakeModel("Send the runbook by Thursday [1]."))
    out = capsys.readouterr().out
    assert out.startswith("Send the runbook by Thursday [1].")
    assert "Sources:" in out and "Vendor sync at 0:10" in out
