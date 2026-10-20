"""Ask my meetings: answer questions across all transcripts, entirely on this Mac.

Unlike the upstream two-stage search (search.py), this works without summaries:

1. Every recording's transcript is split into short passages (and its summary, if any,
   is one more passage).
2. The passages are ranked against the question with BM25 keyword scoring, limited to
   a date range when the question names one ("yesterday", "last week", ...).
3. The best passages go to the local model, which answers and cites them as [1], [2].

Helper functions return data; ``ask()`` prints (plain text, or JSON lines for apps).
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ownscribe.config import Config
from ownscribe.output.naming import parse_folder_name
from ownscribe.summarization.base import estimate_tokens

# -- Data -------------------------------------------------------------------


@dataclass
class MeetingDoc:
    folder: Path
    title: str
    started: datetime
    notes: Path | None
    transcript: Path | None
    summary: Path | None

    @property
    def label(self) -> str:
        return f"{self.started:%Y-%m-%d %H:%M} · {self.title}"


@dataclass
class Passage:
    meeting: MeetingDoc
    text: str  # "Speaker: words" lines
    start: float | None  # seconds into the recording; None for the summary
    kind: str = "transcript"  # or "summary"
    tokens: list[str] = field(default_factory=list)
    score: float = 0.0


# -- Discovery --------------------------------------------------------------


def discover_meetings(output_dir: Path) -> list[MeetingDoc]:
    """Recording folders under *output_dir* that have something to read, newest first."""
    if not output_dir.is_dir():
        return []
    found: list[MeetingDoc] = []
    for folder in output_dir.iterdir():
        if not folder.is_dir():
            continue
        parsed = parse_folder_name(folder.name)
        if parsed is None:
            continue
        try:
            started = parsed.started
        except ValueError:
            continue
        transcript = folder / "transcript.md"
        summary = folder / "summary.md"
        notes = folder / f"{folder.name}.md"
        if not notes.exists():
            candidates = sorted(p for p in folder.glob("*.md") if p.name not in ("transcript.md", "summary.md"))
            notes = candidates[0] if candidates else None
        doc = MeetingDoc(
            folder=folder,
            title=parsed.title or "Recording",
            started=started,
            notes=notes,
            transcript=transcript if transcript.exists() else None,
            summary=summary if summary.exists() else None,
        )
        if doc.transcript or doc.summary or doc.notes:
            found.append(doc)
    found.sort(key=lambda d: d.started, reverse=True)
    return found


# -- Passages ---------------------------------------------------------------

_PASSAGE_WORDS = 160


def _transcript_text(doc: MeetingDoc) -> str:
    if doc.transcript:
        return doc.transcript.read_text(errors="replace")
    if doc.notes:
        # A notes file carries the transcript under "## Transcript".
        text = doc.notes.read_text(errors="replace")
        marker = text.find("\n## Transcript")
        if marker >= 0:
            return text[marker + 1 :].split("\n", 1)[-1]
    return ""


def _strip_summary(text: str) -> str:
    return re.sub(r"^# Meeting Summary\s*", "", text.strip())


def build_passages(doc: MeetingDoc, words_per_passage: int = _PASSAGE_WORDS) -> list[Passage]:
    from ownscribe.output.markdown import parse_transcript

    passages: list[Passage] = []
    if doc.summary:
        summary = _strip_summary(doc.summary.read_text(errors="replace"))
        if summary:
            passages.append(Passage(doc, summary, None, kind="summary"))

    result = parse_transcript(_transcript_text(doc))
    if result is None:
        return passages

    lines: list[str] = []
    count = 0
    start: float | None = None
    speaker: str | None = None
    for seg in result.segments:
        text = seg.text.strip()
        if not text:
            continue
        if start is None:
            start = seg.start
        if lines and seg.speaker == speaker:
            lines[-1] += " " + text
        else:
            lines.append(f"{seg.speaker}: {text}" if seg.speaker else text)
            speaker = seg.speaker
        count += len(text.split())
        if count >= words_per_passage:
            passages.append(Passage(doc, "\n".join(lines), start))
            lines, count, start, speaker = [], 0, None, None
    if lines:
        passages.append(Passage(doc, "\n".join(lines), start))
    return passages


# -- Words ------------------------------------------------------------------

_STOP_WORDS = """a an the is are was were be been being have has had do does did will would shall should may might
    can could of in to for on with at by from about into through during before after above below between out
    off over under again further then once here there when where why how all each every both few more most
    other some such no nor not only own same so than too very and but or if what which who whom whose this
    that these those i me my myself we our ours ourselves you your yours yourself yourselves he him his
    himself she her hers herself it its itself they them their theirs themselves am s t d ll ve re m don
    didn doesn isn wasn aren weren won any anything something anyone someone
    tell show give find know think get got let us please just also really like okay ok yeah yes
    meeting meetings call calls talk talked talking discuss discussed discussion say said says mention
    mentioned mentions conversation summarize summary recap anybody everyone
    el la los las un una unos unas de del al y o que en es por para con se su sus lo le les me mi"""
_STOP = frozenset(_STOP_WORDS.split())


def _stem(word: str) -> str:
    """A light suffix strip so 'deadlines'/'deadline' and 'migrating'/'migrated' match."""
    for suffix, minimum in (("ies", 5), ("ing", 6), ("ed", 5), ("es", 5), ("s", 4)):
        if word.endswith(suffix) and len(word) >= minimum:
            if suffix == "ies":
                return word[:-3] + "y"
            if suffix == "es" and not word.endswith(("ses", "xes", "zes", "ches", "shes")):
                word = word[:-1]
            else:
                word = word[: -len(suffix)]
            break
    # "decide" / "decided" / "decides" all become "decid".
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


_WORD_RE = re.compile(r"[a-z0-9áéíóúñü]+(?:'[a-z]+)?")


def tokenize(text: str, keep_stop_words: bool = False) -> list[str]:
    words = [w.split("'")[0] for w in _WORD_RE.findall(text.lower())]
    return [_stem(w) for w in words if keep_stop_words or (w not in _STOP and len(w) > 1)]


# -- Dates in questions -----------------------------------------------------

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass
class DateRange:
    start: date
    end: date  # inclusive
    phrase: str


def parse_date_range(question: str, today: date | None = None) -> DateRange | None:
    """A date range named in the question ("yesterday", "last week", "past 3 days", ...)."""
    today = today or date.today()
    q = question.lower()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    if m := re.search(r"\b(?:past|last)\s+(\d{1,3})\s+(day|week|month)s?\b", q):
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return DateRange(today - timedelta(days=days - 1 if unit == "day" else days), today, m.group(0))
    if m := re.search(r"\btoday\b|\bthis morning\b|\bthis afternoon\b|\bearlier today\b", q):
        return DateRange(today, today, m.group(0))
    if m := re.search(r"\byesterday\b", q):
        day = today - timedelta(days=1)
        return DateRange(day, day, m.group(0))
    if m := re.search(r"\blast week\b|\bprevious week\b", q):
        start = week_start - timedelta(days=7)
        return DateRange(start, start + timedelta(days=6), m.group(0))
    if m := re.search(r"\bthis week\b", q):
        return DateRange(week_start, today, m.group(0))
    if m := re.search(r"\blast month\b|\bprevious month\b", q):
        end = month_start - timedelta(days=1)
        return DateRange(end.replace(day=1), end, m.group(0))
    if m := re.search(r"\bthis month\b", q):
        return DateRange(month_start, today, m.group(0))
    # "on Monday", "last Friday", "Tuesday's call"; a bare "the Friday deadline" is not a date.
    if m := re.search(r"\b(?:on|last|this past)\s+(" + "|".join(_WEEKDAYS) + r")\b|\b("
                      + "|".join(_WEEKDAYS) + r")'s\b", q):
        target = _WEEKDAYS.index(m.group(1) or m.group(2))
        back = (today.weekday() - target) % 7
        if back == 0 and ("last" in m.group(0) or "past" in m.group(0)):
            back = 7
        day = today - timedelta(days=back)
        return DateRange(day, day, m.group(0))
    return None


# -- Ranking ----------------------------------------------------------------


def rank_passages(
    question: str,
    passages: list[Passage],
    date_range: DateRange | None = None,
) -> list[Passage]:
    """Passages that share words with the question, best first (BM25, title words count double)."""
    query_text = question.lower()
    if date_range:
        query_text = query_text.replace(date_range.phrase, " ")
    query = list(dict.fromkeys(tokenize(query_text)))
    if not query or not passages:
        return []

    for p in passages:
        p.tokens = tokenize(p.text) + tokenize(p.meeting.title) * 2
    n = len(passages)
    avg_len = sum(len(p.tokens) for p in passages) / n or 1.0
    df = Counter()
    for p in passages:
        df.update(set(p.tokens))

    k1, b = 1.2, 0.75
    ranked: list[Passage] = []
    for p in passages:
        counts = Counter(p.tokens)
        score = 0.0
        matched = 0
        for term in query:
            tf = counts.get(term, 0)
            if not tf:
                continue
            matched += 1
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(p.tokens) / avg_len))
        if matched:
            # Passages matching more of the question's words beat ones repeating a single word.
            p.score = score * (1 + 0.5 * (matched - 1) / max(1, len(query) - 1))
            ranked.append(p)
    ranked.sort(key=lambda p: (p.score, p.meeting.started), reverse=True)
    return ranked


def select_passages(
    ranked: list[Passage],
    token_budget: int,
    max_passages: int = 12,
    max_per_meeting: int = 4,
) -> list[Passage]:
    """The best passages that fit the budget, at most *max_per_meeting* from one meeting."""
    chosen: list[Passage] = []
    used = 0
    per_meeting: Counter = Counter()
    for p in ranked:
        if len(chosen) >= max_passages:
            break
        if per_meeting[p.meeting.folder] >= max_per_meeting:
            continue
        cost = estimate_tokens(p.text) + 30
        if used + cost > token_budget:
            continue
        chosen.append(p)
        used += cost
        per_meeting[p.meeting.folder] += 1
    return chosen


def overview_passages(meetings: list[MeetingDoc], token_budget: int, limit: int = 6) -> list[Passage]:
    """For questions without searchable words ("what happened yesterday?"): each meeting's
    summary if it has one, else the start of its transcript, newest meetings first."""
    chosen: list[Passage] = []
    used = 0
    per_meeting = max(400, token_budget // max(1, min(limit, len(meetings))))
    for doc in meetings[:limit]:
        spent = 0
        for p in build_passages(doc):
            cost = estimate_tokens(p.text) + 30
            if used + cost > token_budget or spent + cost > per_meeting:
                break
            chosen.append(p)
            used += cost
            spent += cost
            if p.kind == "summary":
                break  # a summary covers the whole meeting
    return chosen


# -- Prompt -----------------------------------------------------------------

ANSWER_SYSTEM = """You answer questions about {me}'s own work meetings, using only the meeting excerpts provided.
In the excerpts, "{me}" is the person asking you; "{them}" means the other people on the call.
Each excerpt starts with its number in brackets, then the meeting's date, time and title, and where in the \
recording it starts.

