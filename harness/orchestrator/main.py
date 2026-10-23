"""Run N solver lineages against the board and print what they cost and did.

    python -m harness.orchestrator.main --solvers 3 --model claude-opus-5-5 \
        --max-turns 40 --stagger 2 --board-url http://127.0.0.1:8765

Each solver slot is a lineage: a session runs until its context nears the
handoff threshold, hands off, and a successor continues in the same directory
(M3). With --supervisors N, N supervisor sessions run every --supervisor-interval
seconds alongside the solvers and once more after they finish (M4). The
operator's controls (M5) live under --control-dir (default control/): civ.toml
gives the run its shape (flags still win over it), ceilings.toml bounds every
spawn through the governor, and --resume continues the lineages a `civ restore`
left mid-flight.

Cheap succession testing: --context-window 40000 makes the thresholds fire
after a few turns, and --force-handoff-after-turns 3 fires the handoff at a
turn count regardless of fill. --preset knob=value moves a knob before any
spawn, which is how a fault is injected for the supervisors to find.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness.orchestrator.chaos import ChaosReader
from harness.orchestrator.heartbeat import Heartbeat
from harness.orchestrator.governor import Governor
from harness.orchestrator.merger import Merger, merge_settings
from harness.orchestrator.population import Population
from harness.orchestrator.reload import BoardServer, VersionWatcher
from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.shape import Shape, ShapeReader, load_shape
from harness.orchestrator.spawner import SpawnResult, Spawner
from harness.orchestrator.succession import LineageResult, LineageRunner
from harness.orchestrator.supervisors import RoundResult, SupervisorCron, SupervisorRound, format_rounds

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
    # Supervisors (M4). Zero means no rounds at all, so a plain solver demo is unchanged.
    supervisors: int = 0
    supervisor_interval_s: float = 600.0
    supervisor_model: str | None = None
    supervisor_max_turns: int = 15
    presets: list[tuple[str, float]] = field(default_factory=list)
    # The operator's controls (M5).
    control_dir: Path | None = None
    resume: bool = False
    # Self-modification (M6): own the board server so a version bump can restart it, and run
    # the merge gate alongside the population. The merger needs a git checkout at the root.
    serve_board: bool = False
    merger: bool = True
    # A crowd, over time (M7): the task pack to run (CIV_PACK for this process and the board
    # server it owns), and the heartbeat interval (civ.toml [cadence] heartbeat_s unless given).
    pack: Path | None = None
    heartbeat_s: float | None = None
    # Fields the command line (or its environment) set explicitly. Anything else that civ.toml
    # also describes is taken from the file, so a flag always wins over the shape.
    overrides: set[str] = field(default_factory=set)

    @property
    def supervisor_model_id(self) -> str:
        return self.supervisor_model or self.model

    def control_path(self) -> Path:
        return (self.control_dir or self.root / "control").resolve()

    def resolved(self, shape: Shape, default_model: str | None) -> "RunConfig":
        """A copy with civ.toml's values filled in wherever the command line said nothing.

        Precedence is flag, then the shape, then the dataclass default (which is what the
        parser already put in place); the model's second tier is ceilings.toml."""
        values = {
            "solvers": shape.solvers, "supervisors": shape.supervisors,
            "supervisor_interval_s": shape.supervisor_round_s, "max_generations": shape.max_generations,
            "model": default_model,
        }
        updates = {name: value for name, value in values.items() if name not in self.overrides and value is not None}
        return replace(self, **updates)

    def effective_shape(self, shape: Shape) -> Shape:
        """The shape the run is actually using: the file's, with the command line's overrides
        folded in, because this is what is written to control_state and what the population
        and the cron steer by."""
        return shape.with_overrides(
            solvers=self.solvers, supervisors=self.supervisors,
            supervisor_round_s=self.supervisor_interval_s, max_generations=self.max_generations,
        )

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "RunConfig":
        env = os.environ
        p = argparse.ArgumentParser(description="civ orchestrator (M5)")
        # The shaped fields default to None here: civ.toml fills them unless a flag or the
        # environment set them, which is the precedence the contract asks for.
        p.add_argument("--solvers", type=int, default=_env_int(env, "CIV_SOLVERS"))
        p.add_argument("--model", default=env.get("CIV_MODEL") or None)
        p.add_argument("--max-turns", type=int, default=int(env.get("CIV_MAX_TURNS", 40)))
        p.add_argument("--stagger", type=float, default=float(env.get("CIV_STAGGER_S", 2)))
        p.add_argument("--board-url", default=env.get("CIV_BOARD_URL", "http://127.0.0.1:8765"))
        p.add_argument("--db-url", default=env.get("CIV_DATABASE_URL", "postgresql://127.0.0.1:5432/civ"))
        p.add_argument("--root", type=Path, default=Path(env.get("CIV_ROOT", DEFAULT_ROOT)))
        p.add_argument("--context-window", type=int, default=int(env.get("CIV_CONTEXT_WINDOW", 1_000_000)),
                       help="tokens the fill percentage is measured against; small values force succession cheaply")
        p.add_argument("--force-handoff-after-turns", type=int, default=None,
                       help="test only: fire the handoff at this turn regardless of context fill")
        p.add_argument("--max-generations", type=int, default=_env_int(env, "CIV_MAX_GENERATIONS"))
        p.add_argument("--supervisors", type=int, default=_env_int(env, "CIV_SUPERVISORS"),
                       help="supervisor sessions per round; 0 runs no rounds")
        p.add_argument("--supervisor-interval", type=float, default=_env_float(env, "CIV_SUPERVISOR_INTERVAL_S"),
                       help="seconds between supervisor rounds")
        p.add_argument("--supervisor-model", default=env.get("CIV_SUPERVISOR_MODEL") or None,
                       help="model for supervisor sessions; defaults to --model")
        p.add_argument("--supervisor-max-turns", type=int, default=int(env.get("CIV_SUPERVISOR_MAX_TURNS", 15)))
        p.add_argument("--preset", action="append", default=[], metavar="KNOB=VALUE",
                       help="set a config knob before any spawn (repeatable); the operator's fault injection")
        p.add_argument("--control-dir", type=Path, default=Path(env["CIV_CONTROL_DIR"]) if env.get("CIV_CONTROL_DIR") else None,
                       help="where ceilings.toml and civ.toml live; default <root>/control")
        p.add_argument("--resume", action="store_true",
                       help="continue the lineages a civ restore left mid-flight before starting new ones")
        p.add_argument("--serve-board", action="store_true",
                       help="start and own the board server, so a harness version bump restarts it")
        p.add_argument("--no-merger", dest="merger", action="store_false",
                       help="do not run the merge gate alongside the population")
        p.add_argument("--pack", type=Path, default=Path(env["CIV_PACK"]) if env.get("CIV_PACK") else None,
                       help="a generated task pack directory (materials/, tasks.json, answers.json); default the repo's task/")
        p.add_argument("--heartbeat-s", type=float, default=_env_float(env, "CIV_HEARTBEAT_S"),
                       help="seconds between heartbeat rows; default civ.toml [cadence] heartbeat_s, else 30")
        a = p.parse_args(argv)
        given = {"solvers": a.solvers, "supervisors": a.supervisors, "supervisor_interval_s": a.supervisor_interval,
                 "max_generations": a.max_generations, "model": a.model}
        overrides = {name for name, value in given.items() if value is not None}
        defaults = cls()
        return cls(
            solvers=a.solvers if a.solvers is not None else defaults.solvers,
            model=a.model or defaults.model,
            max_turns=a.max_turns, stagger_s=a.stagger, board_url=a.board_url, db_url=a.db_url, root=a.root,
            context_window=a.context_window, force_handoff_after_turns=a.force_handoff_after_turns,
            max_generations=a.max_generations if a.max_generations is not None else defaults.max_generations,
            supervisors=a.supervisors if a.supervisors is not None else defaults.supervisors,
            supervisor_interval_s=a.supervisor_interval if a.supervisor_interval is not None else defaults.supervisor_interval_s,
            supervisor_model=a.supervisor_model, supervisor_max_turns=a.supervisor_max_turns,
            presets=[parse_preset(text) for text in a.preset],
            control_dir=a.control_dir, resume=a.resume, overrides=overrides,
            serve_board=a.serve_board, merger=a.merger,
            pack=a.pack, heartbeat_s=a.heartbeat_s,
        )


