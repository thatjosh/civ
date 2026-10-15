"""Tests for the Hollowmere task pack.

Three guarantees: the verifier accepts a correct result and rejects a wrong one
for every task, tasks.json is well formed, and every answer in answers.json can
be re-derived from the materials with plain Python, so the hidden answers and
the corpus cannot drift apart when someone edits a letter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from task.verifier import verifier

ROOT = Path(__file__).resolve().parents[2]
MATERIALS = ROOT / "task" / "materials"
TASKS = json.loads((ROOT / "task" / "tasks.json").read_text())["tasks"]
ANSWERS = json.loads((ROOT / "task" / "verifier" / "answers.json").read_text())

CHRONICLE = (
    "In 1851 Josiah Reeve was miller at Hollowmere, keeping the ledgers himself. "
    "On the night of 4 October 1853 the mill burned; no one was hurt, but the wheel "
    "and stones were lost. Josiah Reeve died in February 1854 and was buried on the "
    "seventeenth. His son Samuel Reeve claimed on the policy, and the Wessex Mutual "
    "paid 80 pounds against an inventory of 96, the parish subscribing the rest. "
    "Masons from Netherby rebuilt the mill through 1854 and it reopened at Michaelmas "
    "1855 with Samuel Reeve as miller."
)

CORRECT = {
    "T-001": "households: 12\npersons: 37\nsurnames: Ashby, Bramwell, Carrow, Dunmore, Fenwick, Grice, Hale, Lumley, Pargeter, Reeve, Tolley, Wick\n",
    "T-002": "year: 1853\nmiller: Reeve, Josiah\n",
    "T-003": "total_shillings: 125\npayees: 9\n",
    "T-004": "in_parish: Tolley, Pargeter, Hale, Grice, Fenwick, Carrow, Ashby\noutsiders: Vane, Marlow\n",
    "T-005": "reopened: 1855\nmiller: samuel reeve\nyears_between: 2\n",
    "T-006": "inventory_pounds: 96\npaid_pounds: 80\nshortfall_pounds: 16\n",
    "T-007": CHRONICLE + "\n",
    "T-008": "answered: 5, 9\n",
}

WRONG = {
    "T-001": "households: 11\npersons: 37\nsurnames: Ashby, Reeve\n",
    "T-002": "year: 1854\nmiller: Samuel Reeve\n",
    "T-003": "total_shillings: 130\npayees: 7\n",
    "T-004": "in_parish: Ashby, Vane\noutsiders: Marlow\n",
    "T-005": "reopened: 1854\nmiller: Josiah Reeve\nyears_between: 1\n",
    "T-006": "inventory_pounds: 80\npaid_pounds: 96\nshortfall_pounds: 0\n",
    "T-007": "The mill burned and later reopened.\n",
    "T-008": "answered: 4, 7\n",
}

# A fake board for the questions task: ids 5 and 9 are questions that later
# got findings posted as replies to them (parent_id), 7 is a question alone in
# its thread and so still open, 4 is a finding that answers nothing.
FAKE_BOARD = [
    {"id": 1, "kind": "task", "thread_id": None, "author_id": "operator", "parent_id": None},
    {"id": 2, "kind": "task", "thread_id": None, "author_id": "operator", "parent_id": None},
    {"id": 4, "kind": "finding", "thread_id": 1, "author_id": "sol-001", "parent_id": None},
    {"id": 5, "kind": "question", "thread_id": 1, "author_id": "sol-002", "parent_id": None},
    {"id": 6, "kind": "finding", "thread_id": 1, "author_id": "sol-003", "parent_id": 5},
    {"id": 3, "kind": "task", "thread_id": None, "author_id": "operator", "parent_id": None},
    {"id": 7, "kind": "question", "thread_id": 3, "author_id": "sol-002", "parent_id": None},
    {"id": 9, "kind": "question", "thread_id": 2, "author_id": "sol-001", "parent_id": None},
    {"id": 10, "kind": "finding", "thread_id": 2, "author_id": "sol-003", "parent_id": 9},
]


@pytest.fixture(autouse=True)
def fake_board(monkeypatch):
    # The questions task is the only one that reads the database; every other
    # path must never need one, which this fake also proves.
    monkeypatch.setattr(verifier, "_load_messages", lambda db_url=None: FAKE_BOARD)


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


@pytest.mark.parametrize("task_id", sorted(CORRECT))
def test_correct_result_scores_full(tmp_path, task_id):
    score, notes = verifier.verify(task_id, _write(tmp_path, "r.txt", CORRECT[task_id]))
    assert score == 1.0, notes


@pytest.mark.parametrize("task_id", sorted(WRONG))
def test_wrong_result_scores_below_full(tmp_path, task_id):
    score, notes = verifier.verify(task_id, _write(tmp_path, "r.txt", WRONG[task_id]))
    assert score < 1.0, notes
    assert notes  # the note names what was wrong, which is what reaches the task row


def test_missing_file_and_unknown_task(tmp_path):
    assert verifier.verify("T-001", str(tmp_path / "nope.txt"))[0] == 0.0
    assert verifier.verify("T-999", _write(tmp_path, "r.txt", "x"))[0] == 0.0


def test_partial_credit_counts_fields(tmp_path):
    # Two of three fields right is two thirds, so a solver sees progress, not a cliff.
    score, _ = verifier.verify("T-006", _write(tmp_path, "r.txt", "inventory_pounds: 96\npaid_pounds: 80\nshortfall_pounds: 15\n"))
    assert score == pytest.approx(2 / 3)


def test_chronicle_length_window_caps_score(tmp_path):
    long_text = (CHRONICLE + " ") * 3
    score, notes = verifier.verify("T-007", _write(tmp_path, "r.txt", long_text))
    assert score <= 0.5 and "outside" in notes


def test_questions_none_is_wrong_while_a_question_is_open(tmp_path):
    score, notes = verifier.verify("T-008", _write(tmp_path, "r.txt", "answered: none\n"))
    assert score == 0.0 and "7" in notes


def test_questions_none_is_right_when_all_answered(tmp_path, monkeypatch):
    closed = [m for m in FAKE_BOARD if m["id"] != 7]
    monkeypatch.setattr(verifier, "_load_messages", lambda db_url=None: closed)
    assert verifier.verify("T-008", _write(tmp_path, "r.txt", "answered: none\n"))[0] == 1.0


def test_questions_unreachable_board_gives_format_credit(tmp_path, monkeypatch):
    def boom(db_url=None):
        raise ConnectionError("no board")

    monkeypatch.setattr(verifier, "_load_messages", boom)
    score, notes = verifier.verify("T-008", _write(tmp_path, "r.txt", "answered: 5\n"))
    assert score == 0.5 and "unreachable" in notes


def test_tasks_json_is_well_formed():
    ids = [t["id"] for t in TASKS]
    assert len(ids) == len(set(ids)), "task ids must be unique"
    assert set(ids) == set(ANSWERS), "every task has an answer key and vice versa"
    for task in TASKS:
        for key in ("title", "description", "priority_base", "result_file", "format"):
            assert task.get(key) not in (None, ""), f"{task['id']} missing {key}"
        assert re.fullmatch(r"T-\d{3}", task["id"])
        assert task["result_file"].endswith(".txt")
    # Collaboration is the point of the demo, so at least two tasks must send
    # the solver to the board before the materials.
    dependent = [t for t in TASKS if "check the board" in t["description"].lower()]
    assert len(dependent) >= 2


def test_no_em_dashes_in_pack():
    for path in list(MATERIALS.glob("*.txt")) + [ROOT / "task" / "tasks.json"]:
        assert chr(0x2014) not in path.read_text(), path


# Derivation: recompute every answer from the corpus with plain parsing.

def _census():
    text = (MATERIALS / "census_1851.txt").read_text()
    entries = re.findall(r"^Household \d+\. .*?\. ([A-Z]+), .*?Persons: (\d+)\.", text, flags=re.M)
    return [name.title() for name, _ in entries], [int(p) for _, p in entries]


def _ledger():
    rows = re.findall(r"^(\d{4}-\d{2}-\d{2})\s+(\w+)\s+.*?\s+(\d+)\s*$", (MATERIALS / "ledger_1852.txt").read_text(), flags=re.M)
    return [(payee, int(shillings)) for _, payee, shillings in rows]


def _log():
    return re.findall(r"^(\d{4})-(\d{2})-(\d{2})\s+(.*)$", (MATERIALS / "parish_log_1850_1855.txt").read_text(), flags=re.M)


def _inventory():
    return [int(m) for m in re.findall(r"^[A-Z][^\n]*?\s(\d+)\s*$", (MATERIALS / "inventory_1854.txt").read_text(), flags=re.M)]


def test_answers_derive_from_materials():
    surnames, persons = _census()
    f = ANSWERS["T-001"]["fields"]
    assert f["households"]["value"] == len(surnames) == 12
    assert f["persons"]["value"] == sum(persons)
    assert f["surnames"]["value"] == sorted(surnames)

    ledger = _ledger()
    f = ANSWERS["T-003"]["fields"]
    assert f["total_shillings"]["value"] == sum(s for _, s in ledger)
    payees = {p for p, _ in ledger}
    assert f["payees"]["value"] == len(payees)

    f = ANSWERS["T-004"]["fields"]
    assert set(f["in_parish"]["value"]) == payees & set(surnames)
    assert set(f["outsiders"]["value"]) == payees - set(surnames)

    log = _log()
    fire = next(e for e in log if "mill burned" in e[3])
    reopened = next(e for e in log if "mill reopened" in e[3])
    buried = next(e for e in log if "Burial: Josiah Reeve" in e[3])
    paid = next(e for e in log if "paid" in e[3] and "mill claim" in e[3])
    assert ANSWERS["T-002"]["fields"]["year"]["value"] == int(fire[0])
    assert ANSWERS["T-005"]["fields"]["reopened"]["value"] == int(reopened[0])
    assert ANSWERS["T-005"]["fields"]["years_between"]["value"] == int(reopened[0]) - int(fire[0])
    assert "Samuel Reeve" in reopened[3] and ANSWERS["T-005"]["fields"]["miller"]["value"] == "Samuel Reeve"
    assert "Josiah Reeve, miller" in fire[3] and ANSWERS["T-002"]["fields"]["miller"]["value"] == "Josiah Reeve"
    assert int(buried[0]) == 1854

    inventory = _inventory()
    f = ANSWERS["T-006"]["fields"]
    assert f["inventory_pounds"]["value"] == sum(inventory) == 96
    assert f["paid_pounds"]["value"] == int(re.search(r"paid (\d+) pounds", paid[3]).group(1))
    assert f["shortfall_pounds"]["value"] == sum(inventory) - f["paid_pounds"]["value"]

    # The chronicle keywords must all be grounded somewhere in the archive, or
    # the task would be asking for invention.
    corpus = " ".join(p.read_text().lower() for p in MATERIALS.glob("*.txt"))
    for group in ANSWERS["T-007"]["groups"]:
        assert any(k in corpus for k in group), group