Rules:
- Answer directly in the first sentence, then add short supporting details or bullet points if useful.
- After each statement, cite the excerpts it comes from by number, like [2] or [1][3].
- Use only what the excerpts say. If they don't answer the question, say you couldn't find it in the \
meetings and stop; don't guess.
- When excerpts from different meetings disagree, prefer the most recent one and say what changed.
- Refer to meetings by date and title, not by folder names.
- Keep it brief. Today is {today}."""

ANSWER_PROMPT = """Question: {question}

Meeting excerpts:

{excerpts}

Answer the question from these excerpts, citing them by number."""


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "summary"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def format_excerpts(passages: list[Passage]) -> str:
    blocks = []
    for i, p in enumerate(passages, 1):
        where = "meeting summary" if p.kind == "summary" else f"at {_clock(p.start)}"
        blocks.append(f"[{i}] {p.meeting.started:%a %Y-%m-%d %H:%M} · {p.meeting.title} ({where})\n{p.text}")
    return "\n\n".join(blocks)


def order_for_prompt(passages: list[Passage]) -> list[Passage]:
    """Oldest meeting first and in recording order, so the model reads events as they happened."""
    return sorted(passages, key=lambda p: (p.meeting.started, -1 if p.start is None else p.start))


def cited_numbers(answer: str, count: int) -> list[int]:
    found: list[int] = []
    for group in re.findall(r"\[([\d,\s]+)\]", answer):
        for part in re.split(r"[,\s]+", group.strip()):
            if part.isdigit() and 1 <= int(part) <= count and int(part) not in found:
                found.append(int(part))
    return found


def source_entry(number: int, p: Passage) -> dict:
    return {
        "ref": number,
        "title": p.meeting.title,
        "date": f"{p.meeting.started:%Y-%m-%d}",
        "time": f"{p.meeting.started:%H:%M}",
        "at": "" if p.kind == "summary" else _clock(p.start),
        "kind": p.kind,
        "folder": str(p.meeting.folder),
        "notes": str(p.meeting.notes or p.meeting.transcript or p.meeting.folder),
    }


# -- Running ----------------------------------------------------------------


@dataclass
class Prepared:
    passages: list[Passage]  # in prompt order, numbered from 1
    meetings_searched: int
    date_range: DateRange | None


def prepare(
    config: Config,
    question: str,
    token_budget: int,
    since: str | None = None,
    until: str | None = None,
    today: date | None = None,
) -> Prepared:
    meetings = discover_meetings(config.output.resolved_dir)
    date_range = parse_date_range(question, today)
    start = date.fromisoformat(since) if since else (date_range.start if date_range else None)
    end = date.fromisoformat(until) if until else (date_range.end if date_range else None)
    if start:
        meetings = [m for m in meetings if m.started.date() >= start]
    if end:
        meetings = [m for m in meetings if m.started.date() <= end]

    passages = [p for doc in meetings for p in build_passages(doc)]
    chosen = select_passages(rank_passages(question, passages, date_range), token_budget)
    if not chosen and (start or end or not tokenize(question)):
        chosen = overview_passages(meetings, token_budget)
    return Prepared(order_for_prompt(chosen), len(meetings), date_range)


class _Output:
    """Plain text for people, or one JSON object per line for apps (--json)."""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def emit(self, event: str, **data) -> None:
        if self.as_json:
            sys.stdout.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        elif event == "status":
            sys.stderr.write(data["message"] + "\n")
            sys.stderr.flush()
        elif event == "token":
            sys.stdout.write(data["text"])
            sys.stdout.flush()
        elif event == "error":
            sys.stderr.write("Error: " + data["message"] + "\n")
        elif event == "sources" and data["sources"]:
            lines = ["", "", "Sources:"]
            for s in data["sources"]:
                at = f" at {s['at']}" if s["at"] else " (summary)"
                lines.append(f"  [{s['ref']}] {s['date']} {s['time']} · {s['title']}{at}")
            sys.stdout.write("\n".join(lines) + "\n")
        elif event == "answer" and data.get("final_only"):
            sys.stdout.write(data["text"] + "\n")


def _stream_answer(summarizer, system: str, prompt: str, on_token) -> str:
    """The model's answer, streamed token by token when the backend supports it."""
    from ownscribe.summarization.prompts import clean_response

    get_llm = getattr(summarizer, "_get_llm", None)
    if get_llm is None:
        text = summarizer.chat(system, prompt)
        on_token(text)
        return text
    llm = get_llm()
    parts: list[str] = []
    stream = llm.create_chat_completion(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        stream=True,
        max_tokens=700,
        temperature=0.2,
    )
    for chunk in stream:
        delta = chunk["choices"][0].get("delta", {}).get("content")
        if delta:
            parts.append(delta)
            on_token(delta)
    return clean_response("".join(parts))


