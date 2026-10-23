"""The soak (M7): a crowd, over time, with faults injected by the clock.

    python scripts/soak.py --pack task/packs/soak-20y --minutes 15 --solvers 6 \\
        --model claude-haiku-5-5 --supervisors 1 --supervisor-model claude-opus-5-5 \\
        --supervisor-interval 180 --context-window 100000 \\
        --chaos "kill-solver@120,board-down@300:20,kill-orchestrator@540" \\
        --out docs/demo/2026-10-23-soak

What it does, in order: bootstrap (databases, migrations), reset the board, seed the pack, start
the out-of-stack watchdog with --restart, start the orchestrator with --serve-board (it owns the
board server, so killing the orchestrator kills both, which is the honest shape of the fault),
then run the chaos schedule by wall clock: a solver killed through `civ chaos kill random-solver`,
a board outage through `civ chaos board-down`, and the orchestrator itself killed with SIGKILL,
after which the soak waits for a fresh heartbeat from a new pid and records how long the
civilisation was down. It stops at the deadline or when every task is done, writes a summary
and the events, renders the dashboard, and copies the dashboard to the Desktop.

The soak is read-only on the repository and writes only under --out, backups/, shared/ and
agents/. It does not run the model itself; the orchestrator does.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PG_BIN = "/opt/homebrew/opt/postgresql@17/bin"
DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"
DEFAULT_BOARD_URL = "http://127.0.0.1:8765"
PYTHON = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable
CHAOS_RE = re.compile(r"^(kill-solver|board-down|kill-orchestrator|lag)@(\d+)(?::(\d+))?$")


# --- plan --------------------------------------------------------------------------------------


def parse_chaos(spec: str) -> list[dict[str, Any]]:
    """'kill-solver@120,board-down@300:20' into [{"kind", "at_s", "arg"}], sorted by time."""
    plan: list[dict[str, Any]] = []
    for item in [x.strip() for x in spec.split(",") if x.strip()]:
        m = CHAOS_RE.match(item)
        if not m:
            raise argparse.ArgumentTypeError(f"chaos item {item!r} is not kind@seconds[:arg]")
        kind, at_s, arg = m.group(1), int(m.group(2)), m.group(3)
        if kind in ("board-down", "lag") and arg is None:
            raise argparse.ArgumentTypeError(f"{kind} needs an argument, as in {kind}@300:20")
        plan.append({"kind": kind, "at_s": at_s, "arg": int(arg) if arg is not None else None})
    return sorted(plan, key=lambda x: x["at_s"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pack", required=True, help="a generated pack directory, for example task/packs/soak-20y")
    p.add_argument("--minutes", type=float, default=15.0)
    p.add_argument("--solvers", type=int, default=6)
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--supervisors", type=int, default=1)
    p.add_argument("--supervisor-model", default="claude-opus-5-5")
    p.add_argument("--supervisor-interval", type=int, default=180)
    p.add_argument("--supervisor-max-turns", type=int, default=12)
    p.add_argument("--context-window", type=int, default=100_000)
    # The demo's 40-turn cap ended every lineage of the first soak before any handoff; a soak
    # wants sessions ended by context fill, so the budget is large by default.
    p.add_argument("--max-turns", type=int, default=400)
    p.add_argument("--max-generations", type=int, default=12)
    p.add_argument("--chaos", type=parse_chaos, default=parse_chaos("kill-solver@120,board-down@300:20,kill-orchestrator@540"))
    p.add_argument("--out", default=f"docs/demo/{dt.date.today():%Y-%m-%d}-soak")
    p.add_argument("--db-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    p.add_argument("--board-url", default=os.environ.get("CIV_BOARD_URL", DEFAULT_BOARD_URL))
    p.add_argument("--watchdog-interval", type=int, default=20, help="seconds between watchdog checks")
    p.add_argument("--desktop-copy", default=str(Path.home() / "Desktop" / "civ-soak-dashboard.html"))
    p.add_argument("--no-desktop-copy", action="store_true")
    return p.parse_args(argv)


# --- helpers -----------------------------------------------------------------------------------


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out)
        self.env = {
            **os.environ,
            "PATH": f"{PG_BIN}:{os.environ.get('PATH', '')}",
            "CIV_DATABASE_URL": args.db_url,
            "CIV_BOARD_URL": args.board_url,
            "CIV_ROOT": str(ROOT),
            "CIV_CONTROL_DIR": os.environ.get("CIV_CONTROL_DIR", str(ROOT / "control")),
            "CIV_PACK": str(Path(args.pack).resolve()),
        }
        self.started = time.monotonic()
        self.log_lines: list[str] = []
        self.heartbeat_gaps: list[dict[str, Any]] = []
        self.chaos_done: list[dict[str, Any]] = []
        self.orchestrator: subprocess.Popen | None = None
        self.watchdog: subprocess.Popen | None = None
        self.orchestrator_log = open(f"{self.out}-orchestrator.log", "a", encoding="utf-8")

    # process control

    def log(self, text: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} +{int(time.monotonic() - self.started):4d}s {text}"
        print(line, flush=True)
        self.log_lines.append(line)

    def run(self, *cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(list(cmd), cwd=ROOT, env=self.env, check=check, text=True,
                              capture_output=capture)

    def civ(self, *args: str) -> str:
        out = self.run(PYTHON, "-m", "control.cli.civ", *args, check=False, capture=True)
        text = (out.stdout + out.stderr).strip()
        self.log(f"civ {' '.join(args)}: {text.splitlines()[0] if text else '(no output)'}")
        return text

    def sql(self, query: str) -> str:
        out = self.run(f"{PG_BIN}/psql", self.args.db_url, "-Atc", query, check=False, capture=True)
        return out.stdout.strip()

    def start_orchestrator(self) -> None:
        a = self.args
        cmd = [PYTHON, "-m", "harness.orchestrator.main", "--serve-board", "--pack", str(Path(a.pack).resolve()),
               "--solvers", str(a.solvers), "--model", a.model, "--context-window", str(a.context_window),
               "--max-turns", str(a.max_turns),
               "--max-generations", str(a.max_generations), "--supervisors", str(a.supervisors),
               "--supervisor-model", a.supervisor_model, "--supervisor-interval", str(a.supervisor_interval),
               "--supervisor-max-turns", str(a.supervisor_max_turns), "--board-url", a.board_url, "--db-url", a.db_url]
        self.orchestrator = subprocess.Popen(cmd, cwd=ROOT, env=self.env, stdout=self.orchestrator_log,
                                             stderr=subprocess.STDOUT)
        self.log(f"orchestrator started (pid {self.orchestrator.pid})")

    def start_watchdog(self) -> None:
        self.watchdog = subprocess.Popen(
            ["bash", str(ROOT / "scripts" / "watchdog.sh"), "--interval", str(self.args.watchdog_interval), "--restart"],
            cwd=ROOT, env=self.env, stdout=open(f"{self.out}-watchdog.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT,
        )
        self.log(f"watchdog started (pid {self.watchdog.pid}, every {self.args.watchdog_interval}s, --restart)")

    def heartbeat(self) -> dict[str, Any] | None:
        raw = self.sql("select value from control_state where key = 'heartbeat'")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def wait_for_health(self, timeout_s: float = 60.0) -> bool:
        import urllib.request

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.args.board_url}/health", timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:  # noqa: BLE001 - not up yet
                pass
            time.sleep(0.5)
        return False

    def tasks_left(self) -> int:
        raw = self.sql("select count(*) from tasks where status in ('open', 'leased')")
        return int(raw) if raw.isdigit() else -1

    # chaos

    def chaos_kill_orchestrator(self) -> None:
        if self.orchestrator is None or self.orchestrator.poll() is not None:
            self.log("kill-orchestrator: no orchestrator process of ours is running; skipping")
            return
        before = self.heartbeat() or {}
        old_pid = before.get("pid")
        killed_at = time.monotonic()
        self.orchestrator.send_signal(signal.SIGKILL)
        self.orchestrator.wait(timeout=10)
        # The sessions it owned keep running as orphans; a VM loss would have taken them too.
        subprocess.run(["pkill", "-f", "claude --output-format stream-json"], check=False)
        self.log(f"kill-orchestrator: SIGKILL sent to pid {self.orchestrator.pid}; waiting for the watchdog")
        self.orchestrator = None
        # The watchdog restores and relaunches; a fresh heartbeat with a new pid is the sign.
        deadline = time.monotonic() + max(180.0, 4 * self.args.watchdog_interval)
        while time.monotonic() < deadline:
            beat = self.heartbeat()
            if beat and beat.get("pid") not in (None, old_pid) and not beat.get("stopping"):
                gap = round(time.monotonic() - killed_at, 1)
                self.heartbeat_gaps.append({"killed_pid": old_pid, "new_pid": beat.get("pid"), "gap_s": gap,
                                            "epoch": beat.get("epoch")})
                self.log(f"kill-orchestrator: back after {gap}s as pid {beat.get('pid')} in epoch {beat.get('epoch')}")
                return
            time.sleep(2)
        self.heartbeat_gaps.append({"killed_pid": old_pid, "new_pid": None, "gap_s": None, "epoch": None})
        self.log("kill-orchestrator: no fresh heartbeat before the deadline")

    def apply_chaos(self, item: dict[str, Any]) -> None:
        kind = item["kind"]
        if kind == "kill-solver":
            result = self.civ("chaos", "kill", "random-solver")
        elif kind == "board-down":
            result = self.civ("chaos", "board-down", str(item["arg"]))
        elif kind == "lag":
            result = self.civ("chaos", "lag", str(item["arg"]))
        elif kind == "kill-orchestrator":
            self.chaos_kill_orchestrator()
            result = "see heartbeat gaps"
        else:
            result = f"unknown chaos kind {kind}"
        self.chaos_done.append({**item, "at": dt.datetime.now().isoformat(timespec="seconds"), "result": result})

    # the run

    def prepare(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.log(f"soak: pack {self.args.pack}, {self.args.minutes:g} minutes, {self.args.solvers} solvers on {self.args.model}, "
                 f"{self.args.supervisors} supervisor(s) on {self.args.supervisor_model} every {self.args.supervisor_interval}s")
        self.run("bash", str(ROOT / "scripts" / "bootstrap.sh"))
        self.run(PYTHON, str(ROOT / "scripts" / "reset_db.py"))
        self.run(PYTHON, str(ROOT / "scripts" / "seed_tasks.py"), "--pack", str(Path(self.args.pack).resolve()))
        total = self.sql("select count(*) from tasks")
        self.log(f"seeded {total} tasks from the pack")

    def main_loop(self) -> None:
        deadline = self.started + self.args.minutes * 60
        pending = list(self.args.chaos)
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - self.started
            while pending and pending[0]["at_s"] <= elapsed:
                self.apply_chaos(pending.pop(0))
            if self.orchestrator is not None and self.orchestrator.poll() is not None:
                code = self.orchestrator.returncode
                self.log(f"orchestrator exited on its own with code {code}")
                self.orchestrator = None
                # The pack may be done, or the governor may have halted it; either way the run is over.
                break
            left = self.tasks_left()
            if left == 0:
                self.log("every task is done; stopping early")
                break
            time.sleep(5)

    def stop(self) -> None:
        # The orchestrator first (SIGINT, so its sessions are disconnected), then the watchdog, so
        # the watchdog does not see the stop as a death and restart it.
        if self.watchdog is not None and self.watchdog.poll() is None:
            self.watchdog.terminate()
            try:
                self.watchdog.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.watchdog.kill()
            self.log("watchdog stopped")
        # The watchdog may have relaunched the orchestrator under a pid we do not hold.
        for proc in (self.orchestrator,):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
        subprocess.run(["pkill", "-INT", "-f", "harness.orchestrator.main"], check=False)
        time.sleep(3)
        subprocess.run(["pkill", "-f", "harness.orchestrator.main"], check=False)
        subprocess.run(["pkill", "-f", "harness.board_mcp.server"], check=False)
        subprocess.run(["pkill", "-f", "claude --output-format stream-json"], check=False)
        self.log("orchestrator and board server stopped")

    # the record

    def summary(self) -> str:
        q = self.sql
        done, total, mean = (q("select count(*) from tasks where status = 'done'"), q("select count(*) from tasks"),
                             q("select coalesce(round(avg(score)::numeric, 3)::text, 'n/a') from tasks where status = 'done'"))
        handoffs = q("select count(*) from events where type = 'agent.handed_off'")
        stubs = q("select count(*) from events where type = 'agent.stub_written'")
        kills = q("select coalesce(string_agg(reason || ': ' || n, ', '), 'none') from "
                  "(select payload->>'reason' as reason, count(*) as n from events where type = 'agent.killed' group by 1) k")
        rounds = q("select count(*) from rounds")
        tunes = q("select coalesce(string_agg(payload->>'knob' || ' ' || (payload->>'from') || ' -> ' || (payload->>'to'), '; '), 'none') "
                  "from events where type = 'tool.civ_tune'")
        pings = q("select count(*) from events where type = 'report.ping'")
        spend = q("select coalesce(string_agg(role || ' $' || round(usd::numeric, 4), ', '), 'none') from "
                  "(select a.role, sum((e.payload->>'cost_usd')::float) as usd from events e join agents a on a.id = e.agent_id "
                  "where e.type = 'agent.ended' and e.payload->>'cost_usd' is not null group by a.role) s")
        epochs = q("select string_agg(epoch::text || ': ' || n || ' events', ', ' order by epoch) from "
                   "(select epoch, count(*) as n from events group by epoch) e")
        generations = q("select coalesce(string_agg(lineage_id || ' g' || g, ', ' order by lineage_id), 'none') from "
                        "(select lineage_id, max(generation) as g from agents where role = 'solver' group by lineage_id) l")
        lines = [
            f"soak, {dt.date.today()}: pack {self.args.pack}, planned {self.args.minutes:g} min, "
            f"wall {int(time.monotonic() - self.started)} s",
            f"solvers {self.args.solvers} on {self.args.model} at a {self.args.context_window} window; "
            f"supervisors {self.args.supervisors} on {self.args.supervisor_model} every {self.args.supervisor_interval} s",
            f"tasks: {done} of {total} done, mean score {mean}",
            f"successions: {handoffs} real handoffs, {stubs} stubs; kills by reason: {kills}",
            f"lineage generations: {generations}",
            f"rounds: {rounds}; tunes: {tunes}; pings: {pings}",
            f"spend by role: {spend}",
            f"epochs: {epochs}",
            "chaos: " + ("; ".join(f"{c['kind']}@{c['at_s']}s -> {c['result']}" for c in self.chaos_done) or "none"),
            "heartbeat gaps: " + ("; ".join(
                f"pid {g['killed_pid']} killed, back as pid {g['new_pid']} in epoch {g['epoch']} after {g['gap_s']} s"
                if g["gap_s"] is not None else f"pid {g['killed_pid']} killed, never came back"
                for g in self.heartbeat_gaps) or "none"),
            "",
            "timeline:",
            *("  " + line for line in self.log_lines),
        ]
        return "\n".join(lines) + "\n"

    def write_records(self) -> None:
        Path(f"{self.out}-summary.txt").write_text(self.summary(), encoding="utf-8")
        events = self.sql("select seq || ' | ' || ts::time || ' | ' || coalesce(agent_id, '') || ' | ' || type || ' | ' || payload::text "
                          "from events order by seq")
        Path(f"{self.out}-events.txt").write_text(events + "\n", encoding="utf-8")
        dashboard = Path(f"{self.out}-dashboard.html")
        self.run(PYTHON, str(ROOT / "scripts" / "dashboard.py"), "--out", str(dashboard), "--db-url", self.args.db_url,
                 "--title", f"civ soak {dt.date.today()}", check=False)
        if dashboard.exists() and not self.args.no_desktop_copy:
            try:
                shutil.copyfile(dashboard, self.args.desktop_copy)
                self.log(f"dashboard copied to {self.args.desktop_copy}")
            except OSError as exc:
                self.log(f"dashboard copy failed: {exc}")
        self.log(f"records: {self.out}-summary.txt, -events.txt, -dashboard.html, -orchestrator.log, -watchdog.log")

    def go(self) -> int:
        try:
            self.prepare()
            self.start_watchdog()
            self.start_orchestrator()
            if not self.wait_for_health():
                self.log("board server did not come up; stopping")
                return 1
            self.main_loop()
        finally:
            self.stop()
            self.write_records()
            self.orchestrator_log.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not Path(args.pack).is_dir():
        print(f"soak: pack directory not found: {args.pack}", file=sys.stderr)
        return 2
    return Soak(args).go()


if __name__ == "__main__":
    raise SystemExit(main())
