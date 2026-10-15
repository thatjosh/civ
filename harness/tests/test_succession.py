"""The lineage runner, driven by scripted clients and a fake operator. No network, no database."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.orchestrator.monitor import StaticKnobs
from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import Spawner
from harness.orchestrator.succession import LineageRunner
from harness.tests.orchestrator_fakes import FakeClient, FakeOperator, assistant, client_factory, init_message, result

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
REQUEST = "HANDOFF REQUEST TEXT"
HANDOFF_BODY = "## State of work\nhalf done\n## Open threads\nseq 9\n## What to try next\nx\n## What not to retry\ny\n## Files worth reading\nwork/a.txt"


def make(tmp_path: Path, clients: list[FakeClient], window: int = 1000, max_generations: int = 5, force_after=None, **runner_kw):
    op = FakeOperator()
    factory = client_factory(clients)
    spawner = Spawner(
        op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR),
        client_factory=factory, knobs=StaticKnobs(), context_window=window, force_handoff_after_turns=force_after,
    )
    runner = LineageRunner(spawner, op, REQUEST, max_generations=max_generations, poll_s=0, **runner_kw)
    return runner, op, factory


def handoff_on_request(op: FakeOperator, agent_id: str):
    """What a well-behaved agent does: the injected request arrives, it calls civ_handoff."""

    def on_query(client: FakeClient, text: str) -> None:
        if text == REQUEST:
            op.write_handoff(agent_id, HANDOFF_BODY)

    return on_query


async def test_handoff_at_threshold_spawns_generation_two_with_the_handoff(tmp_path):
    first = FakeClient([init_message(), assistant(100), assistant(850), assistant(860), assistant(870)])
    second = FakeClient([init_message(), assistant(100), result(num_turns=2, cost=0.02, text="no tasks left")])
    runner, op, factory = make(tmp_path, [first, second])
    first.on_query = handoff_on_request(op, "sol-001")

    outcome = await runner.run("L-001", "sol-001")

    # The request went down both channels: the status flip and the injected message.
    assert op.handoff_requests == ["sol-001"]
    assert first.sent == [REQUEST]
    # The runner cut the first session once the handoff row existed, and never on the second.
    assert first.interrupted and first.disconnected
    assert not second.interrupted and second.disconnected
    # Generation two was registered through succeed, in the same directory, with the handoff in its prompt.
    assert op.succeeded == [("sol-001", "sol-001-g2", str((tmp_path / "agents" / "sol-001").resolve()))]
    assert op.registered[-1]["generation"] == 2 and op.registered[-1]["lineage_id"] == "L-001"
    assert "half done" in second.options.system_prompt and "generation: 2" in second.options.system_prompt
    assert second.options.cwd == str((tmp_path / "agents" / "sol-001").resolve())
    assert op.latest_handoff("L-001")["to_agent"] == "sol-001-g2"
    assert outcome.generations == 2 and outcome.handoffs == 1 and outcome.stubs == 0
    assert outcome.ended_by == "natural" and outcome.error is None
    assert outcome.sessions[0].stopped_by_runner and not outcome.sessions[1].stopped_by_runner
    types = [e[1] for e in op.events if e[0] == "sol-001"]
    assert "agent.handoff_sent" in types and "agent.handed_off" in types


async def test_kill_without_handoff_writes_a_stub_and_spawns_a_successor(tmp_path):
    # The agent ignores the request: no on_query hook writes a handoff, and the context keeps growing.
    first = FakeClient([init_message(), assistant(850), assistant(880), assistant(950), assistant(960)])
    second = FakeClient([init_message(), result()])
    runner, op, factory = make(tmp_path, [first, second])
    (tmp_path / "agents" / "sol-001" / "work").mkdir(parents=True)
    (tmp_path / "agents" / "sol-001" / "work" / "draft.txt").write_text("x")

    outcome = await runner.run("L-001", "sol-001")

    assert op.handoff_requests == ["sol-001"] and first.sent == [REQUEST]
    assert ("sol-001", "dead") in op.status
    assert op.stubs == [("sol-001", ["work/draft.txt"])]
    assert op.latest_handoff("L-001")["stub"] is True
    assert "This handoff is a stub" in second.options.system_prompt
    assert outcome.stubs == 1 and outcome.handoffs == 0 and outcome.generations == 2
    kill = [e for e in op.events if e[1] == "agent.killed"]
    assert kill and kill[0][2]["reason"] == "kill threshold reached"


async def test_natural_end_stops_the_lineage(tmp_path):
    only = FakeClient([init_message(), assistant(100), result(num_turns=1, text="done")])
    runner, op, _ = make(tmp_path, [only])
    outcome = await runner.run("L-002", "sol-002")
    assert outcome.generations == 1 and outcome.ended_by == "natural"
    assert op.handoff_requests == [] and op.succeeded == []
    assert ("sol-002", "ended") in op.status


async def test_max_generations_stops_after_a_handoff(tmp_path):
    first = FakeClient([init_message(), assistant(850), assistant(860)])
    runner, op, _ = make(tmp_path, [first], max_generations=1)
    first.on_query = handoff_on_request(op, "sol-003")
    outcome = await runner.run("L-003", "sol-003")
    assert outcome.generations == 1 and outcome.handoffs == 1
    assert outcome.ended_by == "max_generations"
    assert op.succeeded == []


async def test_forced_handoff_fires_on_turn_count_without_fill(tmp_path):
    first = FakeClient([init_message(), assistant(10), assistant(10), assistant(10), assistant(10)])
    second = FakeClient([init_message(), result()])
    runner, op, _ = make(tmp_path, [first, second], window=1_000_000, force_after=2)
    first.on_query = handoff_on_request(op, "sol-004")
    outcome = await runner.run("L-004", "sol-004")
    assert op.handoff_requests == ["sol-004"]
    forced = [e for e in op.events if e[1] == "agent.handoff_forced"]
    assert forced and forced[0][2]["turn"] == 2
    assert outcome.generations == 2 and outcome.handoffs == 1


async def test_handoff_timeout_becomes_a_kill(tmp_path):
    # The agent never hands off and never fills up further; the watcher's clock runs out.
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0])
    first = FakeClient([init_message(), assistant(850)] + [assistant(851)] * 6)
    second = FakeClient([init_message(), result()])
    runner, op, _ = make(tmp_path, [first, second], clock=lambda: next(ticks, 1000.0), handoff_wait_s=10)
    outcome = await runner.run("L-005", "sol-005")
    kill = [e for e in op.events if e[1] == "agent.killed"]
    assert kill and kill[0][2]["reason"] == "handoff timed out"
    assert first.interrupted and outcome.stubs == 1


async def test_session_that_ends_after_handing_off_is_still_a_handoff(tmp_path):
    # The agent writes its handoff and then its session ends on its own before the watcher ran.
    first = FakeClient([init_message(), assistant(850)])
    second = FakeClient([init_message(), result()])
    runner, op, _ = make(tmp_path, [first, second])

    def on_query(client: FakeClient, text: str) -> None:
        op.write_handoff("sol-006", HANDOFF_BODY)
        client.messages.append(result(num_turns=3, text="handed off"))

    first.on_query = on_query
    outcome = await runner.run("L-006", "sol-006")
    assert outcome.handoffs == 1 and outcome.stubs == 0 and outcome.generations == 2


async def test_auto_compaction_left_on_is_logged_as_a_problem(tmp_path):
    usage = {"totalTokens": 100, "maxTokens": 1000, "percentage": 10.0, "isAutoCompactEnabled": True, "autoCompactThreshold": 700}
    only = FakeClient([init_message(), assistant(100), result()], usage=usage)
    runner, op, _ = make(tmp_path, [only])
    await runner.run("L-007", "sol-007")
    state = [e for e in op.events if e[1] == "agent.compaction_state"]
    assert len(state) == 1
    # handoff_pct 80 is above the CLI's 70 percent threshold, so compaction would win: a problem.
    assert state[0][2] == {"auto_compact_enabled": True, "threshold_pct": 70.0, "handoff_pct": 80.0, "problem": True}
    assert only.options.env == {"DISABLE_AUTO_COMPACT": "1"}


async def test_probe_takes_the_larger_estimate_and_can_fire_the_handoff(tmp_path):
    # Arithmetic says 10 percent; the CLI says 850 of our 1000-token window. The larger wins.
    usage = {"totalTokens": 850, "maxTokens": 1_000_000, "percentage": 0.085, "isAutoCompactEnabled": False}
    first = FakeClient([init_message()] + [assistant(100)] * 6, usage=usage)
    second = FakeClient([init_message(), result()])
    runner, op, _ = make(tmp_path, [first, second])
    first.on_query = handoff_on_request(op, "sol-008")
    outcome = await runner.run("L-008", "sol-008")
    probes = [e for e in op.events if e[1] == "agent.context_probe"]
    assert probes and probes[0][2]["probe_pct"] == 85.0 and probes[0][2]["arith_pct"] == 10.0
    assert op.handoff_requests == ["sol-008"] and outcome.handoffs == 1


async def test_error_in_first_session_ends_the_lineage_with_the_error(tmp_path):
    only = FakeClient([init_message(connected=False)])
    runner, op, _ = make(tmp_path, [only])
    outcome = await runner.run("L-009", "sol-009")
    assert outcome.ended_by == "error" and "McpNotConnected" in (outcome.error or "")
    assert outcome.generations == 1 and op.succeeded == []


def test_lineage_result_aggregates_sessions():
    from harness.orchestrator.spawner import SpawnResult
    from harness.orchestrator.succession import LineageResult

    r = LineageResult("L-1", "sol-1", sessions=[
        SpawnResult("sol-1", turns=5, cost_usd=0.1, tokens_in=100, tokens_out=5, context_pct=80.0),
        SpawnResult("sol-1-g2", turns=3, cost_usd=0.05, tokens_in=50, tokens_out=2, context_pct=12.0),
    ], handoffs=1)
    assert r.generations == 2 and r.turns == 8 and r.cost_usd == pytest.approx(0.15)
    assert r.tokens_in == 150 and r.tokens_out == 7 and r.context_pct == 12.0


async def test_ids_survive_the_trip_through_run_population(tmp_path):
    # The runner names successors after the agent (sol-001-g2) inside the lineage (L-001),
    # and run_population must hand it the two ids by keyword so they cannot swap.
    from harness.orchestrator.main import run_population

    first = FakeClient([init_message(), assistant(850), assistant(860)])
    second = FakeClient([init_message(), result()])
    runner, op, _ = make(tmp_path, [first, second])
    first.on_query = handoff_on_request(op, "sol-001")

    results = await run_population(1, 0, runner.run)

    assert [a["id"] for a in op.registered] == ["sol-001", "sol-001-g2"]
    assert {a["lineage_id"] for a in op.registered} == {"L-001"}
    assert results[0].lineage_id == "L-001" and results[0].agent_id == "sol-001"


async def test_handed_off_session_reports_its_real_cost(tmp_path):
    first = FakeClient([init_message(), assistant(100), assistant(850), assistant(860), assistant(870)])
    second = FakeClient([init_message(), assistant(100), result(num_turns=2, cost=0.02)])
    runner, op, _ = make(tmp_path, [first, second])
    first.on_query = handoff_on_request(op, "sol-030")
    outcome = await runner.run("L-030", "sol-030")
    # The interrupted first session drained the CLI's result: a real cost, not an estimate.
    assert outcome.sessions[0].cost_usd == 0.05 and not outcome.sessions[0].cost_estimated
    assert outcome.cost_usd == pytest.approx(0.07) and not outcome.cost_estimated
    ends = [e for e in op.events if e[1] == "agent.ended"]
    assert [e[0] for e in ends] == ["sol-030", "sol-030-g2"]
