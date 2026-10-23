"""The civ command line: the operator's hands on the civilisation.

    civ say "text"                       broadcast to every agent (pinned, pushed on the next tool result)
    civ vote <id> <+1|-1> --reason ...   the pinned, must-acknowledge operator vote
    civ vote --report <id>               who never acknowledged it
    civ inbox [--all] [--limit N]        reports, pings and alerts since last read; unacknowledged steers
    civ status                           epoch, pause, agents, tasks, tokens, spend, shape, knobs
    civ pause [--reason ...] / resume    stop or restart new spawns (the orchestrator's governor reads it)
    civ reshape                          re-read control/civ.toml, bump the epoch
    civ restore --yes                    the one-shot recovery step: new epoch, stubs, leases, knobs, broadcast
    civ chaos kill|board-down|lag|clear|show   inject a fault mid-run (M7)
    civ merge --list | --approve ID | --reject ID --reason ...   the merges waiting on the operator (M6)

This module imports nothing from harness/: it talks to Postgres directly and reads the two toml
files under control/. The harness is agent-editable and may be broken at exactly the moment the
operator needs to pause, broadcast or restore, so the one channel that must keep working depends
on nothing the agents can change. The cost is a little SQL that also exists in the server (the
operator vote weight, the broadcast insert); a test pins the shapes the two copies must agree on.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"
DEFAULT_CONTROL_DIR = REPO_ROOT / "control"
OPERATOR_ID = "operator"
PING_PREFIX = "[ping: "
# The successor prompt path accepts a handoff only with these five headings, bare.
STUB_HEADINGS = ("State of work", "Open threads", "What to try next", "What not to retry", "Files worth reading")
# civ.toml keys that are also config knobs; the restore resets exactly these.
KNOB_KEYS = ("handoff_pct", "kill_pct", "kill_grace_turns", "decay_offset_h", "decay_power",
             "vote_quota", "min_sample", "big_move_steps")

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2


class Refused(Exception):
    """A command that could not be carried out; its text is printed and the exit code is 1."""


# --- the two files -----------------------------------------------------------------------------


class Control:
    """The operator's two settings files."""

    def __init__(self, control_dir: Path) -> None:
        self.dir = Path(control_dir)

    def _load(self, name: str) -> dict[str, Any]:
        path = self.dir / name
        if not path.exists():
            raise Refused(f"{path} not found")
        with path.open("rb") as handle:
            return tomllib.load(handle)

    def ceilings(self) -> dict[str, Any]:
        return self._load("ceilings.toml")

    def shape_tree(self) -> dict[str, Any]:
        """civ.toml as its nested tables. This is the form control_state.shape holds, because the
        orchestrator reads it back section by section the way it reads the file."""
        return self._load("civ.toml")

    def shape(self) -> dict[str, Any]:
        """civ.toml flattened to its leaf keys, because every key is unique across sections and the
        status line, the knob seeds and the reshape diff all speak in leaf names."""
        return flatten(self.shape_tree())


