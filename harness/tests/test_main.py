import asyncio

from harness.orchestrator.main import RunConfig, format_summary, run_population
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
