"""Supervisor rounds and the cron, driven by scripted clients and the fake operator."""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness.orchestrator.prompts import BRIEF_HEADING, PromptAssembler
from harness.orchestrator.spawner import SUPERVISOR_KICKOFF, SUPERVISOR_TOOLS, Spawner
from harness.orchestrator.supervisors import RoundResult, SupervisorCron, SupervisorRound, format_rounds, supervisor_ids
from harness.tests.orchestrator_fakes import FakeClient, FakeOperator, assistant, client_factory, init_message, result
from harness.tests.test_brief import full_brief

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def make_round(tmp_path: Path, clients: list[FakeClient], count: int = 1, **spawner_kw) -> tuple[SupervisorRound, FakeOperator, object]:
    op = FakeOperator()
    op.brief_template = full_brief()
    factory = client_factory(clients)
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=factory,
                      model="claude-opus-5-5", max_turns=15, **spawner_kw)
    return SupervisorRound(spawner, op, count), op, factory


def test_supervisor_ids_follow_slot_and_round():
    assert supervisor_ids(1, 3) == ("sup-001-r3", "S-001", "sup-001")
    assert supervisor_ids(2, 1) == ("sup-002-r1", "S-002", "sup-002")


async def test_round_opens_briefs_spawns_and_closes(tmp_path):
    client = FakeClient([init_message(), assistant(500), result(num_turns=4, cost=0.03, text="round done")])
    rnd, op, factory = make_round(tmp_path, [client])
    op.reports[1] = [{"id": 40, "author_id": "sup-001-r1", "body": "## Window\n..."},
                     {"id": 41, "author_id": "sup-001-r1", "body": "[ping: succession_failing] two windows"}]
    op.reopened[1] = ["T-005"]

    outcome = await rnd.run(1)

    # The round row was opened and the brief built before any spawn, then closed with the summary.
    assert [e[1] for e in op.events if e[1].startswith("round.")] == ["round.opened", "round.brief_rendered", "round.closed"]
    assert op.rounds[0]["brief"]["round"] == {"id": 1, "number": 1, "final": False, "window_start": "2026-10-16T10:00:00",
                                              "window_end": "2026-10-16T10:10:00", "supervisors": 1}
    # One supervisor, registered as such, in its slot directory, generation = round number.
    assert op.registered == [{"id": "sup-001-r1", "role": "supervisor", "lineage_id": "S-001",
                              "cwd": str((tmp_path / "agents" / "sup-001").resolve()), "generation": 1, "harness_version": "dev"}]
    opts = client.options
    assert opts.tools == SUPERVISOR_TOOLS and opts.add_dirs == [str(tmp_path / "shared")]
    assert client.connected_with.startswith(SUPERVISOR_KICKOFF)
    # The rendered brief is in the prompt, after the policies and before identity.
    prompt = opts.system_prompt
    assert BRIEF_HEADING in prompt and "### Kill: sol-001" in prompt
    assert prompt.index("# Succession health") < prompt.index(BRIEF_HEADING) < prompt.index("agent_id: sup-001-r1")
    assert "role: supervisor" in prompt and "generation: 1" in prompt and "lineage_id: S-001" in prompt
    # The summary read back from the board, not from what the agent said.
    assert outcome.reports == 2 and outcome.pings == 1 and outcome.reopened == ["T-005"]
    assert outcome.cost_usd == 0.03 and outcome.turns == 4 and outcome.error is None and not outcome.failed
    assert op.rounds[0]["summary"] == {"reports": 2, "pings": 1, "tuned": None, "reopened": ["T-005"], "cost_usd": 0.03,
                                       "turns": 4, "sessions": 1, "errors": []}
    assert op.current_round() is None


async def test_round_reads_the_tune_from_the_round_row(tmp_path):
    client = FakeClient([init_message(), result(cost=0.01)])
    rnd, op, _ = make_round(tmp_path, [client])
    original_current = op.current_round

    def current_with_tune():
        row = original_current()
        return {**row, "tuned_knob": "kill_grace_turns", "tuned_from": 0.0, "tuned_to": 1.0} if row else None

    op.current_round = current_with_tune
    outcome = await rnd.run(2)
    assert outcome.tuned == "kill_grace_turns" and outcome.tuned_from == 0.0 and outcome.tuned_to == 1.0
    assert "tuned kill_grace_turns (0 -> 1)" in format_rounds([outcome])


async def test_several_supervisors_run_together_and_share_nothing_but_the_round(tmp_path):
    gate = asyncio.Event()
    started: list[str] = []

    class Gated(FakeClient):
        async def connect(self, prompt=None):
            started.append(prompt or "")
            await gate.wait()
            await super().connect(prompt)

    clients = [Gated([init_message(), result(cost=0.01)]), Gated([init_message(), result(cost=0.02)])]
    rnd, op, _ = make_round(tmp_path, clients, count=2)
    task = asyncio.create_task(rnd.run(1))
    await asyncio.sleep(0.01)
    assert len(started) == 2, "both sessions start before either finishes"
    gate.set()
    outcome = await task
    assert [a["id"] for a in op.registered] == ["sup-001-r1", "sup-002-r1"]
    assert [a["cwd"] for a in op.registered] == [str((tmp_path / "agents" / "sup-001").resolve()), str((tmp_path / "agents" / "sup-002").resolve())]
    assert outcome.cost_usd == 0.03 and len(outcome.sessions) == 2


