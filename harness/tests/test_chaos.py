"""Chaos controls: the reader over control_state and the runner's chaos kill."""

from __future__ import annotations

from pathlib import Path

from harness.orchestrator.chaos import ChaosReader, normalise
from harness.orchestrator.monitor import StaticKnobs
from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import Spawner
from harness.orchestrator.succession import LineageRunner
from harness.tests.orchestrator_fakes import FakeClient, FakeOperator, assistant, client_factory, init_message, result

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def test_normalise_fills_defaults_and_coerces():
    assert normalise(None) == {"kill": [], "board_down_until": None, "lag_ms": 0}
    assert normalise({"kill": ["sol-001"], "lag_ms": "250", "junk": 1}) == {"kill": ["sol-001"], "board_down_until": None, "lag_ms": 250}


def test_reader_caches_for_poll_s_and_acknowledges():
    op = FakeOperator()
    op.control["chaos"] = {"kill": ["sol-003"]}
    ticks = iter([0.0, 0.5, 1.0, 5.0, 5.0, 5.0])
    reader = ChaosReader(op, poll_s=2.0, clock=lambda: next(ticks))
    assert reader.should_kill("sol-003") and not reader.should_kill("sol-001")
    # Written between polls: not seen until the cache ages out.
    op.control["chaos"] = {"kill": ["sol-003", "sol-001"]}
    assert not reader.should_kill("sol-001")
    assert reader.should_kill("sol-001")
    reader.acknowledge("sol-003")
    assert op.control["chaos"]["kill"] == ["sol-001"] and reader.current()["kill"] == ["sol-001"]


def make(tmp_path: Path, clients: list[FakeClient], op: FakeOperator):
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR),
                      client_factory=client_factory(clients), knobs=StaticKnobs({"kill_grace_s": 0.0}), context_window=1000)
    runner = LineageRunner(spawner, op, "HANDOFF REQUEST", max_generations=5, poll_s=0,
                           chaos=ChaosReader(op, poll_s=0))
    return runner


async def test_chaos_kill_takes_the_kill_path_writes_a_stub_and_spawns_a_successor(tmp_path):
    op = FakeOperator()
    op.control["chaos"] = {"kill": ["sol-050"], "board_down_until": None, "lag_ms": 0}
    first = FakeClient([init_message(), assistant(100), assistant(110), assistant(120)])
    second = FakeClient([init_message(), result()])
    runner = make(tmp_path, [first, second], op)

    outcome = await runner.run("L-050", "sol-050")

    killed = [e for e in op.events if e[1] == "agent.killed"]
    assert killed and killed[0][2]["reason"] == "chaos kill" and killed[0][2]["requested"] is False
    assert [e for e in op.events if e[1] == "chaos.killed"][0][2] == {"agent_id": "sol-050", "generation": 1}
    assert ("sol-050", "dead") in op.status and outcome.stubs == 1 and outcome.generations == 2
    # Acknowledged: the id is gone, so the successor (a different id anyway) is not hit again.
    assert op.control["chaos"]["kill"] == [] and first.interrupted
    assert [a["id"] for a in op.registered] == ["sol-050", "sol-050-g2"]


async def test_no_chaos_entry_means_no_kill(tmp_path):
    op = FakeOperator()
    only = FakeClient([init_message(), assistant(100), result(num_turns=1, text="done")])
    runner = make(tmp_path, [only], op)
    outcome = await runner.run("L-051", "sol-051")
    assert outcome.ended_by == "natural" and outcome.stubs == 0
    assert not [e for e in op.events if e[1] == "chaos.killed"]


async def test_chaos_kill_during_handoff_still_counts_as_a_kill(tmp_path):
    # Asked to hand off at 850, then the operator names it: the kill wins over the grace wait.
    op = FakeOperator()
    first = FakeClient([init_message(), assistant(850), assistant(860), assistant(870)])
    second = FakeClient([init_message(), result()])
    runner = make(tmp_path, [first, second], op)

    def on_query(client: FakeClient, text: str) -> None:
        # The request has gone out; now the operator pulls the chaos lever.
        op.control["chaos"] = {"kill": ["sol-052"], "board_down_until": None, "lag_ms": 0}

    first.on_query = on_query
    outcome = await runner.run("L-052", "sol-052")
    killed = [e for e in op.events if e[1] == "agent.killed"]
    assert killed and killed[0][2]["reason"] == "chaos kill" and killed[0][2]["requested"] is True
    assert outcome.stubs == 1 and outcome.handoffs == 0