def _env_int(env, name: str) -> int | None:
    return int(env[name]) if env.get(name) else None


def _env_float(env, name: str) -> float | None:
    return float(env[name]) if env.get(name) else None


def parse_preset(text: str) -> tuple[str, float]:
    """'knob=value' into (knob, float); the bounds check belongs to the operator write."""
    knob, sep, value = text.partition("=")
    if not sep or not knob.strip():
        raise argparse.ArgumentTypeError(f"preset must look like knob=value, got {text!r}")
    try:
        return knob.strip(), float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"preset value for {knob.strip()} is not a number: {value!r}") from exc


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


def build_operator(cfg: RunConfig) -> tuple[Any, Any]:
    """(operator, knobs) over the database. Imported here so the unit tests of this
    module never need psycopg or the board package to import."""
    from harness.board_mcp.db import Db
    from harness.board_mcp.limits import Knobs
    from harness.board_mcp.operator import Operator

    db = Db(cfg.db_url)
    return Operator(db), Knobs(db)


def build_spawner(cfg: RunConfig, operator: Any = None, knobs: Any = None, governor: Any = None) -> tuple[Spawner, Any]:
    if operator is None:
        operator, knobs = build_operator(cfg)
    prompts = PromptAssembler(cfg.root / "harness" / "prompts")
    spawner = Spawner(
        operator, cfg.board_url, cfg.root, prompts, model=cfg.model, max_turns=cfg.max_turns,
        knobs=knobs, context_window=cfg.context_window, force_handoff_after_turns=cfg.force_handoff_after_turns,
        governor=governor,
    )
    return spawner, operator


