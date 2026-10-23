"""The Pack abstraction: both layouts, the environment switch, and the verifier reading through it."""

from __future__ import annotations

import json
from pathlib import Path

from task.pack import ENV_VAR, Pack

ROOT = Path(__file__).resolve().parents[2]


def test_default_pack_is_the_repo_layout():
    pack = Pack.default()
    assert pack.root == ROOT / "task"
    assert pack.materials_dir == ROOT / "task" / "materials"
    assert pack.tasks_path == ROOT / "task" / "tasks.json"
    assert pack.answers_path == ROOT / "task" / "verifier" / "answers.json"
    assert not pack.generated and pack.name == "hollowmere"
    assert {t["id"] for t in pack.tasks()} == set(pack.answers())


def test_generated_pack_keeps_answers_beside_tasks(tmp_path):
    (tmp_path / "materials").mkdir()
    (tmp_path / "tasks.json").write_text(json.dumps({"pack": "p", "materials_dir": "x", "tasks": [{"id": "T-001"}]}))
    (tmp_path / "answers.json").write_text(json.dumps({"T-001": {"type": "fields", "fields": {}}}))
    pack = Pack(tmp_path)
    assert pack.generated and pack.answers_path == tmp_path / "answers.json"
    assert pack.tasks() == [{"id": "T-001"}] and pack.name == "p"


def test_from_env_reads_civ_pack_and_falls_back(tmp_path, monkeypatch):
    assert Pack.from_env({}) == Pack.default()
    monkeypatch.setenv(ENV_VAR, str(tmp_path))
    assert Pack.from_env().root == tmp_path.resolve()
    monkeypatch.delenv(ENV_VAR)
    assert Pack.from_env() == Pack.default()


def test_verifier_scores_against_the_pack_in_the_environment(tmp_path, monkeypatch):
    # The server scores whatever CIV_PACK names, so a generated pack and the repo pack share one verifier.
    from task.verifier import verifier

    (tmp_path / "answers.json").write_text(json.dumps({"T-900": {"type": "fields", "fields": {"n": {"kind": "int", "value": 4}}}}))
    (tmp_path / "tasks.json").write_text(json.dumps({"pack": "tiny", "materials_dir": "m", "tasks": []}))
    result = tmp_path / "r.txt"
    result.write_text("n: 4\n")
    monkeypatch.setenv(ENV_VAR, str(tmp_path))
    assert verifier.verify("T-900", str(result)) == (1.0, "all fields correct")
    monkeypatch.delenv(ENV_VAR)
    assert verifier.verify("T-900", str(result))[1] == "no answer key for T-900"
