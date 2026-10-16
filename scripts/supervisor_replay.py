"""Replay a recorded run for one supervisor and record what it diagnosed.

A supervisor's value is judgement over data the orchestrator cannot judge for itself: why an
agent failed to hand off, and which knob or prompt is at fault. That judgement can be measured
without running any solvers. Load a recorded run (a pg_dump of the board after it), open a round
whose window covers the whole recording, build the brief the orchestrator would build, run one
supervisor session against it, and keep what it wrote and what it tuned. score_report.py then
scores that output against a rubric written from what really happened in the recording.

    python scripts/supervisor_replay.py --fixture docs/demo/2026-10-15-m3-fixture.sql \\
        --model claude-opus-5-5 --trials 3 [--without-succession-section]

Each trial writes docs/demo/<date>-replay-<model>-<trial>[-control].json. The control arm
(--without-succession-section) blanks the succession block of the brief, so the difference in
score between the two arms is what the orchestrator's incident packet buys over a supervisor
reading the board on its own.

The WS-F (server) and WS-G (orchestrator) modules are imported inside functions, so that
argument parsing and the fixture load work before those modules exist, and a missing method is
reported by name rather than as a traceback.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_FIXTURE = ROOT / "docs" / "demo" / "2026-10-15-m3-fixture.sql"
DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ_replay"
PSQL_CANDIDATES = ["/opt/homebrew/opt/postgresql@17/bin/psql", "psql"]
# These tables are rebuilt by the migration and the seeds, so the fixture's copies must be
# skipped: config would clash with the seeded rows (and lacks knobs added since the recording),
# and schema_migrations is written by migrate.py itself.
SKIP_TABLES = {"public.config", "public.schema_migrations"}
REQUIRED_OPERATOR_METHODS = ("open_round", "round_brief", "close_round", "current_round")
HEALTH_WAIT_S = 20.0


# --- database ------------------------------------------------------------------


def maintenance_url(db_url: str) -> tuple[str, str]:
    """Split a database url into (url of the postgres maintenance db, database name)."""
    base, _, name = db_url.rpartition("/")
    return f"{base}/postgres", name


def ensure_database(db_url: str) -> None:
    import psycopg

    admin_url, name = maintenance_url(db_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(f'create database "{name}"')
    # A scratch database starts empty every time, so a stale replay cannot leak into a trial.
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("drop schema public cascade; create schema public")


def find_psql() -> str:
    for candidate in PSQL_CANDIDATES:
        path = shutil.which(candidate) if not candidate.startswith("/") else (candidate if Path(candidate).exists() else None)
        if path:
            return path
    raise SystemExit("psql not found: install postgresql@17 or put psql on PATH")


def filtered_fixture(fixture: Path, prepend: str = "") -> Path:
    """Write a copy of the dump without the COPY blocks of the tables we rebuild ourselves."""
    out_lines: list[str] = [prepend] if prepend else []
    skipping = False
    for line in fixture.read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            table = line.split()[1]
            skipping = table in SKIP_TABLES
        if not skipping:
            out_lines.append(line)
        elif line.strip() == "\\.":
            # The terminator of a skipped block; the next line is outside it.
            skipping = False
    handle = tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8")
    handle.write("\n".join(out_lines) + "\n")
    handle.close()
    return Path(handle.name)


def load_fixture(db_url: str, fixture: Path) -> None:
    import migrate  # scripts/migrate.py

    migrate.apply(db_url, log=lambda *_: None)
    psql = find_psql()

    def run(path: Path) -> subprocess.CompletedProcess:
        return subprocess.run([psql, "-v", "ON_ERROR_STOP=1", "-q", "-d", db_url, "-f", str(path)],
                              capture_output=True, text=True)

    plain = filtered_fixture(fixture)
    result = run(plain)
    if result.returncode != 0:
        # The dump's rows are in id order so the self-referencing FK on messages should hold row by
        # row; if the server disagrees, load with FK triggers off (superuser only, which the local
        # Homebrew role is).
        relaxed = filtered_fixture(fixture, prepend="set session_replication_role = replica;")
        result = run(relaxed)
        if result.returncode != 0:
            raise SystemExit(f"fixture load failed:\n{result.stderr}")

    import psycopg

    from harness.board_mcp.seed import seed_config

    with psycopg.connect(db_url) as conn:
        seed_config(conn)
        conn.commit()


def fixture_span(db_url: str) -> tuple[dt.datetime, dt.datetime]:
    import psycopg

    with psycopg.connect(db_url) as conn:
        first, last = conn.execute("select min(ts), max(ts) from events").fetchone()
    if first is None:
        raise SystemExit("the fixture has no events; nothing to replay")
    return first, last


# --- board server ----------------------------------------------------------------


def start_server(db_url: str, port: int, log_path: Path) -> subprocess.Popen:
    env = {**os.environ, "CIV_DATABASE_URL": db_url, "CIV_BOARD_PORT": str(port), "CIV_ROOT": str(ROOT)}
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "harness.board_mcp.server"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + HEALTH_WAIT_S
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return proc
        except Exception:  # noqa: BLE001 - not up yet
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    proc.kill()
    raise SystemExit(f"board server did not come up on port {port}; see {log_path}")


# --- one trial ---------------------------------------------------------------------


def check_operator(operator: Any) -> None:
    missing = [m for m in REQUIRED_OPERATOR_METHODS if not callable(getattr(operator, m, None))]
    if missing:
        print(f"Operator is missing M4 methods: {', '.join(missing)} (WS-F); cannot replay", file=sys.stderr)
        raise SystemExit(2)


def open_covering_round(operator: Any, db_url: str, number: int) -> int:
    """Open a round whose window is the whole recording.

    open_round sets window_start to the previous round's start, and in a replay there is no
    previous round, so the window is widened by hand to a minute before the first recorded
    event. The brief is built after that, so it sees every incident in the fixture.
    """
    import psycopg

    round_id = operator.open_round(number=number, final=True)
    first, _ = fixture_span(db_url)
    with psycopg.connect(db_url) as conn:
        conn.execute("update rounds set window_start = %s - interval '1 minute' where id = %s", (first, round_id))
        conn.commit()
    return int(round_id)


def collect(db_url: str, agent_id: str) -> dict[str, Any]:
    import psycopg

    def rows(conn, sql: str) -> list[Any]:
        return [r[0] for r in conn.execute(sql, (agent_id,)).fetchall()]

    with psycopg.connect(db_url) as conn:
        reports = rows(conn, "select body from messages where kind = 'report' and author_id = %s order by id")
        tunes = rows(conn, "select payload from events where type = 'tool.civ_tune' and agent_id = %s order by seq")
        refusals = rows(conn, "select payload from events where type = 'tool.refused' and agent_id = %s order by seq")
        pings = rows(conn, "select payload from events where type = 'report.ping' and agent_id = %s order by seq")
    return {"report": "\n\n---\n\n".join(reports), "tunes": tunes, "refusals": refusals, "pings": pings}


def run_trial(args: argparse.Namespace, trial: int, board_url: str) -> Path:
    from harness.board_mcp.db import Db
    from harness.board_mcp.limits import Knobs
    from harness.board_mcp.operator import Operator
    from harness.orchestrator.prompts import PromptAssembler
    from harness.orchestrator.spawner import Spawner

    try:
        from harness.orchestrator.brief import render_brief
    except ImportError as exc:
        print(f"harness.orchestrator.brief.render_brief is missing (WS-G): {exc}", file=sys.stderr)
        raise SystemExit(2)

    db = Db(args.db_url)
    operator = Operator(db)
    check_operator(operator)

    round_id = open_covering_round(operator, args.db_url, trial)
    brief = dict(operator.round_brief(round_id))
    if args.without_succession_section:
        brief["succession"] = {}
    brief_md = render_brief(brief)

    agent_id = f"sup-001-r{trial}"
    spawner = Spawner(operator, board_url, ROOT, PromptAssembler(ROOT / "harness" / "prompts"),
                      model=args.model, max_turns=args.max_turns, knobs=Knobs(db))
    try:
        result = asyncio.run(spawner.spawn(agent_id=agent_id, lineage_id="S-001", generation=trial,
                                           role="supervisor", brief=brief_md))
    except TypeError as exc:
        print(f"Spawner.spawn does not take role/brief as the M4 contract says (WS-G): {exc}", file=sys.stderr)
        raise SystemExit(2)

    found = collect(args.db_url, agent_id)
    tuned = found["tunes"][-1].get("knob") if found["tunes"] else None
    summary = {"reports": 1 if found["report"] else 0, "pings": len(found["pings"]), "tuned": tuned,
               "reopened": [], "cost_usd": result.cost_usd, "turns": result.turns, "replay": True}
    operator.close_round(round_id, summary)
    db.close()

    record = {
        "model": args.model,
        "trial": trial,
        "control": bool(args.without_succession_section),
        "fixture": str(Path(args.fixture).relative_to(ROOT)) if Path(args.fixture).is_relative_to(ROOT) else str(args.fixture),
        "agent_id": agent_id,
        "round_id": round_id,
        "brief_markdown": brief_md,
        "brief": brief,
        "report": found["report"],
        "tunes": found["tunes"],
        "refusals": found["refusals"],
        "pings": found["pings"],
        "cost_usd": result.cost_usd,
        "cost_estimated": bool(getattr(result, "cost_estimated", False)),
        "turns": result.turns,
        "error": result.error,
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    out = Path(args.out_dir) / f"{dt.date.today():%Y-%m-%d}-replay-{model_short(args.model)}-{trial}{'-control' if args.without_succession_section else ''}.json"
    out.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    return out


def model_short(model: str) -> str:
    # claude-opus-5-5 -> opus, claude-haiku-5-5 -> haiku; anything else keeps its first token.
    return re.sub(r"^claude-", "", model).split("-")[0]


# --- main -----------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--without-succession-section", action="store_true",
                   help="control arm: blank the succession block of the brief")
    p.add_argument("--db-url", default=os.environ.get("CIV_REPLAY_DATABASE_URL", DEFAULT_DB_URL))
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "demo")
    p.add_argument("--max-turns", type=int, default=15)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.fixture.exists():
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 1
    args.out_dir.mkdir(parents=True, exist_ok=True)

    ensure_database(args.db_url)
    load_fixture(args.db_url, args.fixture)
    first, last = fixture_span(args.db_url)
    print(f"replay: fixture {args.fixture.name} covers {first:%H:%M:%S} to {last:%H:%M:%S} on {first:%Y-%m-%d}")

    board_url = f"http://127.0.0.1:{args.port}"
    server_log = args.out_dir / "replay-board-server.log"
    server = start_server(args.db_url, args.port, server_log)
    written: list[Path] = []
    try:
        for trial in range(1, args.trials + 1):
            out = run_trial(args, trial, board_url)
            written.append(out)
            record = json.loads(out.read_text(encoding="utf-8"))
            print(f"replay: trial {trial} -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out} "
                  f"(turns {record['turns']}, cost ${record['cost_usd'] or 0:.4f}, tunes {len(record['tunes'])}, "
                  f"pings {len(record['pings'])})")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        if server_log.exists():
            server_log.unlink()
    print("replay: score each file with  python scripts/score_report.py <file> --rubric docs/eval/m3-kills.rubric.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