def _model_cached(model: str) -> bool:
    if not model.startswith("hf:"):
        return True
    try:
        from huggingface_hub import try_to_load_from_cache

        spec = model[3:]
        repo, filename = spec.rsplit("/", 1)
        return isinstance(try_to_load_from_cache(repo, filename), str)
    except Exception:
        return True


def ask(
    config: Config,
    question: str,
    since: str | None = None,
    until: str | None = None,
    as_json: bool = False,
    summarizer=None,
) -> int:
    """Answer *question* from the meetings in the output folder. Returns an exit status."""
    out = _Output(as_json)
    question = question.strip()
    if not question:
        out.emit("error", message="Ask a question.")
        return 2

    context = config.summarization.context_size or 8192
    budget = max(1500, min(7000, context - 2500))
    out.emit("status", message="Searching your meetings…")
    prepared = prepare(config, question, budget, since=since, until=until)

    if not prepared.passages:
        if prepared.meetings_searched == 0:
            message = "There are no meetings to search yet" + (
                " in that time range." if prepared.date_range or since or until else "."
            )
        else:
            message = f"I couldn't find anything about that in your {prepared.meetings_searched} meetings."
        out.emit("sources", sources=[])
        out.emit("answer", text=message, final_only=True)
        return 0

    meetings_used = len({p.meeting.folder for p in prepared.passages})
    out.emit(
        "status",
        message=f"Reading {len(prepared.passages)} excerpts from {meetings_used} "
        f"meeting{'s' if meetings_used != 1 else ''}…",
    )

    owns_summarizer = summarizer is None
    if owns_summarizer:
        from ownscribe.summarization import create_summarizer

        if config.summarization.backend == "local" and not _model_cached(config.summarization.model):
            out.emit("status", message="Downloading the local model (about 5 GB, first time only)…")
        try:
            summarizer = create_summarizer(config)
        except ImportError as exc:
            out.emit("error", message=str(exc))
            return 1
        if not summarizer.is_available():
            out.emit("error", message="The local model isn't available. Check the summarization settings.")
            return 1

    system = ANSWER_SYSTEM.format(
        me=config.speakers.me_label or "the user",
        them=config.speakers.them_label or "Them",
        today=f"{date.today():%A %Y-%m-%d}",
    )
    prompt = ANSWER_PROMPT.format(question=question, excerpts=format_excerpts(prepared.passages))

    first = True

    def on_token(text: str) -> None:
        nonlocal first
        if first:
            out.emit("status", message="Writing the answer…")
            first = False
        out.emit("token", text=text)

    try:
        answer = _stream_answer(summarizer, system, prompt, on_token).strip()
    except Exception as exc:  # model load or generation failure
        out.emit("error", message=f"The local model failed: {exc}")
        return 1
    finally:
        if owns_summarizer:
            summarizer.close()

    numbers = cited_numbers(answer, len(prepared.passages))
    sources = [source_entry(n, prepared.passages[n - 1]) for n in numbers]
    out.emit("answer", text=answer or "The model returned no answer.")
    out.emit("sources", sources=sources)
    return 0
