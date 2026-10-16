"""Review tests for the verifier's normalisation and scoring edges, and for the pack's internal consistency."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from task.verifier import verifier

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import reset_db  # noqa: E402

from harness.board_mcp.seed import CONFIG_SEEDS  # noqa: E402

TASKS = json.loads((ROOT / "task" / "tasks.json").read_text())["tasks"]
ANSWERS = json.loads((ROOT / "task" / "verifier" / "answers.json").read_text())
T007 = ANSWERS["T-007"]


def _score(tmp_path: Path, task_id: str, text: str) -> float:
    path = tmp_path / f"{task_id}.txt"
    path.write_text(text)
    return verifier.verify(task_id, str(path))[0]


def test_trailing_punctuation_case_and_spacing_are_forgiven(tmp_path):
    assert _score(tmp_path, "T-002", "YEAR : 1853.\nMiller:   josiah   reeve.  \n") == 1.0


def test_comma_name_order_and_thousands_separator_are_forgiven(tmp_path):
    assert _score(tmp_path, "T-005", "reopened: 1,855\nmiller: Reeve, Samuel\nyears_between: two (2)\n") == 1.0


def test_hyphenated_keys_match(tmp_path):
    assert _score(tmp_path, "T-003", "total-shillings: 125\npayees: 9\n") == 1.0


def test_later_duplicate_field_wins(tmp_path):
    assert _score(tmp_path, "T-002", "year: 1852\nmiller: Josiah Reeve\nyear: 1853\n") == 1.0


def test_honorifics_and_and_separated_sets_are_not_forgiven(tmp_path):
    # The format lines are explicit, so the verifier is deliberately strict here; this pins it.
    assert _score(tmp_path, "T-002", "year: 1853\nmiller: Mr Josiah Reeve\n") == 0.5
    assert _score(tmp_path, "T-006", "inventory_pounds: 96\npaid_pounds: 80\nshortfall_pounds: sixteen\n") < 1.0


def test_year_embedded_in_a_date_is_still_found(tmp_path):
    assert _score(tmp_path, "T-002", "year: 4 October 1853\nmiller: Josiah Reeve\n") == 1.0


def _chronicle(words: int) -> str:
    core = "josiah reeve 1853 fire 1854 died 80 pounds samuel reeve 1855 reopened michaelmas"
    padding = ["and"] * (words - len(core.split()))
    return core + " " + " ".join(padding)


def test_keyword_word_window_boundaries(tmp_path):
    lo, hi = T007["min_words"], T007["max_words"]
    assert _score(tmp_path, "T-007", _chronicle(lo)) == 1.0
    assert _score(tmp_path, "T-007", _chronicle(hi)) == 1.0
    assert _score(tmp_path, "T-007", _chronicle(lo - 1)) == 0.5
    assert _score(tmp_path, "T-007", _chronicle(hi + 1)) == 0.5


def test_keyword_partial_coverage_is_proportional(tmp_path):
    text = "josiah reeve 1853 fire " + " ".join(["and"] * 70)
    groups = len(T007["groups"])
    assert _score(tmp_path, "T-007", text) == pytest.approx(3 / groups)


def test_result_files_and_ids_are_unique_and_every_task_has_an_answer():
    ids = [t["id"] for t in TASKS]
    files = [t["result_file"] for t in TASKS]
    assert len(set(ids)) == len(ids) and len(set(files)) == len(files)
    assert set(ids) == set(ANSWERS)
    assert all(t.get("format") for t in TASKS)


def test_reset_script_has_no_private_copy_of_the_seeds():
    # One source of truth for the knobs: reset_db must re-seed through harness/board_mcp/seed.py
    # rather than carry its own list that can drift from the migration.
    source = Path(reset_db.__file__).read_text()
    assert "seed_config" in source and "CONFIG_SEEDS" not in source
    # And every Python seed has a migration row somewhere, so a knob added in one place and not the
    # other is caught here rather than by a fresh database missing it.
    migrations = Path(reset_db.__file__).resolve().parents[1] / "harness" / "board_mcp" / "migrations"
    seeded_sql = "\n".join(f.read_text() for f in migrations.glob("*.sql"))
    assert all(f"'{knob}'" in seeded_sql for knob in CONFIG_SEEDS)


def test_completion_finding_does_not_count_as_an_answer(monkeypatch, tmp_path):
    board = [
        {"id": 1, "kind": "task", "thread_id": None, "author_id": "operator"},
        {"id": 2, "kind": "question", "thread_id": 1, "author_id": "sol-002"},
        {"id": 3, "kind": "finding", "thread_id": 1, "author_id": "sol-001"},  # "[T-001 completed] ..."
    ]
    monkeypatch.setattr(verifier, "_load_messages", lambda db_url=None: board)
    assert _score(tmp_path, "T-008", "answered: none\n") == 0.0
