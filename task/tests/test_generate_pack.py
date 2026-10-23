"""The soak pack generator: deterministic, large enough, internally consistent, and scored at 1.0
by the verifier on its own reference results."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from task import generate_pack
from task.pack import ENV_VAR, Pack

YEARS, START, SEED = 20, 1850, 7


@pytest.fixture(scope="module")
def pack_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("pack") / "soak"
    generate_pack.generate(YEARS, START, SEED, out)
    return out


def _bytes(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        data = p.read_bytes()
        if p.name == "tasks.json":
            # materials_dir is the pack's own path, which differs between two output directories.
            doc = json.loads(data)
            doc.pop("materials_dir", None)
            data = json.dumps(doc, sort_keys=True).encode()
        out[str(p.relative_to(root))] = data
    return out


def test_same_seed_gives_the_same_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_pack.generate(5, START, SEED, a)
    generate_pack.generate(5, START, SEED, b)
    assert _bytes(a) == _bytes(b)
    c = tmp_path / "c"
    generate_pack.generate(5, START, SEED + 1, c)
    assert _bytes(a) != _bytes(c)


def test_twenty_years_is_large_enough_and_well_formed(pack_dir):
    pack = Pack(pack_dir)
    materials = sum(len(p.read_text()) for p in pack.materials_dir.glob("*.txt"))
    tasks = pack.tasks()
    assert materials >= 150_000
    assert len(tasks) >= 55
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == len(ids) and ids[0] == "T-001"
    assert set(ids) == set(pack.answers())
    assert all(t["format"] and t["result_file"] for t in tasks)
    # Tasks that read a file name it the way the agent sees it; the assembled ones point at the board.
    for t in tasks:
        if t["family"] in ("census", "ledger", "insurance", "fire"):
            assert "in the materials directory" in t["description"], t["id"]
        else:
            assert "board" in t["description"].lower(), t["id"]
    families = {t["family"] for t in tasks}
    assert families == {"census", "ledger", "fire", "customers", "insurance", "chronicle"}
    # No em dashes anywhere in what the agents read.
    for p in pack.materials_dir.glob("*.txt"):
        assert "—" not in p.read_text()
    assert "—" not in json.dumps(tasks)


def test_priority_decreases_with_year_within_each_family(pack_dir):
    tasks = Pack(pack_dir).tasks()
    by_family: dict[str, list[tuple[int, float]]] = {}
    for t in tasks:
        by_family.setdefault(t["family"], []).append((t["year"], t["priority_base"]))
    for family, pairs in by_family.items():
        pairs.sort()
        priorities = [p for _, p in pairs]
        assert priorities == sorted(priorities, reverse=True), family


def test_dependent_tasks_name_their_dependencies(pack_dir):
    tasks = {t["id"]: t for t in Pack(pack_dir).tasks()}
    for t in tasks.values():
        if t["family"] == "customers":
            named = re.findall(r"T-\d{3}", t["description"])
            assert len(named) == 2 and {tasks[n]["family"] for n in named} == {"census", "ledger"}
            assert "Check the board first" in t["description"]


def test_every_answer_is_derivable_from_the_files_it_names(pack_dir):
    pack = Pack(pack_dir)
    derivations = json.loads((pack_dir / "derivations.json").read_text())
    assert set(derivations) == set(pack.answers())
    for task_id, d in derivations.items():
        texts = []
        for name in d["files"]:
            path = pack.materials_dir / name
            assert path.exists(), f"{task_id} names a missing file {name}"
            texts.append(path.read_text().lower())
        joined = "\n".join(texts)
        for token in d["tokens"]:
            assert str(token).lower() in joined, f"{task_id}: {token!r} not found in {d['files']}"


def test_verifier_scores_every_reference_result_at_one(pack_dir, monkeypatch):
    from task.verifier import verifier

    monkeypatch.setenv(ENV_VAR, str(pack_dir))
    pack = Pack(pack_dir)
    for t in pack.tasks():
        score, notes = verifier.verify(t["id"], str(pack_dir / "reference" / t["result_file"]))
        assert score == 1.0, f"{t['id']} ({t['family']}): {notes}"


def test_wrong_results_do_not_score_full_marks(pack_dir, monkeypatch, tmp_path):
    from task.verifier import verifier

    monkeypatch.setenv(ENV_VAR, str(pack_dir))
    pack = Pack(pack_dir)
    ledger = next(t for t in pack.tasks() if t["family"] == "ledger")
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("total_shillings: 1\npayees: 1\n")
    assert verifier.verify(ledger["id"], str(wrong))[0] < 1.0


def test_cli_prints_counts(tmp_path, capsys):
    assert generate_pack.main(["--years", "3", "--out", str(tmp_path / "p")]) == 0
    out = capsys.readouterr().out
    assert "material files" in out and "tasks" in out
