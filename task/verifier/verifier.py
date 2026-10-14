"""The Hollowmere verifier: the one piece of the task pack agents never read.

The board server calls ``verify(task_id, result_path)`` when a solver completes
a task and stores the score on the task row. Agents only ever see that number,
never this module or ``answers.json``, because the first thing a capable swarm
does when it meets a grader is take it apart (design doc, section 1).

Three kinds of answer are scored:

* ``fields``: the result file is ``key: value`` lines and every expected field
  is compared exactly after normalisation (integers, names, or sets of names).
* ``keywords``: prose scored by how many keyword groups it covers, with a word
  count window so a solver cannot paste the whole archive.
* ``questions``: the "answer the open questions" task, which can only be
  judged against the board itself, so this verifier reads the messages table
  directly. It depends on nothing but the schema in docs/CONTRACT.md.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ANSWERS_PATH = Path(__file__).with_name("answers.json")
DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"


def _load_answers() -> dict:
    with ANSWERS_PATH.open() as fh:
        return json.load(fh)


# Normalisation helpers. Solvers write "Josiah Reeve", "josiah  reeve" or
# "Reeve, Josiah" in equal measure, so comparisons happen on a canonical form
# rather than on raw text.

def _norm_text(value: str) -> str:
    value = value.strip().strip(".").lower()
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _norm_name(value: str) -> str:
    # "Reeve, Josiah" and "Josiah Reeve" are the same person, so a comma form
    # is flipped before comparison.
    if "," in value:
        last, first = value.split(",", 1)
        value = f"{first} {last}"
    return _norm_text(value)


def _parse_int(value: str) -> int | None:
    match = re.search(r"-?\d[\d,]*", value)
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def _parse_set(value: str) -> set[str]:
    parts = re.split(r"[,;]", value)
    return {_norm_text(p) for p in parts if _norm_text(p)}


def _parse_fields(text: str) -> dict[str, str]:
    """Read ``key: value`` lines; later duplicates win, because a solver that
    corrects itself at the bottom of the file meant the correction."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = _norm_text(key).replace(" ", "_")
        if key:
            fields[key] = value.strip()
    return fields


@dataclass
class _FieldCheck:
    name: str
    ok: bool
    expected: str
    got: str


def _check_field(name: str, spec: dict, raw: str | None) -> _FieldCheck:
    kind = spec["kind"]
    expected = spec["value"]
    if raw is None:
        return _FieldCheck(name, False, str(expected), "<missing>")
    if kind == "int":
        got = _parse_int(raw)
        return _FieldCheck(name, got == int(expected), str(expected), str(got))
    if kind == "text":
        return _FieldCheck(name, _norm_name(raw) == _norm_name(expected), expected, raw.strip())
    if kind == "set":
        want = {_norm_text(v) for v in expected}
        got = _parse_set(raw)
        return _FieldCheck(name, got == want, ", ".join(sorted(want)), ", ".join(sorted(got)))
    raise ValueError(f"unknown field kind {kind!r} for {name}")


def _score_fields(spec: dict, text: str) -> tuple[float, str]:
    fields = _parse_fields(text)
    checks = [_check_field(name, fspec, fields.get(name)) for name, fspec in spec["fields"].items()]
    correct = sum(1 for c in checks if c.ok)
    score = correct / len(checks)
    wrong = [f"{c.name}: expected {c.expected}, got {c.got}" for c in checks if not c.ok]
    notes = "all fields correct" if not wrong else "; ".join(wrong)
    return score, notes


def _score_keywords(spec: dict, text: str) -> tuple[float, str]:
    body = _norm_text(text)
    words = len(body.split())
    hits = [any(_norm_text(k) in body for k in group) for group in spec["groups"]]
    score = sum(hits) / len(hits)
    missing = [group[0] for group, hit in zip(spec["groups"], hits) if not hit]
    notes = f"{sum(hits)}/{len(hits)} keyword groups"
    if missing:
        notes += ", missing: " + ", ".join(missing)
    lo, hi = spec["min_words"], spec["max_words"]
    if not lo <= words <= hi:
        # A chronicle that ignores the length brief is half credit at most,
        # because length is the only thing stopping a solver pasting the archive.
        score = min(score, 0.5)
        notes += f"; {words} words, outside {lo} to {hi}"
    return score, notes


