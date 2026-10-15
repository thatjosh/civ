"""The civ tools: handoff, alert, status, and the supervisor-only tune and report.

handoff carries its full validation and ends the agent itself (M3); tune carries its validation
even though supervisor rounds (M4) do not drive it yet, so that milestone adds orchestration
rather than rules.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..auth import AgentIdentity
from ..db import Db, fetch_one
from ..limits import Knobs, live_counts, posts_in_last_hour
from .common import insert_message, log_event, pin_block, refuse

HANDOFF_HEADINGS = ("State of work", "Open threads", "What to try next", "What not to retry", "Files worth reading")
SLOS = ("completions_per_mtok", "blocked_failed_rate", "duplicate_rate", "lease_wait")


class CivTools:
    def __init__(self, db: Db, knobs: Knobs, shared_dir: Path):
        self.db = db
        self.knobs = knobs
        self.shared_dir = shared_dir

    def handoff(self, identity: AgentIdentity, body: str) -> dict[str, Any]:
        missing = [h for h in HANDOFF_HEADINGS if not re.search(rf"^##\s+{re.escape(h)}\s*$", body or "", re.I | re.M)]
        if missing:
            return refuse("handoff is missing headings: " + ", ".join(f"## {h}" for h in missing))
        max_tokens = int(self.knobs.get("max_handoff_tokens"))
        approx_tokens = len(body) / 4
        if approx_tokens > max_tokens:
            return refuse(f"handoff is about {int(approx_tokens)} tokens; the ceiling is {max_tokens}")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity, during_handoff=True)
            if blocked:
                return blocked
            status = fetch_one(conn, "select status from agents where id = %s", (identity.id,))
            if status is None or status["status"] != "handing_off":
                return refuse("handoff not requested; the orchestrator sets your status to handing_off first")
            row = fetch_one(
                conn,
                "insert into handoffs (lineage_id, from_agent, generation, body) values (%s, %s, %s, %s) returning id",
                (identity.lineage_id, identity.id, identity.generation, body),
            )
            insert_message(conn, identity, "handoff", body, thread_id=None, parent_id=None, refs=None,
                           shared_dir=self.shared_dir, knobs=self.knobs)
            # The handoff is the agent's last act: ending it here (rather than waiting for the
            # orchestrator) means its token stops working at once, so a confused agent cannot keep
            # writing after it has handed over, and the lease stays put for the successor to inherit.
            conn.execute("update agents set status = 'ended', ended_at = now() where id = %s", (identity.id,))
            log_event(conn, identity.id, "tool.civ_handoff", {"handoff_id": int(row["id"])})
            log_event(conn, identity.id, "agent.handoff_written",
                      {"handoff_id": int(row["id"]), "lineage_id": identity.lineage_id, "generation": identity.generation})
            return {"ok": True, "handoff_id": int(row["id"]), "stop": True}

    def alert(self, identity: AgentIdentity, body: str) -> dict[str, Any]:
        if not body or not body.strip():
            return refuse("alert body is empty")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            # An alert is a post by another name, so it draws on the same hourly allowance; otherwise
            # the one kind that reaches the supervisors would be the one kind an agent could flood.
            limit = int(self.knobs.get("post_rate_per_h", conn))
            if posts_in_last_hour(conn, identity.id) >= limit:
                return refuse(f"post rate limit of {limit} per hour reached; alerts count toward it")
            inserted = insert_message(conn, identity, "alert", body.strip(), thread_id=None, parent_id=None,
                                      refs=None, shared_dir=self.shared_dir, knobs=self.knobs)
            log_event(conn, identity.id, "tool.civ_alert", {"id": inserted["id"]})
            return {"ok": True, "id": inserted["id"]}

    def status(self, identity: AgentIdentity) -> dict[str, Any]:
        with self.db.transaction() as conn:
            live = live_counts(conn)
            tasks = {"open": 0, "leased": 0, "done": 0, "blocked": 0}
            for r in conn.execute("select status, count(*) as n from tasks group by status").fetchall():
                tasks[r["status"]] = int(r["n"])
            # The orchestrator logs one agent.turn event per model turn with the turn's token deltas.
            tpm = fetch_one(
                conn,
                """
                select coalesce(sum((payload->>'tokens_in')::bigint + (payload->>'tokens_out')::bigint), 0) as n
                from events where type = 'agent.turn' and ts > now() - interval '1 minute'
                """,
            )
            me = fetch_one(conn, "select generation, context_pct from agents where id = %s", (identity.id,))
            lease = fetch_one(conn, "select id from tasks where lease_owner = %s and status = 'leased'", (identity.id,))
            last_claim = fetch_one(
                conn, "select max(ts) as ts from events where agent_id = %s and type = 'task.claimed'", (identity.id,)
            )
            if last_claim and last_claim["ts"]:
                cast = fetch_one(conn, "select count(*) as n from votes where voter_id = %s and created_at > %s",
                                 (identity.id, last_claim["ts"]))
            else:
                cast = fetch_one(conn, "select count(*) as n from votes where voter_id = %s", (identity.id,))
            log_event(conn, identity.id, "tool.civ_status", {})
            return {
                "ok": True,
                "live_agents": live,
                "tokens_per_min": int(tpm["n"]),
                "tasks": tasks,
                "you": {
                    "id": identity.id,
                    "generation": int(me["generation"]) if me else identity.generation,
                    "context_pct": float(me["context_pct"]) if me else 0.0,
                    "lease": lease["id"] if lease else None,
                    "votes_since_claim": int(cast["n"]),
                },
            }

    def tune(self, identity: AgentIdentity, knob: str, value: float, slo: str, reason: str,
             ttl_s: int = 3600) -> dict[str, Any]:
        if not identity.is_supervisor:
            return refuse("supervisor only")
        if slo not in SLOS:
            return refuse("slo must be one of " + ", ".join(SLOS))
        if not reason or not reason.strip():
            return refuse("a tune needs a reason")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            row = fetch_one(conn, "select * from config where knob = %s for update", (knob,))
            if row is None:
                return refuse(f"unknown knob {knob}")
            value = float(value)
            if value < row["min"] or value > row["max"]:
                return refuse(f"{knob} must stay within [{row['min']}, {row['max']}]")
            if abs(value - float(row["value"])) > float(row["step"]) + 1e-9:
                return refuse(f"{knob} moves at most one step ({row['step']}) per tune; it is {row['value']}")
            if row["set_by"] is not None:
                cooling = fetch_one(conn, "select (%s + make_interval(secs => %s)) > now() as hot",
                                    (row["set_at"], int(row["cooldown_s"])))
                if cooling["hot"]:
                    return refuse(f"{knob} is inside its {row['cooldown_s']}s cooldown")
            updated = fetch_one(
                conn,
                """
                update config set value = %s, set_by = %s, reason = %s, set_at = now(), ttl_s = %s,
                       expires_at = now() + make_interval(secs => %s)
                where knob = %s returning expires_at
                """,
                (value, identity.id, reason.strip(), int(ttl_s), int(ttl_s), knob),
            )
            log_event(conn, identity.id, "tool.civ_tune",
                      {"knob": knob, "from": float(row["value"]), "to": value, "slo": slo, "reason": reason.strip()})
            self.knobs.invalidate()
            return {"ok": True, "knob": knob, "value": value, "expires_at": updated["expires_at"].isoformat()}

    def report(self, identity: AgentIdentity, body: str, trigger: str | None = None) -> dict[str, Any]:
        if not identity.is_supervisor:
            return refuse("supervisor only")
        if trigger:
            return refuse("trigger verification arrives in M4")
        if not body or not body.strip():
            return refuse("report body is empty")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            inserted = insert_message(conn, identity, "report", body.strip(), thread_id=None, parent_id=None,
                                      refs=None, shared_dir=self.shared_dir, knobs=self.knobs)
            log_event(conn, identity.id, "tool.civ_report", {"id": inserted["id"]})
            return {"ok": True, "id": inserted["id"]}