def flatten(tree: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            flat.update(flatten(value))
        else:
            flat[key] = value
    return flat


# --- the database ------------------------------------------------------------------------------


class Board:
    """Every query the CLI makes, over one connection inside one transaction."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def one(self, sql: str, params: Any = None) -> dict | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Any = None) -> list[dict]:
        return self.conn.execute(sql, params).fetchall()

    # control_state

    def state_get(self, key: str) -> Any:
        row = self.one("select value from control_state where key = %s", (key,))
        return row["value"] if row else None

    def state_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "insert into control_state (key, value, updated_at) values (%s, %s, now()) "
            "on conflict (key) do update set value = excluded.value, updated_at = now()",
            (key, json.dumps(value)),
        )

    def epoch(self) -> int:
        value = self.state_get("epoch")
        return int(value) if value is not None else 1

    def bump_epoch(self) -> int:
        new = self.epoch() + 1
        self.state_set("epoch", new)
        return new

    def paused(self) -> tuple[bool, str]:
        """(on, reason). The value is true/false or {"on": bool, "reason": str}; both are accepted."""
        value = self.state_get("paused")
        if isinstance(value, dict):
            return bool(value.get("on")), str(value.get("reason") or "")
        return bool(value), ""

    def event(self, type: str, payload: dict[str, Any]) -> None:
        self.conn.execute("insert into events (agent_id, type, payload) values (%s, %s, %s)",
                          (OPERATOR_ID, type, json.dumps(payload, default=str)))

    # heartbeat and chaos (M7)

    def heartbeat(self) -> dict[str, Any] | None:
        """The orchestrator's last beat as written, plus its age in seconds, or None when there has
        never been one. The age is computed by the database clock, the same clock that stamped it."""
        row = self.one(
            "select value, extract(epoch from (now() - updated_at)) as age_s from control_state where key = 'heartbeat'"
        )
        if row is None or not isinstance(row["value"], dict):
            return None
        beat = dict(row["value"])
        # The beat carries its own timestamp; the row's updated_at is the fallback for a beat
        # written by an older orchestrator without one.
        at = beat.get("at")
        if at:
            parsed = self.one("select extract(epoch from (now() - %s::timestamptz)) as age_s", (at,))
            beat["age_s"] = float(parsed["age_s"])
        else:
            beat["age_s"] = float(row["age_s"])
        return beat

    def chaos(self) -> dict[str, Any]:
        value = self.state_get("chaos")
        base = {"kill": [], "board_down_until": None, "lag_ms": 0}
        if isinstance(value, dict):
            base.update(value)
        base["kill"] = list(base.get("kill") or [])
        return base

    def chaos_set(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge a patch into control_state.chaos and return the result. A merge rather than a
        replace, so a lag set earlier survives a kill added now."""
        merged = {**self.chaos(), **patch}
        self.state_set("chaos", merged)
        return merged

    def random_live_solver(self) -> str | None:
        row = self.one("select id from agents where role = 'solver' and status = 'live' order by random() limit 1")
        return row["id"] if row else None

    # messages and votes

    def broadcast(self, text: str) -> int:
        row = self.one("insert into messages (author_id, kind, body) values (%s, 'broadcast', %s) returning id",
                       (OPERATOR_ID, text))
        return int(row["id"])

    def operator_weight(self) -> float:
        # Live solvers over live supervisors, floored at one: the same rule the server applies, so an
        # operator steer counts for one supervisor's voice and never for nothing.
        row = self.one(
            "select count(*) filter (where role = 'solver') as s, count(*) filter (where role = 'supervisor') as p "
            "from agents where status in ('live', 'handing_off')"
        )
        return max(float(row["s"]) / float(max(int(row["p"]), 1)), 1.0)

    def vote(self, message_id: int, value: int, reason: str, pin_s: int) -> tuple[float, dt.datetime]:
        if self.one("select 1 from messages where id = %s", (message_id,)) is None:
            raise Refused(f"no message #{message_id}")
        weight = self.operator_weight()
        self.conn.execute(
            "insert into votes (message_id, voter_id, value, weight, reason) values (%s, %s, %s, %s, %s) "
            "on conflict (message_id, voter_id) do update set value = excluded.value, weight = excluded.weight, "
            "reason = excluded.reason, created_at = now()",
            (message_id, OPERATOR_ID, value, weight, reason),
        )
        row = self.one(
            "insert into operator_pins (message_id, pinned_until) values (%s, now() + make_interval(secs => %s)) "
            "on conflict (message_id) do update set pinned_until = excluded.pinned_until returning pinned_until",
            (message_id, int(pin_s)),
        )
        return weight, row["pinned_until"]

    def unacknowledged(self, message_id: int) -> list[str]:
        """Agents live during the pin window with no ack. The window starts at the operator's vote
        and ends at pinned_until; an agent spawned before the end and still alive at the start was
        shown the pin and owed an acknowledgement."""
        pin = self.one(
            "select p.pinned_until, v.created_at as started from operator_pins p "
            "join votes v on v.message_id = p.message_id and v.voter_id = %s where p.message_id = %s",
            (OPERATOR_ID, message_id),
        )
        if pin is None:
            raise Refused(f"no operator vote on #{message_id}")
        rows = self.all(
            """
            select a.id from agents a
            where a.spawned_at <= %(until)s and (a.ended_at is null or a.ended_at >= %(started)s)
              and not exists (select 1 from operator_acks k where k.message_id = %(mid)s and k.agent_id = a.id)
            order by a.id
            """,
            {"until": pin["pinned_until"], "started": pin["started"], "mid": message_id},
        )
        return [r["id"] for r in rows]

    def expired_pins(self, hours: float = 24.0) -> list[int]:
        rows = self.all(
            "select message_id from operator_pins where pinned_until < now() "
            "and pinned_until > now() - %s * interval '1 hour' order by message_id",
            (hours,),
        )
        return [int(r["message_id"]) for r in rows]

    def inbox(self, since_seq: int, limit: int) -> tuple[list[dict], int]:
        """Unread reports and alerts, oldest first so a limit never skips the older ones for good,
        and the count of unread beyond the limit."""
        rows = self.all(
            "select id, kind, author_id, body, created_at from messages "
            "where kind in ('report', 'alert') and id > %s order by id asc limit %s",
            (since_seq, limit),
        )
        total = self.one("select count(*) as n from messages where kind in ('report', 'alert') and id > %s", (since_seq,))
        return rows, int(total["n"]) - len(rows)

    # status

    def live_agents(self) -> dict[str, int]:
        counts = {"solver": 0, "supervisor": 0}
        for r in self.all("select role, count(*) as n from agents where status in ('live', 'handing_off') group by role"):
            counts[r["role"]] = int(r["n"])
        return counts

    def tasks(self) -> tuple[dict[str, int], float | None]:
        counts = {"open": 0, "leased": 0, "done": 0, "blocked": 0}
        for r in self.all("select status, count(*) as n from tasks group by status"):
            counts[r["status"]] = int(r["n"])
        mean = self.one("select avg(score) as mean from tasks where status = 'done'")["mean"]
        return counts, (float(mean) if mean is not None else None)

    def tokens_per_minute(self) -> int:
        row = self.one(
            "select coalesce(sum((payload->>'tokens_in')::bigint + (payload->>'tokens_out')::bigint), 0) as n "
            "from events where type = 'agent.turn' and ts > now() - interval '1 minute'"
        )
        return int(row["n"])

    def spend_ended_usd(self, hours: float = 24.0) -> float:
        row = self.one(
            "select coalesce(sum((payload->>'cost_usd')::float), 0) as usd from events "
            "where type = 'agent.ended' and ts > now() - %s * interval '1 hour' and payload->>'cost_usd' is not null",
            (hours,),
        )
        return float(row["usd"])

    def knobs(self) -> list[dict]:
        return self.all("select knob, value, set_by, expires_at from config order by knob")

    # restore

    def last_event_ts(self) -> dt.datetime | None:
        row = self.one("select max(ts) as ts from events")
        return row["ts"] if row else None

    def lineage_heads_in_flight(self) -> list[dict]:
        """The latest generation of every lineage whose agent is still active: these are the sessions
        a VM loss (or a kill of the orchestrator) left mid-flight."""
        return self.all(
            """
            select a.id, a.lineage_id, a.generation,
                   (select t.id from tasks t where t.lease_owner = a.id and t.status = 'leased' limit 1) as task
            from agents a
            where a.status in ('live', 'handing_off') and a.role = 'solver'
              and a.generation = (select max(b.generation) from agents b where b.lineage_id = a.lineage_id)
            order by a.lineage_id
            """
        )

    def retire_active_supervisors(self) -> list[str]:
        """Supervisors caught mid-round are simply ended: a round is a reading of one window, not a
        lineage, so there is nothing to hand to a successor (the first drill stubbed one and the
        resumed orchestrator tried to resume it as a solver)."""
        rows = self.all(
            "update agents set status = 'dead', ended_at = now() "
            "where role = 'supervisor' and status in ('live', 'handing_off') returning id"
        )
        return [r["id"] for r in rows]

    def recent_post_seqs(self, agent_id: str, n: int = 10) -> list[int]:
        rows = self.all("select id from messages where author_id = %s order by id desc limit %s", (agent_id, n))
        return sorted(int(r["id"]) for r in rows)

    def write_restore_stub(self, head: dict, at: dt.datetime) -> int:
        seqs = self.recent_post_seqs(head["id"])
        state = [f"Restore stub written by civ restore at {at.isoformat(timespec='seconds')}; no agent wrote this."]
        state.append(f"Held task: {head['task']}." if head["task"] else "Held no task.")
        state.append("Last posts (read them on the board): " + (", ".join(f"seq {s}" for s in seqs) if seqs else "none") + ".")
        body = "\n".join([
            f"## {STUB_HEADINGS[0]}", *state, "",
            f"## {STUB_HEADINGS[1]}", "Reread the task thread; anything after the last post above is lost.", "",
            f"## {STUB_HEADINGS[2]}", "Continue the held task from the board's findings.", "",
            f"## {STUB_HEADINGS[3]}", "", f"## {STUB_HEADINGS[4]}", "",
        ])
        row = self.one(
            "insert into handoffs (lineage_id, from_agent, generation, body, stub) values (%s, %s, %s, %s, true) returning id",
            (head["lineage_id"], head["id"], int(head["generation"]), body),
        )
        self.conn.execute("update agents set status = 'dead', ended_at = now() where id = %s", (head["id"],))
        return int(row["id"])

    def release_leases(self) -> int:
        # Attempts are left alone: the agents did not fail these tasks, the world did.
        rows = self.all(
            "update tasks set status = 'open', lease_owner = null, lease_expires = null, next_attempt_at = now() "
            "where status = 'leased' returning id"
        )
        return len(rows)

    def reset_knobs(self, seeds: dict[str, Any]) -> int:
        reset = 0
        for knob, value in seeds.items():
            cur = self.conn.execute(
                "update config set value = %s, set_by = null, reason = null, ttl_s = null, expires_at = null where knob = %s",
                (float(value), knob),
            )
            reset += cur.rowcount
        # Every other knob keeps its value but loses its tune: the old world's TTLs mean nothing now.
        self.conn.execute("update config set set_by = null, reason = null, ttl_s = null, expires_at = null")
        return reset


# --- the commands -----------------------------------------------------------------------------


    # merge gate (M6)

    def merges_open(self) -> list[dict]:
        return self.all(
            "select id, proposal_id, status, paths from merges where status not in ('merged', 'rejected', 'failed') order by id"
        )

    def merge_approve(self, merge_id: int) -> dict:
        """An operator-set path waits in queued_for_operator; approving it hands it to the merger."""
        row = self.one("update merges set status = 'queued', detail = coalesce(detail, '') || ' [approved by operator]' "
                       "where id = %s and status = 'queued_for_operator' returning id, proposal_id, paths", (merge_id,))
        if row is None:
            raise Refused(f"merge #{merge_id} is not queued for the operator")
        return row

    def merge_reject(self, merge_id: int, reason: str) -> dict:
        row = self.one("update merges set status = 'rejected', detail = %s, finished_at = now() "
                       "where id = %s and status not in ('merged', 'rejected') returning id, proposal_id", (reason, merge_id))
        if row is None:
            raise Refused(f"merge #{merge_id} cannot be rejected (merged already, or already rejected)")
        return row


def _local(ts: dt.datetime) -> str:
    return ts.astimezone().strftime("%H:%M:%S")


class Cli:
    def __init__(self, db_url: str, control: Control, out=None) -> None:
        self.db_url = db_url
        self.control = control
        self.out = out or sys.stdout

    def _print(self, *lines: str) -> None:
        for line in lines:
            print(line, file=self.out)

    def run(self, command: str, args: argparse.Namespace) -> int:
        handler = getattr(self, f"cmd_{command}")
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            board = Board(conn)
            try:
                handler(board, args)
            except Refused as exc:
                conn.rollback()
                self._print(str(exc))
                return EXIT_REFUSED
            conn.commit()
        return EXIT_OK

    def cmd_say(self, board: Board, args: argparse.Namespace) -> None:
        text = " ".join(args.text).strip()
        if not text:
            raise Refused("nothing to say")
        seq = board.broadcast(text)
        board.event("operator.say", {"id": seq})
        self._print(f"broadcast #{seq}")

    def cmd_vote(self, board: Board, args: argparse.Namespace) -> None:
        if args.report is not None:
            missing = board.unacknowledged(args.report)
            board.event("operator.vote_report", {"message_id": args.report, "unacknowledged": missing})
            self._print(*(missing or ["all acknowledged"]))
            return
        value = {"+1": 1, "1": 1, "-1": -1}[args.value]
        pin_s = int(self.control.shape().get("operator_pin_s", 300))
        weight, until = board.vote(args.message_id, value, args.reason, pin_s)
        board.event("operator.vote", {"message_id": args.message_id, "value": value, "weight": weight,
                                      "pinned_until": until})
        self._print(f"vote on #{args.message_id} pinned until {_local(until)}")

    def cmd_inbox(self, board: Board, args: argparse.Namespace) -> None:
        since = 0 if args.all else int(board.state_get("inbox_last_seq") or 0)
        rows, more = board.inbox(since, args.limit)
        # Pings first, then everything else, newest first: the operator reads the top of the pile.
        pings = [r for r in rows if r["kind"] == "report" and r["body"].startswith(PING_PREFIX)]
        rest = [r for r in rows if r not in pings]
        shown = 0
        for row in sorted(pings, key=lambda r: -r["id"]) + sorted(rest, key=lambda r: -r["id"]):
            kind = "ping" if row in pings else row["kind"]
            self._print(f"#{row['id']} {_local(row['created_at'])} {kind} {row['author_id']}")
            self._print(*("  " + line for line in row["body"].splitlines()))
            shown += 1
        if not rows:
            self._print("inbox empty")
        if more:
            self._print(f"and {more} more unread; run again")
        steers = []
        for message_id in board.expired_pins():
            missing = board.unacknowledged(message_id)
            if missing:
                steers.append(f"#{message_id}: {', '.join(missing)}")
        if steers:
            self._print("unacknowledged steers (last 24 h):", *steers)
        marked = None
        if rows and not args.all:
            marked = max(int(r["id"]) for r in rows)
            board.state_set("inbox_last_seq", marked)
        board.event("operator.inbox", {"shown": shown, "marked": marked, "more": more})

    def cmd_status(self, board: Board, args: argparse.Namespace) -> None:
        ceilings = self.control.ceilings()
        on, reason = board.paused()
        live = board.live_agents()
        counts, mean = board.tasks()
        stored = board.state_get("shape")
        # Stored nested (plus written_at); flattening also accepts an older flat record.
        shape = flatten({k: v for k, v in stored.items() if k != "written_at"}) if isinstance(stored, dict) else self.control.shape()
        source = "control_state" if isinstance(stored, dict) else "civ.toml"
        beat = board.heartbeat()
        if beat is None:
            heartbeat_line = "heartbeat: none"
        else:
            heartbeat_line = (f"heartbeat: {int(round(beat['age_s']))} s ago (pid {beat.get('pid', '?')}, "
                              f"epoch {beat.get('epoch', '?')})" + (" stopping" if beat.get("stopping") else ""))
        self._print(
            heartbeat_line,
            f"epoch {board.epoch()}  paused {'yes (' + reason + ')' if on and reason else 'yes' if on else 'no'}",
            f"agents: {live['solver']} solvers, {live['supervisor']} supervisors live (ceiling {ceilings['max_live_agents']})",
            f"tasks: open {counts['open']}, leased {counts['leased']}, done {counts['done']}, blocked {counts['blocked']}"
            + (f", mean score {mean:.3f}" if mean is not None else ""),
            f"tokens/min: {board.tokens_per_minute()} (ceiling {ceilings['max_tokens_per_minute']})",
            f"spend 24h: ${board.spend_ended_usd():.2f} ended sessions (ceiling ${float(ceilings['max_spend_per_day_usd']):.2f}; live sessions not included)",
            f"shape: solvers {shape.get('solvers')}, supervisors {shape.get('supervisors')}, round {shape.get('supervisor_round_s')}s, "
            f"max_generations {shape.get('max_generations')} (from {source})",
            f"{'knob':<20} {'value':>8} {'seed':>8} {'set_by':<12} expires_at",
        )
        seeds = {k: v for k, v in self.control.shape().items() if k in KNOB_KEYS}
        for row in board.knobs():
            seed = seeds.get(row["knob"])
            expires = _local(row["expires_at"]) if row["expires_at"] else ""
            self._print(f"{row['knob']:<20} {row['value']:>8g} {(f'{seed:g}' if seed is not None else ''):>8} {(row['set_by'] or ''):<12} {expires}")
        board.event("operator.status", {})

    def cmd_pause(self, board: Board, args: argparse.Namespace) -> None:
        reason = (args.reason or "").strip()
        board.state_set("paused", {"on": True, "reason": reason})
        board.event("operator.pause", {"reason": reason})
        self._print(f"paused ({reason})" if reason else "paused")

    def cmd_resume(self, board: Board, args: argparse.Namespace) -> None:
        board.state_set("paused", False)
        board.event("operator.resume", {})
        self._print("resumed")

    def cmd_reshape(self, board: Board, args: argparse.Namespace) -> None:
        new = self.control.shape()
        stored = board.state_get("shape")
        old = flatten({k: v for k, v in stored.items() if k != "written_at"}) if isinstance(stored, dict) else {}
        changes = [(k, old.get(k), new[k]) for k in new if old.get(k) != new[k]]
        changes += [(k, old[k], None) for k in old if k not in new]
        if not changes:
            self._print(f"shape unchanged; epoch stays {board.epoch()}")
            return
        before = board.epoch()
        board.state_set("shape", {**self.control.shape_tree(),
                                  "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        after = board.bump_epoch()
        board.event("operator.reshape", {"changes": [{"key": k, "from": a, "to": b} for k, a, b in changes],
                                         "epoch": after})
        self._print(*(f"{k} {'none' if a is None else a} -> {'none' if b is None else b}" for k, a, b in changes),
                    f"epoch {before} -> {after}")

    def cmd_restore(self, board: Board, args: argparse.Namespace) -> None:
        if not args.yes:
            raise Refused("restore retires the whole population; run again with --yes")
        shape = self.control.shape()
        now = dt.datetime.now(dt.timezone.utc)
        last = board.last_event_ts()
        minutes_lost = round((now - last).total_seconds() / 60.0, 1) if last else 0.0
        # The epoch moves first so the stubs, the broadcast and the events below belong to the new one.
        epoch = board.bump_epoch()
        heads = board.lineage_heads_in_flight()
        for head in heads:
            board.write_restore_stub(head, now)
        retired = board.retire_active_supervisors()
        released = board.release_leases()
        board.reset_knobs({k: v for k, v in shape.items() if k in KNOB_KEYS})
        record = {"at": now.isoformat(timespec="seconds"), "epoch": epoch, "lineages_stubbed": len(heads),
                  "leases_released": released, "minutes_lost": minutes_lost, "supervisors_retired": retired}
        board.state_set("last_restore", record)
        line = (f"Restored at {_local(now)} into epoch {epoch}: {len(heads)} lineages queued for stubs, "
                f"{released} leases released, about {minutes_lost:g} minutes of activity after "
                f"{_local(last) if last else 'the start'} are lost")
        board.broadcast(line)
        board.event("operator.restore", record)
        self._print(line)


    def cmd_merge(self, board: Board, args: argparse.Namespace) -> None:
        if args.approve is not None:
            row = board.merge_approve(args.approve)
            board.event("operator.merge_approve", {"merge_id": args.approve, "proposal_id": int(row["proposal_id"]),
                                                   "paths": list(row["paths"] or [])})
            self._print(f"merge #{args.approve} approved; the merger will run it")
            return
        if args.reject is not None:
            row = board.merge_reject(args.reject, args.reason.strip())
            board.event("operator.merge_reject", {"merge_id": args.reject, "proposal_id": int(row["proposal_id"]),
                                                  "reason": args.reason.strip()})
            self._print(f"merge #{args.reject} rejected")
            return
        rows = board.merges_open()
        if not rows:
            self._print("no merges waiting")
            return
        for row in rows:
            self._print(f"#{row['id']} proposal #{row['proposal_id']} {row['status']} {','.join(row['paths'] or [])}")


    def cmd_chaos(self, board: Board, args: argparse.Namespace) -> None:
        """Three faults the operator can inject mid-run, all through control_state.chaos: a kill
        the orchestrator carries out, a board outage the server honours, and a lag on every tool
        result. They are rows, not signals, so they survive a restart of either process and show
        up in `civ chaos show`."""
        action = args.action
        if action == "kill":
            target = args.value
            if target == "random-solver":
                target = board.random_live_solver()
                if target is None:
                    raise Refused("no live solver to kill")
            else:
                row = board.one("select status from agents where id = %s", (target,))
                if row is None:
                    raise Refused(f"no agent {target}")
                if row["status"] not in ("live", "handing_off"):
                    raise Refused(f"{target} is {row['status']}, not live")
            state = board.chaos()
            if target not in state["kill"]:
                state["kill"].append(target)
            board.chaos_set({"kill": state["kill"]})
            board.event("operator.chaos", {"action": "kill", "agent_id": target})
            self._print(f"chaos: kill queued for {target}")
        elif action == "board-down":
            seconds = int(args.value)
            if seconds <= 0:
                raise Refused("board-down needs a positive number of seconds")
            row = board.one("select (now() + make_interval(secs => %s)) as until", (seconds,))
            until = row["until"]
            board.chaos_set({"board_down_until": until.isoformat()})
            board.event("operator.chaos", {"action": "board-down", "seconds": seconds, "until": until.isoformat()})
            self._print(f"chaos: board down until {_local(until)} ({seconds} s)")
        elif action == "lag":
            lag_ms = int(args.value)
            if lag_ms < 0:
                raise Refused("lag cannot be negative")
            board.chaos_set({"lag_ms": lag_ms})
            board.event("operator.chaos", {"action": "lag", "lag_ms": lag_ms})
            self._print("chaos: lag cleared" if lag_ms == 0 else f"chaos: lag {lag_ms} ms on every tool result")
        elif action == "clear":
            board.state_set("chaos", {"kill": [], "board_down_until": None, "lag_ms": 0})
            board.event("operator.chaos", {"action": "clear"})
            self._print("chaos: cleared")
        elif action == "show":
            state = board.chaos()
            down = state.get("board_down_until")
            self._print(
                f"kill: {', '.join(state['kill']) if state['kill'] else 'none'}",
                f"board_down_until: {down or 'none'}",
                f"lag_ms: {int(state.get('lag_ms') or 0)}",
            )
        else:
            raise Refused(f"unknown chaos action {action}")


# --- argument parsing --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="civ", description="the operator's controls for a running civilisation")
    p.add_argument("--db-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    p.add_argument("--control-dir", type=Path, default=Path(os.environ.get("CIV_CONTROL_DIR", DEFAULT_CONTROL_DIR)))
    sub = p.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="broadcast to every agent")
    say.add_argument("text", nargs="+")

    vote = sub.add_parser("vote", help="the pinned operator vote, or --report on who missed one")
    vote.add_argument("message_id", nargs="?", type=int)
    vote.add_argument("value", nargs="?", choices=["+1", "1", "-1"])
    vote.add_argument("--reason", default="")
    vote.add_argument("--report", type=int, metavar="MESSAGE_ID")

    inbox = sub.add_parser("inbox", help="unread reports, pings and alerts")
    inbox.add_argument("--all", action="store_true", help="show everything and mark nothing read")
    inbox.add_argument("--limit", type=int, default=50)

    sub.add_parser("status", help="the snapshot the agents see, plus spend, shape and knobs")
    pause = sub.add_parser("pause", help="stop new spawns; live agents finish their current task")
    pause.add_argument("--reason", default="")
    sub.add_parser("resume", help="allow spawns again")
    sub.add_parser("reshape", help="re-read control/civ.toml and bump the epoch")
    restore = sub.add_parser("restore", help="the one-shot recovery step")
    restore.add_argument("--yes", action="store_true")

    chaos = sub.add_parser("chaos", help="inject a fault: kill <agent|random-solver>, board-down <s>, lag <ms>, clear, show")
    chaos.add_argument("action", choices=["kill", "board-down", "lag", "clear", "show"])
    chaos.add_argument("value", nargs="?")

    merge = sub.add_parser("merge", help="merges waiting on the operator: list, approve or reject")
    merge.add_argument("--list", action="store_true")
    merge.add_argument("--approve", type=int, metavar="MERGE_ID")
    merge.add_argument("--reject", type=int, metavar="MERGE_ID")
    merge.add_argument("--reason", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "vote":
        if args.report is None and (args.message_id is None or args.value is None):
            parser.error("vote needs <message_id> <+1|-1> --reason ..., or --report <message_id>")
        if args.report is None and not args.reason.strip():
            parser.error("an operator vote needs --reason")
    if args.command == "chaos":
        needs_value = args.action in ("kill", "board-down", "lag")
        if needs_value and args.value is None:
            parser.error(f"chaos {args.action} needs a value")
        if args.action in ("board-down", "lag") and not str(args.value).lstrip("-").isdigit():
            parser.error(f"chaos {args.action} needs an integer")
    if args.command == "merge":
        chosen = [x for x in (args.list, args.approve is not None, args.reject is not None) if x]
        if len(chosen) != 1:
            parser.error("merge takes exactly one of --list, --approve ID, --reject ID")
        if args.reject is not None and not args.reason.strip():
            parser.error("a rejection needs --reason")
    cli = Cli(args.db_url, Control(args.control_dir))
    try:
        return cli.run(args.command, args)
    except psycopg.OperationalError as exc:
        print(f"cannot reach the database at {args.db_url}: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
