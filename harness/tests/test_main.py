import asyncio

import pytest

from harness.orchestrator.main import RunConfig, format_summary, parse_preset, run_population, run_with_supervisors
from harness.orchestrator.supervisors import RoundResult
from harness.orchestrator.spawner import SpawnResult


async def test_run_population_staggers_starts_and_runs_concurrently():
    started: list[tuple[str, str]] = []
    sleeps: list[float] = []
    release = asyncio.Event()

    async def spawn(agent_id: str, lineage_id: str) -> SpawnResult:
        started.append((agent_id, lineage_id))
        await release.wait()
        return SpawnResult(agent_id=agent_id, turns=1, cost_usd=0.1)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        await asyncio.sleep(0)

    task = asyncio.create_task(run_population(3, 2.0, spawn, sleep=fake_sleep))
    await asyncio.sleep(0.01)
    # All three have started while none has finished, so they run concurrently.
    assert started == [("sol-001", "L-001"), ("sol-002", "L-002"), ("sol-003", "L-003")]
    assert sleeps == [2.0, 2.0]
    release.set()
    results = await task
    assert [r.agent_id for r in results] == ["sol-001", "sol-002", "sol-003"]


def test_format_summary_totals_cost_and_shows_errors():
    rows = [
        SpawnResult("sol-001", turns=5, cost_usd=0.25, tokens_in=1000, tokens_out=50, context_pct=0.1, result="done"),
        SpawnResult("sol-002", turns=0, cost_usd=None, error="McpNotConnected: nope"),
    ]
    out = format_summary(rows, {"open": 0, "done": 3})
    assert "sol-001" in out and "0.2500" in out
    assert "McpNotConnected" in out
    assert "total" in out and "done=3" in out


def test_run_config_from_args_and_env(monkeypatch):
    monkeypatch.setenv("CIV_BOARD_URL", "http://10.0.0.1:9000")
    cfg = RunConfig.from_args(["--solvers", "5", "--max-turns", "12", "--stagger", "0"])
    assert cfg.solvers == 5 and cfg.max_turns == 12 and cfg.stagger_s == 0
    assert cfg.board_url == "http://10.0.0.1:9000"
    assert cfg.model == "claude-opus-5-5"


async def test_run_population_hands_ids_to_a_lineage_runner_the_right_way_round():
    # LineageRunner.run takes (lineage_id, agent_id) while a spawner takes
    # (agent_id, lineage_id); run_population must pass both by keyword so
    # neither order can swap them (a swap once produced agents called L-001).
    seen: list[tuple[str, str]] = []

    class RunnerShaped:
        async def run(self, lineage_id: str, agent_id: str) -> SpawnResult:
            seen.append((lineage_id, agent_id))
            return SpawnResult(agent_id=agent_id)

    await run_population(2, 0, RunnerShaped().run)
    assert seen == [("L-001", "sol-001"), ("L-002", "sol-002")]


def test_format_summary_marks_estimated_costs_with_a_tilde():
    rows = [
        SpawnResult("sol-001", turns=5, cost_usd=0.25),
        SpawnResult("sol-002", turns=3, cost_usd=0.1234, cost_estimated=True),
    ]
    out = format_summary(rows)
    lines = out.splitlines()
    assert " 0.2500 " in lines[1] and "~0.1234" in lines[2]
    # One estimated row makes the total an estimate too.
    assert lines[-1].startswith("total cost") and "~0.3734" in lines[-1]


def test_supervisor_flags_default_to_no_rounds_and_parse_presets():
    cfg = RunConfig.from_args([])
    assert cfg.supervisors == 0 and cfg.supervisor_interval_s == 600 and cfg.supervisor_max_turns == 15
    assert cfg.supervisor_model is None and cfg.supervisor_model_id == cfg.model and cfg.presets == []

    cfg = RunConfig.from_args(["--supervisors", "2", "--supervisor-interval", "15", "--supervisor-model", "claude-opus-5-5",
                               "--supervisor-max-turns", "9", "--model", "claude-haiku-5-5",
                               "--preset", "kill_grace_turns=0", "--preset", "handoff_pct=85"])
    assert cfg.supervisors == 2 and cfg.supervisor_interval_s == 15 and cfg.supervisor_max_turns == 9
    assert cfg.supervisor_model_id == "claude-opus-5-5" and cfg.model == "claude-haiku-5-5"
    assert cfg.presets == [("kill_grace_turns", 0.0), ("handoff_pct", 85.0)]


