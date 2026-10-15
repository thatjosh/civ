"""Run N solvers against the board and print what they cost and did.

    python -m harness.orchestrator.main --solvers 3 --model claude-opus-5-5 \
        --max-turns 40 --stagger 2 --board-url http://127.0.0.1:8765

This is the M2 shape of the orchestrator: spawn, wait, summarise. Succession
(M3), supervisors (M4) and the governor (M5) hang off the same loop later.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import SpawnResult, Spawner

log = logging.getLogger("civ.orchestrator")

DEFAULT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class RunConfig:
    solvers: int = 3
    model: str = "claude-opus-5-5"
    max_turns: int = 40
    stagger_s: float = 2.0
    board_url: str = "http://127.0.0.1:8765"
    db_url: str = "postgresql://127.0.0.1:5432/civ"
    root: Path = DEFAULT_ROOT

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "RunConfig":
        env = os.environ
        p = argparse.ArgumentParser(description="civ orchestrator (M2)")
        p.add_argument("--solvers", type=int, default=int(env.get("CIV_SOLVERS", 3)))
        p.add_argument("--model", default=env.get("CIV_MODEL", "claude-opus-5-5"))
        p.add_argument("--max-turns", type=int, default=int(env.get("CIV_MAX_TURNS", 40)))
        p.add_argument("--stagger", type=float, default=float(env.get("CIV_STAGGER_S", 2)))
        p.add_argument("--board-url", default=env.get("CIV_BOARD_URL", "http://127.0.0.1:8765"))
        p.add_argument("--db-url", default=env.get("CIV_DATABASE_URL", "postgresql://127.0.0.1:5432/civ"))
        p.add_argument("--root", type=Path, default=Path(env.get("CIV_ROOT", DEFAULT_ROOT)))
        a = p.parse_args(argv)
        return cls(a.solvers, a.model, a.max_turns, a.stagger, a.board_url, a.db_url, a.root)


SpawnFn = Callable[[str, str], Awaitable[SpawnResult]]


async def run_population(count: int, stagger_s: float, spawn: SpawnFn, sleep=asyncio.sleep) -> list[SpawnResult]:
    """Start `count` solvers `stagger_s` apart and wait for all of them.

    Spawns are staggered so a population does not hit the board and the
    model in the same second. Each spawn runs concurrently once started.
    """
    tasks: list[asyncio.Task[SpawnResult]] = []
    for i in range(1, count + 1):
        agent_id = f"sol-{i:03d}"
        lineage_id = f"L-{i:03d}"
        tasks.append(asyncio.create_task(spawn(agent_id, lineage_id)))
        if i < count and stagger_s > 0:
            await sleep(stagger_s)
    return list(await asyncio.gather(*tasks))


def format_summary(results: list[SpawnResult], tasks_summary: dict | None = None) -> str:
    rows = [f"{'agent':<9} {'turns':>5} {'cost $':>8} {'tok in':>10} {'tok out':>8} {'ctx %':>6}  note"]
    total = 0.0
    for r in results:
        cost = r.cost_usd or 0.0
        total += cost
        note = r.error or (r.result or "").strip().replace("\n", " ")[:60]
        rows.append(f"{r.agent_id:<9} {r.turns:>5} {cost:>8.4f} {r.tokens_in:>10} {r.tokens_out:>8} {r.context_pct:>6.2f}  {note}")
    rows.append(f"{'total cost':<15} {total:>8.4f}")
    if tasks_summary:
        rows.append("tasks: " + ", ".join(f"{k}={v}" for k, v in tasks_summary.items()))
    return "\n".join(rows)


def build_spawner(cfg: RunConfig) -> tuple[Spawner, object]:
    # Imported here so the unit tests of this module never need psycopg or
    # the board package to import.
    from harness.board_mcp.db import Db
    from harness.board_mcp.operator import Operator

    operator = Operator(Db(cfg.db_url))
    prompts = PromptAssembler(cfg.root / "harness" / "prompts")
    spawner = Spawner(operator, cfg.board_url, cfg.root, prompts, model=cfg.model, max_turns=cfg.max_turns)
    return spawner, operator


async def amain(cfg: RunConfig) -> list[SpawnResult]:
    spawner, operator = build_spawner(cfg)
    log.info("spawning %d solvers against %s with %s", cfg.solvers, cfg.board_url, cfg.model)
    results = await run_population(cfg.solvers, cfg.stagger_s, spawner.spawn)
    summary = None
    try:
        summary = operator.tasks_summary()
    except Exception as exc:  # noqa: BLE001 - the summary is a nicety, the results are the output
        log.warning("tasks summary unavailable: %s", exc)
    print(format_summary(results, summary))
    return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("CIV_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = RunConfig.from_args(argv)
    results = asyncio.run(amain(cfg))
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