async def test_directory_is_shared_across_rounds_and_generation_tracks_the_round(tmp_path):
    rnd, op, factory = make_round(tmp_path, [FakeClient([init_message(), result()]), FakeClient([init_message(), result()])])
    await rnd.run(1)
    await rnd.run(2)
    assert [a["id"] for a in op.registered] == ["sup-001-r1", "sup-001-r2"]
    assert op.registered[0]["cwd"] == op.registered[1]["cwd"]
    assert [a["generation"] for a in op.registered] == [1, 2]
    assert "generation: 2" in factory.handed[1].options.system_prompt


async def test_round_survives_a_session_error_and_a_broken_brief(tmp_path):
    # A session that fails to attach to the board is an error on the result, not a crash of the round.
    rnd, op, _ = make_round(tmp_path, [FakeClient([init_message(connected=False)])])
    outcome = await rnd.run(1)
    assert outcome.failed and outcome.sessions[0].error and outcome.error is None
    assert op.rounds[0]["ended_at"] == "closed" and op.rounds[0]["summary"]["errors"]

    # A brief the operator cannot build still closes the round and records the error.
    rnd2, op2, _ = make_round(tmp_path, [FakeClient([init_message(), result()])])

    def boom(round_id):
        raise RuntimeError("no database")

    op2.round_brief = boom
    outcome2 = await rnd2.run(1)
    assert outcome2.error == "RuntimeError: no database" and outcome2.failed
    assert op2.rounds[0]["ended_at"] == "closed" and op2.registered == []


async def test_cron_runs_every_interval_then_a_final_round():
    calls: list[tuple[int, bool]] = []
    sleeps: list[float] = []
    done = asyncio.Event()

    class FakeRound:
        async def run(self, number: int, final: bool = False) -> RoundResult:
            calls.append((number, final))
            if number == 2:
                done.set()  # the population finishes during round two
            return RoundResult(number=number, final=final)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        await asyncio.sleep(0)

    cron = SupervisorCron(FakeRound(), 15, sleep=fake_sleep)
    results = await cron.run_until(done)
    assert calls == [(1, False), (2, False), (3, True)]
    assert sleeps == [15, 15]
    assert [r.number for r in results] == [1, 2, 3] and results[-1].final


async def test_cron_with_a_population_that_finishes_first_runs_only_the_final_round():
    calls: list[tuple[int, bool]] = []
    done = asyncio.Event()
    done.set()

    class FakeRound:
        async def run(self, number: int, final: bool = False) -> RoundResult:
            calls.append((number, final))
            return RoundResult(number=number, final=final)

    never = asyncio.Event()

    async def hang(_s: float) -> None:
        await never.wait()

    results = await SupervisorCron(FakeRound(), 600, sleep=hang).run_until(done)
    assert calls == [(1, True)] and len(results) == 1


async def test_cron_wakes_early_when_the_population_finishes_mid_interval():
    done = asyncio.Event()
    calls: list[tuple[int, bool]] = []

    class FakeRound:
        async def run(self, number: int, final: bool = False) -> RoundResult:
            calls.append((number, final))
            return RoundResult(number=number, final=final)

    never = asyncio.Event()

    async def hang(_s: float) -> None:
        await never.wait()

    task = asyncio.create_task(SupervisorCron(FakeRound(), 600, sleep=hang).run_until(done))
    await asyncio.sleep(0.01)
    assert calls == []
    done.set()
    results = await asyncio.wait_for(task, 1)
    assert calls == [(1, True)] and results[0].final


def test_format_rounds_lines():
    rows = [
        RoundResult(number=1, final=False, reports=1, pings=0, tuned=None, reopened=[], cost_usd=0.0123, turns=6),
        RoundResult(number=2, final=True, reports=1, pings=1, tuned="handoff_pct", tuned_from=80, tuned_to=75, reopened=["T-005"],
                    cost_usd=0.02, cost_estimated=True, turns=8, error="RuntimeError: x"),
    ]
    out = format_rounds(rows).splitlines()
    assert out[0] == "round 1: reports 1, pings 0, tuned none, reopened [], turns 6, cost $0.0123"
    assert out[1] == "round 2 (final): reports 1, pings 1, tuned handoff_pct (80 -> 75), reopened ['T-005'], turns 8, cost $~0.0200, error: RuntimeError: x"


async def test_cron_continues_round_numbers_from_first_number(tmp_path):
    # After a restore the orchestrator restarts; round ids must not collide with the old epoch's.
    from harness.orchestrator.supervisors import SupervisorCron

    class StubRound:
        def __init__(self):
            self.numbers = []

        async def run(self, number, final=False):
            self.numbers.append((number, final))
            return RoundResult(number=number, final=final)

    done = asyncio.Event()
    done.set()
    cron = SupervisorCron(StubRound(), interval_s=0, sleep=lambda s: asyncio.sleep(0), first_number=4)
    results = await cron.run_until(done)
    assert [r.number for r in results] == [4] and results[0].final is True