def build_runner(cfg: RunConfig, spawner: Spawner, operator: Any) -> LineageRunner:
    request = (cfg.root / "harness" / "prompts" / HANDOFF_REQUEST_FILE).read_text(encoding="utf-8")
    return LineageRunner(spawner, operator, request, max_generations=cfg.max_generations)


def build_supervisor_cron(cfg: RunConfig, operator: Any, knobs: Any, governor: Any = None,
                          shape_reader: ShapeReader | None = None) -> SupervisorCron:
    """A second spawner for the supervisors, because they may run a different
    model and always run a different turn budget, and the spawner owns both."""
    prompts = PromptAssembler(cfg.root / "harness" / "prompts")
    spawner = Spawner(operator, cfg.board_url, cfg.root, prompts, model=cfg.supervisor_model_id,
                      max_turns=cfg.supervisor_max_turns, knobs=knobs, context_window=cfg.context_window,
                      governor=governor)
    first_number = int(getattr(operator, "last_round_number", lambda: 0)()) + 1
    return SupervisorCron(SupervisorRound(spawner, operator, cfg.supervisors, shape_reader=shape_reader),
                          cfg.supervisor_interval_s, shape_reader=shape_reader, first_number=first_number)


def load_shape_for(cfg: RunConfig) -> Shape:
    """control/civ.toml, or the orchestrator's own defaults with a warning when there is none,
    so a checkout without a control directory still runs the small demo."""
    path = cfg.control_path() / "civ.toml"
    if not path.exists():
        log.warning("no civ.toml at %s; running on the orchestrator's defaults", path)
        return Shape()
    return load_shape(path)


def build_governor(cfg: RunConfig, operator: Any) -> Governor:
    # A missing ceilings file raises here, at start: running without ceilings is the one thing
    # the design says never to do, and the error names the path to create.
    return Governor(operator, cfg.control_path() / "ceilings.toml")


