"""Resume after a restore, the population's reshape behaviour, and the spend halt end to end."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.orchestrator.governor import Governor
from harness.orchestrator.monitor import StaticKnobs
from harness.orchestrator.population import Population, lineage_ids, lineage_index
from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.shape import Shape, ShapeReader
from harness.orchestrator.spawner import Spawner
from harness.orchestrator.succession import LineageRunner, first_agent_id, generation_of
from harness.tests.control_fixtures import write_control_dir
from harness.tests.orchestrator_fakes import FakeClient, FakeOperator, assistant, client_factory, init_message, result

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
STUB = "## State of work\nheld T-007; last posts 12, 15\n## Open threads\n## What to try next\n## What not to retry\n## Files worth reading\n"


def make(tmp_path, clients, op: FakeOperator | None = None, governor=None, **runner_kw):
    op = op or FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR),
                      client_factory=client_factory(clients), knobs=StaticKnobs(), context_window=1000, governor=governor)
    runner = LineageRunner(spawner, op, "HANDOFF REQUEST", max_generations=5, poll_s=0, **runner_kw)
    return runner, op


def test_agent_id_helpers():
    assert first_agent_id("sol-002-g3") == "sol-002" and first_agent_id("sol-002") == "sol-002"
    assert generation_of("sol-002-g3") == 3 and generation_of("sol-002") == 1
    assert lineage_ids(4) == ("L-004", "sol-004") and lineage_index("L-012") == 12
    with pytest.raises(ValueError):
        lineage_index("S-001")


async def test_resume_registers_the_next_generation_from_the_unclaimed_handoff(tmp_path):
    op = FakeOperator()
    # What the database holds after a restore: sol-002-g2 was mid-flight and the CLI wrote a stub for it.
    op.register_agent("sol-002", "solver", "L-002", str(tmp_path / "agents" / "sol-002"))
    op.register_agent("sol-002-g2", "solver", "L-002", str(tmp_path / "agents" / "sol-002"), generation=2)
    op.write_handoff("sol-002-g2", STUB, stub=True)
    row = {**op.latest_handoff("L-002"), "lineage_id": "L-002", "from_agent": "sol-002-g2", "generation": 2}
    only = FakeClient([init_message(), assistant(100), result(num_turns=1, text="done")])
    runner, op = make(tmp_path, [only], op)

    outcome = await runner.resume("L-002", "sol-002-g2", row)

    # Registered through succeed, exactly as after a kill: generation 3, same directory, stub framing.
    assert op.succeeded == [("sol-002-g2", "sol-002-g3", str((tmp_path / "agents" / "sol-002").resolve()))]
    assert op.registered[-1]["id"] == "sol-002-g3" and op.registered[-1]["generation"] == 3
    assert "This handoff is a stub" in only.options.system_prompt and "held T-007" in only.options.system_prompt
    assert op.latest_handoff("L-002")["to_agent"] == "sol-002-g3"
    assert outcome.resumed_from == "sol-002-g2" and outcome.agent_id == "sol-002" and outcome.ended_by == "natural"
    resumed = [e for e in op.events if e[1] == "lineage.resumed"][0]
    assert resumed[0] == "sol-002-g3" and resumed[2]["stub"] is True and resumed[2]["generation"] == 3


async def test_population_resumes_first_then_fills_to_the_target(tmp_path):
    op = FakeOperator()
    op.register_agent("sol-002", "solver", "L-002", str(tmp_path / "agents" / "sol-002"))
    op.write_handoff("sol-002", STUB, stub=True)
    op.resume_rows = [{**op.latest_handoff("L-002"), "lineage_id": "L-002", "from_agent": "sol-002", "generation": 1}]
    clients = [FakeClient([init_message(), result(text="done")]) for _ in range(3)]
    runner, op = make(tmp_path, clients, op)
    population = Population(runner, ShapeReader(op, Shape(solvers=3)), stagger_s=0, resume_rows=op.resume_rows, poll_s=0)

    results = await population.run()

    ids = [a["id"] for a in op.registered]
    # The resumed lineage took slot 2 as sol-002-g2; slots 1 and 3 were started fresh.
    assert "sol-002-g2" in ids and "sol-001" in ids and "sol-003" in ids and "sol-002-g3" not in ids
    assert sorted(r.lineage_id for r in results) == ["L-001", "L-002", "L-003"]
    assert [r for r in results if r.lineage_id == "L-002"][0].resumed_from == "sol-002"


async def test_reshape_down_closes_the_slot_at_the_next_succession(tmp_path):
    op = FakeOperator()
    reader = ShapeReader(op, Shape(solvers=1), poll_s=0.0)
    first = FakeClient([init_message(), assistant(850), assistant(860)])
    runner, op = make(tmp_path, [first], op)
    population = Population(runner, reader, stagger_s=0, poll_s=0)
    runner.set_slot_open(population.slot_open)

    def on_query(client, text):
        if text == "HANDOFF REQUEST":
            # The operator reshaped to zero solvers while this agent was running.
            op.control["shape"] = Shape(solvers=0).to_json()
            op.write_handoff("sol-001", STUB)

    first.on_query = on_query
    results = await population.run()
    assert results[0].ended_by == "reshaped" and results[0].generations == 1 and op.succeeded == []
    ended = [e for e in op.events if e[1] == "lineage.ended"][0][2]
    assert ended["by"] == "reshaped"
    # The handoff is left unclaimed, so a later resume can pick the lineage up again.
    assert op.latest_handoff("L-001")["to_agent"] is None


async def test_reshape_up_starts_new_slots_while_the_population_runs(tmp_path):
    op = FakeOperator()
    reader = ShapeReader(op, Shape(solvers=1), poll_s=0.0)
    first = FakeClient([init_message(), assistant(100), assistant(110), result(text="done")])
    second = FakeClient([init_message(), result(text="done")])
    runner, op = make(tmp_path, [first, second], op)
    population = Population(runner, reader, stagger_s=0, poll_s=0)
    # The reshape lands once sol-001 is under way; the population's next check starts sol-002.
    original = first.receive_messages

    async def receive():
        async for m in original():
            yield m
            op.control["shape"] = Shape(solvers=2).to_json()

    first.receive_messages = receive
    results = await population.run()
    assert sorted(r.lineage_id for r in results) == ["L-001", "L-002"]
    assert [a["id"] for a in op.registered] == ["sol-001", "sol-002"]


async def test_spend_halt_ends_the_lineage_and_main_exits_two(tmp_path, monkeypatch):
    from harness.orchestrator import main as m

    op = FakeOperator()
    op.spend = 10.0
    control = write_control_dir(tmp_path)
    governor = Governor(op, control / "ceilings.toml", poll_s=0.0)
    runner, op = make(tmp_path, [FakeClient([init_message(), result()])], op, governor=governor)

    outcome = await runner.run("L-001", "sol-001")

    assert outcome.ended_by == "halted" and "ceiling is $5.00" in outcome.halted and outcome.sessions == []
    assert op.registered == []  # refused before the agent registered
    ended = [e for e in op.events if e[1] == "lineage.ended"][0][2]
    assert ended["by"] == "halted"
    assert m.exit_code([outcome], []) == 2


def test_main_exits_two_and_names_the_halt(monkeypatch, capsys):
    from harness.orchestrator import main as m
    from harness.orchestrator.succession import LineageResult

    halted = LineageResult("L-001", "sol-001", ended_by="halted", halted="spend in the last 24 hours is $5.00, the ceiling is $5.00")
    monkeypatch.setattr(m, "amain", lambda cfg: _ready(([halted], [])))
    monkeypatch.setattr(m.RunConfig, "from_args", classmethod(lambda cls, argv=None: cls()))
    assert m.main([]) == 2
    assert "halted by the governor: spend in the last 24 hours" in capsys.readouterr().out
    # A halt outranks an ordinary error in the exit code, and no halt means the old rule.
    errored = LineageResult("L-002", "sol-002", ended_by="error", error="boom")
    assert m.exit_code([errored, halted], []) == 2 and m.exit_code([errored], []) == 1 and m.exit_code([], []) == 0


async def test_spend_halt_on_a_successor_ends_the_registered_agent(tmp_path):
    op = FakeOperator()
    control = write_control_dir(tmp_path)
    governor = Governor(op, control / "ceilings.toml", poll_s=0.0)
    first = FakeClient([init_message(), assistant(850), assistant(860)])
    runner, op = make(tmp_path, [first], op, governor=governor)

    def on_query(client, text):
        op.write_handoff("sol-001", STUB)
        op.spend = 5.0  # the ceiling is reached while the handoff is being written

    first.on_query = on_query
    outcome = await runner.run("L-001", "sol-001")
    assert outcome.ended_by == "halted" and outcome.handoffs == 1 and outcome.generations == 1
    # succeed registered sol-001-g2 before the gate refused it; it must not linger as live.
    assert op.succeeded[0][1] == "sol-001-g2" and ("sol-001-g2", "ended") in op.status


async def _ready(value):
    return value
