"""Run N solver lineages against the board and print what they cost and did.

    python -m harness.orchestrator.main --solvers 3 --model claude-opus-5-5 \
        --max-turns 40 --stagger 2 --board-url http://127.0.0.1:8765

Each solver slot is a lineage: a session runs until its context nears the
handoff threshold, hands off, and a successor continues in the same directory
(M3). Supervisors (M4) and the governor (M5) hang off the same loop later.

Cheap succession testing: --context-window 40000 makes the thresholds fire
after a few turns, and --force-handoff-after-turns 3 fires the handoff at a
turn count regardless of fill.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import SpawnResult, Spawner
from harness.orchestrator.succession import LineageResult, LineageRunner

log = logging.getLogger("civ.orchestrator")

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
HANDOFF_REQUEST_FILE = "handoff_request.md"


@dataclass
class RunConfig:
    solvers: int = 3
    model: str = "claude-opus-5-5"
    max_turns: int = 40
    stagger_s: float = 2.0
    board_url: str = "http://127.0.0.1:8765"
    db_url: str = "postgresql://127.0.0.1:5432/civ"
    root: Path = DEFAULT_ROOT
    context_window: int = 1_000_000
    force_handoff_after_turns: int | None = None
    max_generations: int = 5

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "RunConfig":
        env = os.environ
        p = argparse.ArgumentParser(description="civ orchestrator (M3)")
        p.add_argument("--solvers", type=int, default=int(env.get("CIV_SOLVERS", 3)))
        p.add_argument("--model", default=env.get("CIV_MODEL", "claude-opus-5-5"))
        p.add_argument("--max-turns", type=int, default=int(env.get("CIV_MAX_TURNS", 40)))
        p.add_argument("--stagger", type=float, default=float(env.get("CIV_STAGGER_S", 2)))
        p.add_argument("--board-url", default=env.get("CIV_BOARD_URL", "http://127.0.0.1:8765"))
        p.add_argument("--db-url", default=env.get("CIV_DATABASE_URL", "postgresql://127.0.0.1:5432/civ"))
        p.add_argument("--root", type=Path, default=Path(env.get("CIV_ROOT", DEFAULT_ROOT)))
        p.add_argument("--context-window", type=int, default=int(env.get("CIV_CONTEXT_WINDOW", 1_000_000)),
                       help="tokens the fill percentage is measured against; small values force succession cheaply")
        p.add_argument("--force-handoff-after-turns", type=int, default=None,
                       help="test only: fire the handoff at this turn regardless of context fill")
        p.add_argument("--max-generations", type=int, default=int(env.get("CIV_MAX_GENERATIONS", 5)))
        a = p.parse_args(argv)
        return cls(a.solvers, a.model, a.max_turns, a.stagger, a.board_url, a.db_url, a.root,
                   a.context_window, a.force_handoff_after_turns, a.max_generations)


RunResult = SpawnResult | LineageResult
SpawnFn = Callable[[str, str], Awaitable[Any]]


async def run_population(count: int, stagger_s: float, spawn: SpawnFn, sleep=asyncio.sleep) -> list[Any]:
    """Start `count` lineages `stagger_s` apart and wait for all of them.

    Starts are staggered so a population does not hit the board and the
    model in the same second. Each lineage runs concurrently once started.
    """
    tasks: list[asyncio.Task[Any]] = []
    for i in range(1, count + 1):
        agent_id = f"sol-{i:03d}"
        lineage_id = f"L-{i:03d}"
        # By keyword, because a spawner and a lineage runner take the two ids in
        # different positional orders and a swap here once ran a whole demo with
        # agents called L-001 in lineage sol-001.
        tasks.append(asyncio.create_task(spawn(agent_id=agent_id, lineage_id=lineage_id)))
        if i < count and stagger_s > 0:
            await sleep(stagger_s)
    return list(await asyncio.gather(*tasks))


def _row(r: RunResult) -> dict[str, Any]:
    # A bare SpawnResult (one session, no runner) and a LineageResult print the same way.
    gens = r.generations if isinstance(r, LineageResult) else 1
    handoffs = r.handoffs if isinstance(r, LineageResult) else 0
    note = r.error or (r.result or "").strip().replace("\n", " ")[:60]
    return {
        "agent": r.agent_id, "gens": gens, "handoffs": handoffs, "turns": r.turns,
        "cost": r.cost_usd or 0.0, "estimated": bool(getattr(r, "cost_estimated", False)),
        "tok_in": r.tokens_in, "tok_out": r.tokens_out, "ctx": r.context_pct, "note": note,
    }


def _cost_cell(cost: float, estimated: bool) -> str:
    # A tilde marks a cost priced from token counts rather than reported by the SDK.
    return f"~{cost:.4f}" if estimated else f"{cost:.4f}"


def format_summary(results: list[RunResult], tasks_summary: dict | None = None) -> str:
    rows = [f"{'agent':<9} {'gens':>4} {'hoffs':>5} {'turns':>5} {'cost $':>8} {'tok in':>10} {'tok out':>8} {'ctx %':>6}  note"]
    total = 0.0
    any_estimated = False
    for r in results:
        d = _row(r)
        total += d["cost"]
        any_estimated = any_estimated or d["estimated"]
        rows.append(
            f"{d['agent']:<9} {d['gens']:>4} {d['handoffs']:>5} {d['turns']:>5} {_cost_cell(d['cost'], d['estimated']):>8} "
            f"{d['tok_in']:>10} {d['tok_out']:>8} {d['ctx']:>6.2f}  {d['note']}"
        )
    rows.append(f"{'total cost':<26} {_cost_cell(total, any_estimated):>8}")
    if tasks_summary:
        rows.append("tasks: " + ", ".join(f"{k}={v}" for k, v in tasks_summary.items()))
    return "\n".join(rows)


def build_spawner(cfg: RunConfig) -> tuple[Spawner, object]:
    # Imported here so the unit tests of this module never need psycopg or
    # the board package to import.
    from harness.board_mcp.db import Db
    from harness.board_mcp.limits import Knobs
    from harness.board_mcp.operator import Operator

    db = Db(cfg.db_url)
    operator = Operator(db)
    prompts = PromptAssembler(cfg.root / "harness" / "prompts")
    spawner = Spawner(
        operator, cfg.board_url, cfg.root, prompts, model=cfg.model, max_turns=cfg.max_turns,
        knobs=Knobs(db), context_window=cfg.context_window, force_handoff_after_turns=cfg.force_handoff_after_turns,
    )
    return spawner, operator


def build_runner(cfg: RunConfig, spawner: Spawner, operator: Any) -> LineageRunner:
    request = (cfg.root / "harness" / "prompts" / HANDOFF_REQUEST_FILE).read_text(encoding="utf-8")
    return LineageRunner(spawner, operator, request, max_generations=cfg.max_generations)


async def amain(cfg: RunConfig) -> list[RunResult]:
    spawner, operator = build_spawner(cfg)
    runner = build_runner(cfg, spawner, operator)
    log.info("spawning %d solver lineages against %s with %s (window %d, max generations %d)",
             cfg.solvers, cfg.board_url, cfg.model, cfg.context_window, cfg.max_generations)
    results = await run_population(cfg.solvers, cfg.stagger_s, runner.run)
    summary = None
    try:
        summary = operator.tasks_summary()
    except Exception as exc:  # noqa: BLE001 - the summary is a nicety, the results are the output
        log.warning("tasks summary unavailable: %s", exc)
    print(format_summary(results, summary))
    return results


def _failed(r: RunResult) -> bool:
    if isinstance(r, LineageResult):
        return bool(r.error) or any(s.error for s in r.sessions)
    return bool(r.error)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("CIV_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = RunConfig.from_args(argv)
    results = asyncio.run(amain(cfg))
    return 1 if any(_failed(r) for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