def record_shape(operator: Any, shape: Shape) -> None:
    """Write the run's shape to control_state (so `civ status` can show it and the population can
    steer by it) and seed the knob-valued keys into the config table as operator presets."""
    operator.control_set("shape", {**shape.to_json(), "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    for knob, value in shape.knob_seeds().items():
        operator.set_knob(knob, value, "civ.toml")


def apply_presets(cfg: RunConfig, operator: Any) -> None:
    # After record_shape, so a --preset wins over civ.toml the way a flag wins over it.
    for knob, value in cfg.presets:
        operator.set_knob(knob, value, "preset from the command line")
        log.info("preset %s = %s", knob, value)


Loop = Callable[[asyncio.Event], Awaitable[Any]]


async def run_everything(population: Awaitable[list[Any]], *loops: Loop | None, after: Loop | None = None) -> tuple[list[Any], list[Any]]:
    """Run the solver population with any number of background loops (the supervisor cron, the
    merger, the version watcher), each an `async run_until(done)`.

    Every loop is told when the population is done through one event. A population that raises
    still sets it and still waits for the loops, because the final supervisor round is the one
    that reads the window in which things went wrong, and an unawaited loop would leave CLI
    processes or a worktree behind. Returns the population's results and each loop's result in
    the order the loops were given (None for a loop that was None).
    """
    done = asyncio.Event()
    tasks = [asyncio.create_task(loop(done)) if loop is not None else None for loop in loops]
    # `after` is told to finish only once every other loop has settled. The merger runs there,
    # because the final supervisor round can queue a merge and a merger that drained on the
    # population's end leaves it queued (the sixth soak did exactly that).
    later = asyncio.Event()
    after_task = asyncio.create_task(after(later)) if after is not None else None

    async def settle() -> list[Any]:
        out: list[Any] = []
        for task in tasks:
            if task is None:
                out.append(None)
                continue
            try:
                out.append(await task)
            except Exception as exc:  # noqa: BLE001 - the population's result is the one to report
                log.warning("background loop failed: %s", exc)
                out.append(None)
        return out

    async def settle_after() -> Any:
        later.set()
        if after_task is None:
            return None
        try:
            return await after_task
        except Exception as exc:  # noqa: BLE001 - as above
            log.warning("background loop failed: %s", exc)
            return None

    try:
        results = await population
    except BaseException:
        done.set()
        await settle()
        await settle_after()
        raise
    done.set()
    first = await settle()
    after_result = await settle_after()
    # The trailing slot exists only when an `after` loop was given, so callers that pass none
    # still unpack exactly one result per loop.
    return results, first + ([after_result] if after is not None else [])


async def run_with_supervisors(population: Awaitable[list[Any]], cron: SupervisorCron | None) -> tuple[list[Any], list[RoundResult]]:
    """The M4 shape of run_everything: the population and the cron, nothing else."""
    results, (rounds,) = await run_everything(population, cron.run_until if cron is not None else None)
    return results, list(rounds or [])


def build_board_server(cfg: RunConfig) -> BoardServer:
    """The board server as a child of the orchestrator, for --serve-board."""
    from urllib.parse import urlparse

    parsed = urlparse(cfg.board_url)
    env = {
        "CIV_DATABASE_URL": cfg.db_url,
        "CIV_BOARD_HOST": parsed.hostname or "127.0.0.1",
        "CIV_BOARD_PORT": str(parsed.port or 8765),
        "CIV_ROOT": str(cfg.root),
        "CIV_CONTROL_DIR": str(cfg.control_path()),
    }
    if cfg.pack is not None:
        # The verifier inside the server scores against the pack's answers, so it must see the same pack.
        env["CIV_PACK"] = str(cfg.pack.resolve())
    cmd = [sys.executable, "-m", "harness.board_mcp.server"]
    return BoardServer(cmd, env, f"{cfg.board_url.rstrip('/')}/health", log_path=cfg.root / "shared" / "board-server.log")


def build_merger(cfg: RunConfig, operator: Any) -> Merger | None:
    """The merge gate, when the root is a git checkout and the flag did not turn it off."""
    if not cfg.merger or not (cfg.root / ".git").exists():
        return None
    settings = merge_settings(operator.control_get("shape"))
    return Merger(operator, cfg.root, settings["test_command"], settings["protected_paths"])


def build_version_watcher(cfg: RunConfig, operator: Any, board: BoardServer | None) -> Callable[[asyncio.Event], Awaitable[int]]:
    """A loop that logs every harness version bump and restarts the board server when we own it."""
    watcher = VersionWatcher(cfg.root / "harness" / "VERSION")

    async def on_bump(old: str, new: str) -> None:
        operator.log_event(None, "harness.version", {"from": old, "to": new, "board_restarted": board is not None})
        log.info("harness version %s -> %s", old, new)
        if board is not None:
            # Off the loop thread: start() blocks on the health probe.
            await asyncio.to_thread(board.restart)

    async def run_until(done: asyncio.Event) -> int:
        return await watcher.run_until(done, on_bump)

    return run_until


def apply_pack(cfg: RunConfig) -> None:
    """Point this process at the pack: the spawner reads CIV_PACK for the materials directory and
    the owned board server inherits it for the verifier."""
    if cfg.pack is not None:
        os.environ["CIV_PACK"] = str(cfg.pack.resolve())


def build_heartbeat(cfg: RunConfig, operator: Any, shape: Shape, spawner: Spawner | None = None) -> Heartbeat:
    interval = cfg.heartbeat_s if cfg.heartbeat_s is not None else float(getattr(shape, "heartbeat_s", 30))
    version = spawner.harness_version if spawner is not None else "dev"
    return Heartbeat(operator, interval, pid=os.getpid(), harness_version=version)


def record_argv(control_dir: Path, argv: list[str]) -> Path:
    """Write this run's command line to control/orchestrator.args, one argument per line, so the
    watchdog can relaunch the orchestrator exactly as the operator started it (with --resume
    appended). A deviation worth naming: control/ is the operator's directory, and this is the
    one file in it the orchestrator writes."""
    path = control_dir / "orchestrator.args"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{arg}\n" for arg in argv), encoding="utf-8")
    return path


async def amain(cfg: RunConfig) -> tuple[list[RunResult], list[RoundResult]]:
    apply_pack(cfg)
    operator, knobs = build_operator(cfg)
    governor = build_governor(cfg, operator)
    file_shape = load_shape_for(cfg)
    cfg = cfg.resolved(file_shape, governor.ceilings.model)
    shape = cfg.effective_shape(file_shape)
    record_shape(operator, shape)
    apply_presets(cfg, operator)
    reader = ShapeReader(operator, shape)
    board = build_board_server(cfg) if cfg.serve_board else None
    if board is not None:
        # Before any spawn, because a session whose MCP server is not up fails its init.
        await asyncio.to_thread(board.start)
    try:
        spawner, _ = build_spawner(cfg, operator, knobs, governor)
        runner = build_runner(cfg, spawner, operator)
        resume_rows = list(operator.lineages_to_resume() or []) if cfg.resume else []
        population = Population(runner, reader, cfg.stagger_s, resume_rows=resume_rows)
        runner.set_slot_open(population.slot_open)
        cron = build_supervisor_cron(cfg, operator, knobs, governor, reader) if cfg.supervisors > 0 else None
        merger = build_merger(cfg, operator)
        watcher = build_version_watcher(cfg, operator, board)
        heartbeat = build_heartbeat(cfg, operator, shape, spawner)
        # The chaos reader is polled by the runner's own watchers, so it needs no loop of its own.
        runner.set_chaos(ChaosReader(operator))
        log.info("spawning %d solver lineages against %s with %s (window %d, max generations %d), %d supervisors every %.0fs%s%s",
                 cfg.solvers, cfg.board_url, cfg.model, cfg.context_window, cfg.max_generations,
                 cfg.supervisors, cfg.supervisor_interval_s,
                 f", resuming {len(resume_rows)} lineage(s)" if resume_rows else "",
                 ", merge gate on" if merger is not None else "")
        results, (rounds, _bumps, _beats, merges) = await run_everything(
            population.run(),
            cron.run_until if cron is not None else None,
            watcher,
            heartbeat.run_until,
            after=merger.run_until if merger is not None else None,
        )
        rounds = list(rounds or [])
        for outcome in merges or []:
            print(f"merge #{outcome.merge_id}: {outcome.status} {outcome.detail.splitlines()[0] if outcome.detail else ''}")
    finally:
        if board is not None:
            await asyncio.to_thread(board.stop)
    summary = None
    try:
        summary = operator.tasks_summary()
    except Exception as exc:  # noqa: BLE001 - the summary is a nicety, the results are the output
        log.warning("tasks summary unavailable: %s", exc)
    print(format_summary(results, summary))
    if rounds:
        print(format_rounds(rounds))
    return results, rounds


def _failed(r: RunResult) -> bool:
    if isinstance(r, LineageResult):
        return bool(r.error) or any(s.error for s in r.sessions)
    return bool(r.error)


def _halted(r: RunResult) -> bool:
    return isinstance(r, LineageResult) and r.ended_by == "halted"


def exit_code(results: list[RunResult], rounds: list[RoundResult]) -> int:
    """2 when the governor halted a lineage on the spend ceiling, 1 on any error, else 0."""
    if any(_halted(r) for r in results):
        return 2
    return 1 if any(_failed(r) for r in results) or any(r.failed for r in rounds) else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("CIV_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = RunConfig.from_args(argv)
    try:
        record_argv(cfg.control_path(), list(argv) if argv is not None else sys.argv[1:])
    except OSError as exc:
        log.warning("could not write control/orchestrator.args: %s", exc)
    results, rounds = asyncio.run(amain(cfg))
    halted = [r for r in results if _halted(r)]
    if halted:
        print(f"halted by the governor: {halted[0].halted}")
    return exit_code(results, rounds)


if __name__ == "__main__":
    raise SystemExit(main())