def test_parse_preset_rejects_bad_shapes():
    import argparse

    assert parse_preset(" top_k = 15 ") == ("top_k", 15.0)
    for bad in ("top_k", "=5", "top_k=many"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_preset(bad)


async def test_run_with_supervisors_signals_the_cron_when_the_population_ends():
    seen: list[str] = []

    class FakeCron:
        async def run_until(self, done: asyncio.Event) -> list[RoundResult]:
            seen.append("cron started")
            await done.wait()
            seen.append("population done")
            return [RoundResult(number=1, final=True)]

    async def population() -> list[str]:
        await asyncio.sleep(0)
        seen.append("population finished")
        return ["sol-001"]

    results, rounds = await run_with_supervisors(population(), FakeCron())
    assert results == ["sol-001"] and [r.number for r in rounds] == [1]
    assert seen == ["cron started", "population finished", "population done"]


async def test_run_with_supervisors_without_a_cron_returns_no_rounds():
    async def population() -> list[str]:
        return ["sol-001"]

    assert await run_with_supervisors(population(), None) == (["sol-001"], [])


async def test_run_with_supervisors_releases_the_cron_even_when_the_population_raises():
    released = asyncio.Event()

    class FakeCron:
        async def run_until(self, done: asyncio.Event) -> list[RoundResult]:
            await done.wait()
            released.set()
            return []

    async def population() -> list[str]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await run_with_supervisors(population(), FakeCron())
    assert released.is_set()


async def test_amain_wires_the_builders_together(monkeypatch, tmp_path):
    # The M4 refactor of main.py dropped build_runner and no test called amain, so the first live
    # run was the one that found it. This runs amain end to end with fakes in place of the database
    # and the model, which is enough to catch a name that does not resolve.
    from harness.orchestrator import main as m
    from harness.orchestrator.monitor import StaticKnobs
    from harness.orchestrator.prompts import PromptAssembler
    from harness.orchestrator.spawner import Spawner
    from harness.tests.orchestrator_fakes import FakeClient, FakeOperator, assistant, client_factory, init_message, result

    from harness.tests.control_fixtures import write_control_dir

    op = FakeOperator()
    control = write_control_dir(tmp_path)
    # --solvers 1 is an override; civ.toml says 3 and must lose to it.
    cfg = m.RunConfig(solvers=1, stagger_s=0, root=m.DEFAULT_ROOT, supervisors=0, control_dir=control,
                      overrides={"solvers", "supervisors"})
    clients = [FakeClient([init_message(), assistant(100), result(num_turns=1, text="done")])]
    spawner = Spawner(op, cfg.board_url, tmp_path, PromptAssembler(m.DEFAULT_ROOT / "harness" / "prompts"),
                      client_factory=client_factory(clients), knobs=StaticKnobs())
    monkeypatch.setattr(m, "build_operator", lambda cfg: (op, StaticKnobs()))
    monkeypatch.setattr(m, "build_spawner", lambda cfg, operator=None, knobs=None, governor=None: (spawner, op))

    results, rounds = await m.amain(cfg)

    assert len(results) == 1 and results[0].ended_by == "natural" and rounds == []
    assert [a["id"] for a in op.registered] == ["sol-001"]
    # The run's shape went to control_state with the override folded in, and the knobs were seeded.
    assert op.control["shape"]["population"]["solvers"] == 1 and "written_at" in op.control["shape"]
    assert ("handoff_pct", 80.0, "civ.toml") in op.presets and ("min_sample", 3.0, "civ.toml") in op.presets


def test_shaped_fields_follow_flag_then_file_then_default(tmp_path, monkeypatch):
    from harness.orchestrator.shape import load_shape
    from harness.tests.control_fixtures import write_control_dir

    for name in ("CIV_SOLVERS", "CIV_MODEL", "CIV_SUPERVISORS", "CIV_SUPERVISOR_INTERVAL_S", "CIV_MAX_GENERATIONS"):
        monkeypatch.delenv(name, raising=False)
    control = write_control_dir(tmp_path)
    shape = load_shape(control / "civ.toml")

    # Nothing on the command line: the file fills every shaped field and ceilings.toml the model.
    cfg = RunConfig.from_args(["--control-dir", str(control)])
    assert cfg.overrides == set() and cfg.control_path() == control.resolve()
    resolved = cfg.resolved(shape, "claude-haiku-5-5")
    assert (resolved.solvers, resolved.supervisors, resolved.supervisor_interval_s, resolved.max_generations) == (3, 1, 600.0, 5)
    assert resolved.model == "claude-haiku-5-5"

    # Flags win over the file, field by field.
    cfg = RunConfig.from_args(["--solvers", "5", "--model", "claude-opus-5-5", "--control-dir", str(control)])
    assert cfg.overrides == {"solvers", "model"}
    resolved = cfg.resolved(shape, "claude-haiku-5-5")
    assert resolved.solvers == 5 and resolved.model == "claude-opus-5-5" and resolved.supervisors == 1
    effective = resolved.effective_shape(shape)
    assert effective.solvers == 5 and effective.supervisors == 1 and effective.knob_seeds()["kill_pct"] == 90.0

    # The environment counts as the command line.
    monkeypatch.setenv("CIV_SUPERVISORS", "2")
    cfg = RunConfig.from_args([])
    assert "supervisors" in cfg.overrides and cfg.resolved(shape, None).supervisors == 2


def test_resume_flag_and_missing_shape_fall_back(tmp_path, caplog):
    from harness.orchestrator import main as m

    cfg = RunConfig.from_args(["--resume", "--control-dir", str(tmp_path / "nowhere")])
    assert cfg.resume is True
    # No civ.toml: the orchestrator's own defaults, with a warning, so a bare checkout still runs.
    with caplog.at_level("WARNING"):
        shape = m.load_shape_for(cfg)
    assert shape.solvers == 3 and "no civ.toml" in caplog.text
    # No ceilings.toml is an error at start, not a default.
    with pytest.raises(FileNotFoundError):
        m.build_governor(cfg, FakeOperatorStub())


class FakeOperatorStub:
    def control_get(self, key):
        return None

    def log_event(self, *a, **k):
        pass
