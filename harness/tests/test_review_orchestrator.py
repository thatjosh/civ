"""Review tests for the orchestrator that need neither a database nor the SDK CLI."""

from __future__ import annotations

import json
from pathlib import Path

import harness.orchestrator.main as main_module
from harness.orchestrator.monitor import Monitor
from harness.orchestrator.spawner import SpawnResult
from harness.orchestrator.supervisors import RoundResult
from harness.tests.orchestrator_fakes import FakeOperator, assistant

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_threshold_fires_once_per_lifetime_even_after_a_drop_and_recross():
    fired: list[float] = []
    monitor = Monitor(FakeOperator(), "sol-001", context_window=1000)
    monitor.on_threshold(80, lambda _agent, pct: fired.append(pct))
    for context in (850, 100, 900):
        monitor.observe(assistant(context))
    # A handoff request must never be sent twice, so the crossing is remembered for life.
    assert len(fired) == 1


def test_main_exit_code_follows_solver_and_round_errors(monkeypatch):
    # amain returns (solver results, supervisor rounds); either side failing is a non-zero exit.
    async def failing(cfg):
        return [SpawnResult("sol-001", error="boom")], []

    async def healthy(cfg):
        return [SpawnResult("sol-001", turns=3, cost_usd=0.01)], [RoundResult(number=1, final=True)]

    async def bad_round(cfg):
        return [SpawnResult("sol-001", turns=3, cost_usd=0.01)], [RoundResult(number=1, final=True, error="no brief")]

    monkeypatch.setattr(main_module, "amain", failing)
    assert main_module.main(["--solvers", "1"]) == 1
    monkeypatch.setattr(main_module, "amain", healthy)
    assert main_module.main(["--solvers", "1"]) == 0
    monkeypatch.setattr(main_module, "amain", bad_round)
    assert main_module.main(["--solvers", "1", "--supervisors", "1"]) == 1


def test_prompts_never_leak_the_answer_key():
    answers = json.loads((REPO_ROOT / "task" / "verifier" / "answers.json").read_text())
    prompts = "\n".join(p.read_text().lower() for p in (REPO_ROOT / "harness" / "prompts").rglob("*.md"))
    assert "answers.json" not in prompts
    for spec in answers.values():
        for field in spec.get("fields", {}).values():
            values = field["value"] if isinstance(field["value"], list) else [field["value"]]
            for value in values:
                text = str(value).lower()
                # Small integers ("2") appear in any prompt; names and years are the leak to catch.
                if text.isdigit() and int(text) < 1000:
                    continue
                assert text not in prompts, f"prompt leaks answer {text!r}"