def _load_messages(db_url: str | None = None) -> list[dict]:
    """Every message as ``{id, kind, thread_id, author_id}``, straight from the
    schema. Kept as a module function so tests can swap in a fake board."""
    import psycopg  # imported here so the fields and keywords paths never need a database

    url = db_url or os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL)
    with psycopg.connect(url) as conn:
        rows = conn.execute("select id, kind, thread_id, author_id from messages order by id").fetchall()
    return [{"id": r[0], "kind": r[1], "thread_id": r[2], "author_id": r[3]} for r in rows]


def _unanswered_questions(messages: list[dict]) -> set[int]:
    """A question is answered once a finding appears later in its thread."""
    unanswered: set[int] = set()
    for msg in messages:
        if msg["kind"] != "question":
            continue
        thread = msg["thread_id"] or msg["id"]
        answered = any(
            other["kind"] == "finding"
            and (other["thread_id"] or other["id"]) == thread
            and other["id"] > msg["id"]
            for other in messages
        )
        if not answered:
            unanswered.add(msg["id"])
    return unanswered


def _score_questions(text: str, messages: list[dict]) -> tuple[float, str]:
    fields = _parse_fields(text)
    raw = fields.get("answered")
    if raw is None:
        return 0.0, "no 'answered:' line"
    questions = {m["id"] for m in messages if m["kind"] == "question"}
    unanswered = _unanswered_questions(messages)
    if _norm_text(raw) in ("none", "n a", "nil", ""):
        # Claiming there was nothing to answer is right only if every question
        # on the board already had a finding under it.
        if not unanswered:
            return 1.0, f"no open questions remained ({len(questions)} on the board)"
        return 0.0, f"{len(unanswered)} open question(s) were unanswered: {sorted(unanswered)}"
    claimed = {int(x) for x in re.findall(r"\d+", raw)}
    if not claimed:
        return 0.0, "answered line names no message ids"
    good = {q for q in claimed if q in questions and q not in unanswered}
    score = len(good) / len(claimed)
    notes = f"{len(good)}/{len(claimed)} claimed questions are real and now answered"
    if unanswered:
        notes += f"; still open: {sorted(unanswered)}"
    return score, notes


def verify(task_id: str, result_path: str) -> tuple[float, str]:
    """Score a result file for ``task_id`` between 0.0 and 1.0 with a short note."""
    answers = _load_answers()
    spec = answers.get(task_id)
    if spec is None:
        return 0.0, f"no answer key for {task_id}"
    path = Path(result_path)
    if not path.is_file():
        return 0.0, f"result file not found: {result_path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    if spec["type"] == "fields":
        return _score_fields(spec, text)
    if spec["type"] == "keywords":
        return _score_keywords(spec, text)
    if spec["type"] == "questions":
        try:
            messages = _load_messages()
        except Exception as exc:  # the board is unreachable, so grade the format only
            fields = _parse_fields(text)
            if "answered" in fields:
                return 0.5, f"board unreachable ({type(exc).__name__}); format accepted, truth unknown"
            return 0.0, "no 'answered:' line and board unreachable"
        return _score_questions(text, messages)
    raise ValueError(f"unknown answer type {spec['type']!r}")


def progress(db_url: str | None = None) -> str:
    """One paragraph on how far the pack has moved, from the tasks table."""
    import psycopg

    url = db_url or os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL)
    with psycopg.connect(url) as conn:
        rows = conn.execute("select status, count(*), avg(score) from tasks group by status").fetchall()
    counts = {status: n for status, n, _ in rows}
    total = sum(counts.values())
    done = counts.get("done", 0)
    mean = next((avg for status, _, avg in rows if status == "done" and avg is not None), None)
    mean_text = f"mean verifier score {mean:.2f}" if mean is not None else "no scored completions yet"
    return (
        f"Hollowmere pack: {done} of {total} tasks done, {counts.get('leased', 0)} leased, "
        f"{counts.get('open', 0)} open, {counts.get('blocked', 0)} blocked; {mean_text}."
    )
