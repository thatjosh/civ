"""Review tests for the civ tools, the operator API, and the seam between monitor and status."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from harness.orchestrator.monitor import Monitor
from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import Spawner
from harness.tests.orchestrator_fakes import assistant, init_message, result, scripted_query

REPO_ROOT = Path(__file__).resolve().parents[2]
HEADINGS = ("State of work", "Open threads", "What to try next", "What not to retry", "Files worth reading")
VALID_HANDOFF = "\n".join(f"## {h}\nsomething" for h in HEADINGS) + "\n"


def test_handoff_headings_are_case_insensitive_but_must_be_bare(civ, solver, op):
    op.set_agent_status(solver.id, "handing_off")
    assert civ.handoff(solver, VALID_HANDOFF.lower())["ok"]
    suffixed = VALID_HANDOFF.replace("## State of work", "## State of work: done")
    refused = civ.handoff(solver, suffixed)
    assert not refused["ok"] and "State of work" in refused["error"]


def test_handoff_size_ceiling_follows_the_knob(civ, solver, op, db):
    db.execute("update config set value = 1000 where knob = 'max_handoff_tokens'")
    op.set_agent_status(solver.id, "handing_off")
    refused = civ.handoff(solver, VALID_HANDOFF + "z" * 4200)
    assert not refused["ok"] and "ceiling is 1000" in refused["error"]


def test_tune_cooldown_applies_across_supervisors(civ, make_agent):
    first, second = make_agent("supervisor")[0], make_agent("supervisor")[0]
    assert civ.tune(first, "top_k", 15, "lease_wait", "wider reads")["ok"]
    assert "cooldown" in civ.tune(second, "top_k", 20, "lease_wait", "wider still")["error"]


def test_tune_reverts_to_the_seed_after_its_ttl(civ, supervisor, knobs):
    assert civ.tune(supervisor, "top_k", 15, "lease_wait", "briefly wider", ttl_s=1)["ok"]
    time.sleep(1.2)
    assert knobs.get("top_k") == 10


def test_tune_rejects_a_two_step_move_and_accepts_one_step_down(civ, supervisor):
    assert "one step" in civ.tune(supervisor, "vote_rate_per_h", 30, "duplicate_rate", "x")["error"]
    assert civ.tune(supervisor, "vote_rate_per_h", 15, "duplicate_rate", "fewer reflex votes")["ok"]


def test_status_reflects_monitor_usage_writes(civ, solver, op):
    Monitor(op, solver.id, context_window=1_000_000).observe(assistant(5000, output_tokens=50))
    you = civ.status(solver)["you"]
    assert you["context_pct"] == pytest.approx(0.5)


def test_status_tokens_per_min_sees_monitor_turn_events(civ, solver, op):
    # The monitor's agent.turn payload and civ_status's sum share the keys tokens_in/tokens_out;
    # this is the seam between two workstreams, so it is pinned here.
    Monitor(op, solver.id).observe(assistant(5000, output_tokens=50))
    assert civ.status(solver)["tokens_per_min"] > 0


def test_register_agent_rejects_a_duplicate_id(op, make_agent):
    identity, _ = make_agent("solver")
    with pytest.raises(Exception):
        op.register_agent(identity.id, "solver", lineage_id="L-dup", cwd="/tmp/dup")


async def test_spawn_logs_exactly_one_spawned_event(op, db, tmp_path):
    spawner = Spawner(
        op, "http://127.0.0.1:1", tmp_path, PromptAssembler(REPO_ROOT / "harness" / "prompts"),
        query_fn=scripted_query([init_message(), result()]),
    )
    await spawner.spawn("sol-900", "L-900")
    row = db.fetch_one("select count(*) as n from events where agent_id = 'sol-900' and type = 'agent.spawned'")
    assert int(row["n"]) == 1


async def test_spawn_registers_the_directory_it_runs_in(op, db, tmp_path):
    # task_complete resolves result paths against agents.cwd, so the registered cwd must be the
    # real session directory, not a relative or unresolved path.
    spawner = Spawner(
        op, "http://127.0.0.1:1", tmp_path, PromptAssembler(REPO_ROOT / "harness" / "prompts"),
        query_fn=scripted_query([init_message(), result()]),
    )
    await spawner.spawn("sol-901", "L-901")
    cwd = db.fetch_one("select cwd from agents where id = 'sol-901'")["cwd"]
    assert Path(cwd) == (tmp_path / "agents" / "sol-901").resolve() and Path(cwd).is_dir()
