"""The operator's and orchestrator's write path into the board.

Agents reach the board only through MCP tools. Everything else that writes (seeding tasks,
broadcasting, registering a spawn, recording usage) goes through this class so that the server
remains the single writer in spirit: one module owns every non-agent write.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from .auth import OPERATOR_ID
from .ceilings import Ceilings
from .db import Db, fetch_all, fetch_one
from .limits import live_counts, vote_weight_for
from .seed import CONFIG_SEEDS
from .slo import SLO_NAMES, compute_slos, compute_succession, status_snapshot

PING_PREFIX = "[ping: "
BRIEF_HISTORY_ROUNDS = 5
BRIEF_TOP_BLOCKERS = 10
BRIEF_LOWEST_SCORES = 3


# The merge section of the brief: how many rows, and how much of a detail or a body to carry.
BRIEF_RECENT_MERGES = 10
BRIEF_DETAIL_CHARS = 200
BRIEF_BODY_CHARS = 300
class Operator:
    def __init__(self, db: Db, ceilings: Ceilings | None = None):
        self.db = db
        # Only spend_usd needs the ceilings (for the model to price live sessions at); built lazily
        # so a bare Operator(db) keeps working everywhere else.
        self._ceilings = ceilings

    @property
    def ceilings(self) -> Ceilings:
        if self._ceilings is None:
            self._ceilings = Ceilings()
        return self._ceilings

    # Agents

    def register_agent(self, agent_id: str, role: str, lineage_id: str, cwd: str,
                       generation: int = 1, harness_version: str = "dev") -> str:
        """Insert the agent row and return its spawn token."""
        token = secrets.token_urlsafe(24)
        with self.db.transaction() as conn:
            conn.execute(
                """
                insert into agents (id, role, generation, lineage_id, harness_version, cwd, token)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (agent_id, role, generation, lineage_id, harness_version, cwd, token),
            )
            self._event(conn, agent_id, "agent.spawned",
                        {"role": role, "generation": generation, "lineage_id": lineage_id})
        return token

    def set_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int, context_pct: float) -> None:
        self.db.execute(
            "update agents set tokens_in = %s, tokens_out = %s, context_pct = %s where id = %s",
            (tokens_in, tokens_out, context_pct, agent_id),
        )

    def set_agent_status(self, agent_id: str, status: str) -> None:
        ended = status in ("ended", "dead")
        with self.db.transaction() as conn:
            conn.execute(
                "update agents set status = %s, ended_at = case when %s then now() else ended_at end where id = %s",
                (status, ended, agent_id),
            )
            self._event(conn, agent_id, "agent.status", {"status": status})

    def log_event(self, agent_id: str | None, type: str, payload: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            self._event(conn, agent_id, type, payload or {})

    # Succession (M3)

    def request_handoff(self, agent_id: str) -> bool:
        """Flip a live agent to handing_off. From here the server refuses every write but the
        handoff, so the request reaches the agent through its next tool result even if the message
        the orchestrator sends into the session is delayed. Returns False if the agent was not live."""
        with self.db.transaction() as conn:
            row = fetch_one(
                conn,
                "update agents set status = 'handing_off' where id = %s and status = 'live' returning id",
                (agent_id,),
            )
            if row is None:
                return False
            self._event(conn, agent_id, "agent.handoff_requested", {})
        return True

    def latest_handoff(self, lineage_id: str) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            """
            select id, lineage_id, from_agent, to_agent, generation, body, stub, created_at
            from handoffs where lineage_id = %s order by id desc limit 1
            """,
            (lineage_id,),
        )
        if row is None:
            return None
        out = dict(row)
        out["id"] = int(out["id"])
        out["created_at"] = out["created_at"].isoformat()
        return out

    def recent_posts(self, agent_id: str, n: int = 30) -> list[dict[str, Any]]:
        """The agent's last n board messages, newest first."""
        rows = self.db.fetch_all(
            "select id, kind, thread_id, parent_id, body, created_at from messages "
            "where author_id = %s order by id desc limit %s",
            (agent_id, int(n)),
        )
        out = []
        for r in rows:
            r = dict(r)
            r["id"] = int(r["id"])
            r["created_at"] = r["created_at"].isoformat()
            out.append(r)
        return out

    def write_stub_handoff(self, agent_id: str, listing: list[str]) -> int:
        """Store a stub handoff for an agent that died without writing one. Built from the board
        and the given directory listing by stub.build_stub, no model call."""
        from .stub import build_stub

        posts = self.recent_posts(agent_id)
        with self.db.transaction() as conn:
            agent = fetch_one(conn, "select lineage_id, generation from agents where id = %s", (agent_id,))
            if agent is None:
                raise ValueError(f"unknown agent {agent_id}")
            task = fetch_one(
                conn,
                "select id, title, description, thread_id from tasks where lease_owner = %s and status = 'leased'",
                (agent_id,),
            )
            body = build_stub(posts, dict(task) if task else None, list(listing))
            row = fetch_one(
                conn,
                "insert into handoffs (lineage_id, from_agent, generation, body, stub) "
                "values (%s, %s, %s, %s, true) returning id",
                (agent["lineage_id"], agent_id, int(agent["generation"]), body),
            )
            handoff_id = int(row["id"])
            self._event(conn, agent_id, "agent.stub_written",
                        {"handoff_id": handoff_id, "posts": len(posts), "task": task["id"] if task else None})
        return handoff_id

    def succeed(self, from_agent: str, to_agent: str, cwd: str, harness_version: str | None = None) -> str:
        """Register the successor of from_agent and hand it everything that outlives a session:
        the lineage, the directory, and the lease. Returns the successor's token.

        The lease is transferred rather than released so no neighbour can claim the task in the gap
        between one generation ending and the next starting; its expiry is reset because the
        successor needs a full lease to pick the work up."""
        token = secrets.token_urlsafe(24)
        with self.db.transaction() as conn:
            pred = fetch_one(
                conn,
                "select role, generation, lineage_id, harness_version, status from agents where id = %s for update",
                (from_agent,),
            )
            if pred is None:
                raise ValueError(f"unknown agent {from_agent}")
            generation = int(pred["generation"]) + 1
            conn.execute(
                """
                insert into agents (id, role, generation, lineage_id, harness_version, cwd, token)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                # A successor is a new spawn, so it carries the current harness version when the
                # runner passes one (a merge may have bumped it since its predecessor was born).
                (to_agent, pred["role"], generation, pred["lineage_id"], harness_version or pred["harness_version"], cwd, token),
            )
            if pred["status"] in ("live", "handing_off"):
                # A predecessor still marked active here never wrote its handoff (the kill path);
                # two active generations of one lineage must never coexist.
                conn.execute("update agents set status = 'ended', ended_at = now() where id = %s", (from_agent,))
            lease_s = fetch_one(conn, "select value from config where knob = 'lease_length_s'")
            lease_length = int(lease_s["value"]) if lease_s else 1800
            moved = fetch_one(
                conn,
                """
                update tasks set lease_owner = %s, lease_expires = now() + make_interval(secs => %s)
                where lease_owner = %s and status = 'leased' returning id
                """,
                (to_agent, lease_length, from_agent),
            )
            conn.execute(
                """
                update handoffs set to_agent = %s where id = (
                    select id from handoffs where lineage_id = %s and to_agent is null order by id desc limit 1)
                """,
                (to_agent, pred["lineage_id"]),
            )
            self._event(conn, to_agent, "agent.spawned",
                        {"role": pred["role"], "generation": generation, "lineage_id": pred["lineage_id"]})
            self._event(conn, from_agent, "agent.succeeded",
                        {"from": from_agent, "to": to_agent, "generation": generation,
                         "lease": moved["id"] if moved else None})
        return token

    def lineage_state(self, lineage_id: str) -> dict[str, Any] | None:
        """The head of a lineage: its latest generation, that agent's status, and any task it holds."""
        head = self.db.fetch_one(
            "select id, generation, status from agents where lineage_id = %s order by generation desc limit 1",
            (lineage_id,),
        )
        if head is None:
            return None
        lease = self.db.fetch_one(
            "select id from tasks where lease_owner = %s and status = 'leased'", (head["id"],)
        )
        return {
            "generation": int(head["generation"]),
            "head_agent": head["id"],
            "status": head["status"],
            "lease": lease["id"] if lease else None,
        }

    # Supervisor rounds (M4)

    def open_round(self, number: int, final: bool = False) -> int:
        """Open round `number`. Its window starts where the previous round started, so consecutive
        rounds partition the timeline; the first round's window starts at the earliest event."""
        with self.db.transaction() as conn:
            previous = fetch_one(conn, "select started_at from rounds order by id desc limit 1")
            if previous is not None:
                window_start = previous["started_at"]
            else:
                first = fetch_one(conn, "select min(ts) as ts from events")
                window_start = first["ts"] if first and first["ts"] else None
            row = fetch_one(
                conn,
                "insert into rounds (number, final, window_start) values (%s, %s, coalesce(%s, now())) returning id, window_start",
                (number, final, window_start),
            )
            round_id = int(row["id"])
            self._event(conn, OPERATOR_ID, "round.opened",
                        {"round_id": round_id, "number": number, "final": final, "window_start": row["window_start"].isoformat()})
        return round_id

    def current_round(self) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            "select id, number, started_at, window_start, tuned_knob, final from rounds where ended_at is null order by id desc limit 1"
        )
        if row is None:
            return None
        out = {"id": int(row["id"]), "number": int(row["number"]), "started_at": row["started_at"].isoformat(),
               "window_start": row["window_start"].isoformat(), "tuned_knob": row["tuned_knob"], "final": bool(row["final"]),
               "tuned_from": None, "tuned_to": None}
        if row["tuned_knob"] is not None:
            # The rounds row records which knob moved; the from and to live on the tune event, and
            # the round summary line wants both, so they are read back here rather than denormalised.
            tune = self.db.fetch_one(
                "select payload from events where type = 'tool.civ_tune' and (payload->>'round_id')::bigint = %s "
                "order by seq desc limit 1",
                (int(row["id"]),),
            )
            if tune is not None:
                out["tuned_from"] = tune["payload"].get("from")
                out["tuned_to"] = tune["payload"].get("to")
        return out

    def reopened_in_round(self, round_id: int) -> list[str]:
        """Task ids reopened by supervisors while the round was open, for the round summary."""
        rows = self.db.fetch_all(
            """
            select e.payload->>'task_id' as task_id from events e, rounds r
            where r.id = %s and e.type = 'task.reopened'
              and e.ts >= r.started_at and (r.ended_at is null or e.ts <= r.ended_at)
            order by e.seq
            """,
            (int(round_id),),
        )
        return [r["task_id"] for r in rows if r["task_id"]]

    def close_round(self, round_id: int, summary: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute("update rounds set ended_at = now(), summary = %s where id = %s", (json.dumps(summary, default=str), round_id))
            self._event(conn, OPERATOR_ID, "round.closed", {"round_id": round_id, **summary})

    def round_brief(self, round_id: int, supervisors: int | None = None) -> dict[str, Any]:
        """Build the round's brief from the database, store it on the round, return it.

        Stored as built because two of the ping triggers are claims about history ("degraded for
        three windows", "kills in two consecutive windows") and the only honest way to verify them
        is against what each round's supervisors were actually shown. `supervisors` is the number
        the orchestrator is about to spawn; when omitted it is counted from the agents table."""
        with self.db.transaction() as conn:
            rnd = fetch_one(conn, "select id, number, started_at, window_start, final, tuned_knob from rounds where id = %s", (round_id,))
            if rnd is None:
                raise ValueError(f"unknown round {round_id}")
            window_start, window_end = rnd["window_start"], rnd["started_at"]
            previous = fetch_one(conn, "select id, started_at, window_start, brief from rounds where id < %s order by id desc limit 1", (round_id,))
            if supervisors is None:
                counted = fetch_one(conn, "select count(*) as n from agents where role = 'supervisor' and generation = %s", (rnd["number"],))
                supervisors = int(counted["n"])
            current_slos = compute_slos(conn, window_start, window_end)
            brief = {
                "round": {
                    "id": int(rnd["id"]), "number": int(rnd["number"]), "final": bool(rnd["final"]),
                    "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
                    "supervisors": supervisors,
                },
                "status": status_snapshot(conn),
                "slo": {
                    "current": current_slos,
                    "previous": compute_slos(conn, previous["window_start"], previous["started_at"]) if previous else None,
                    "min_sample": int(self._knob_value(conn, "min_sample")),
                    "history": self._slo_history(conn, round_id, int(rnd["number"]), current_slos),
                },
                "succession": compute_succession(conn, window_start, window_end),
                "blockers": self._blockers(conn, window_start, window_end),
                "blocked_tasks": self._blocked_tasks(conn),
                "alerts": self._alerts(conn, window_start),
                "previous_reports": self._reports(conn, int(previous["id"])) if previous else [],
                "tunes": {
                    "active": self._active_tunes(conn),
                    "pending": self._pending_tunes(conn),
                    "round_tuned": rnd["tuned_knob"] is not None,
                },
                "verifier": self._verifier_progress(conn),
                "merges": {"recent": self._recent_merges(conn), "pending_proposals": self._pending_proposals(conn)},
            }
            conn.execute("update rounds set brief = %s where id = %s", (json.dumps(brief, default=str), round_id))
        return brief

    def set_knob(self, knob: str, value: float, reason: str) -> None:
        """An operator preset: bounds only, no step, cooldown or TTL, because the operator owns the
        box the supervisors move inside. Used for launch values and for fault injection."""
        with self.db.transaction() as conn:
            row = fetch_one(conn, "select value, min, max from config where knob = %s for update", (knob,))
            if row is None:
                raise ValueError(f"unknown knob {knob}")
            value = float(value)
            if value < row["min"] or value > row["max"]:
                raise ValueError(f"{knob} must stay within [{row['min']}, {row['max']}]")
            conn.execute(
                "update config set value = %s, set_by = %s, reason = %s, set_at = now(), ttl_s = null, expires_at = null where knob = %s",
                (value, OPERATOR_ID, reason, knob),
            )
            self._event(conn, OPERATOR_ID, "knob.preset", {"knob": knob, "from": float(row["value"]), "to": value, "reason": reason})

    def reports_for_round(self, round_id: int) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            return self._reports(conn, round_id)

    def pings_since(self, ts: Any) -> list[dict[str, Any]]:
        """Reports that carried a verified trigger, newest last, for the operator's inbox (M5)."""
        rows = self.db.fetch_all(
            "select id, author_id, body, created_at from messages where kind = 'report' and body like %s and created_at > %s order by id",
            (PING_PREFIX + "%", ts),
        )
        return [self._shape_report(r) for r in rows]

    # brief helpers

    @staticmethod
    def _knob_value(conn, knob: str) -> float:
        row = fetch_one(conn, "select value from config where knob = %s", (knob,))
        return float(row["value"]) if row else float(CONFIG_SEEDS[knob][0])

    @staticmethod
    def _slo_history(conn, round_id: int, number: int, current: dict[str, Any]) -> list[dict[str, Any]]:
        """One row per round, oldest first, this round last, up to the last few. Earlier rows come
        from the briefs stored on those rounds, so the history is what was shown, not a recomputation."""
        rows = fetch_all(
            conn,
            "select number, brief from rounds where id < %s and brief <> '{}'::jsonb order by id desc limit %s",
            (round_id, BRIEF_HISTORY_ROUNDS - 1),
        )
        history = []
        for r in reversed(rows):
            slos = (r["brief"].get("slo") or {}).get("current") or {}
            history.append({"round": int(r["number"]), **{name: (slos.get(name) or {}).get("value") for name in SLO_NAMES}})
        history.append({"round": number, **{name: current[name]["value"] for name in SLO_NAMES}})
        return history

    @staticmethod
    def _blockers(conn, window_start, window_end) -> list[dict[str, Any]]:
        rows = fetch_all(
            conn,
            """
            select m.id, m.thread_id, t.id as task_id, m.author_id, m.body, m.created_at,
                   coalesce((select sum(v.value * v.weight) from votes v where v.message_id = m.id), 0) as score
            from messages m left join tasks t on t.thread_id = m.thread_id
            where m.kind = 'blocker' and m.created_at >= %s and m.created_at < %s
            order by score desc, m.id desc limit %s
            """,
            (window_start, window_end, BRIEF_TOP_BLOCKERS),
        )
        return [{"id": int(r["id"]), "thread_id": int(r["thread_id"]) if r["thread_id"] is not None else None,
                 "task_id": r["task_id"], "author_id": r["author_id"], "body": r["body"],
                 "score": float(r["score"]), "created_at": r["created_at"].isoformat()} for r in rows]

    @staticmethod
    def _blocked_tasks(conn) -> list[dict[str, Any]]:
        rows = fetch_all(
            conn,
            """
            select t.id, t.title, t.attempts,
                   (select body from messages m where m.thread_id = t.thread_id and m.kind = 'blocker' order by m.id desc limit 1) as last_release_reason
            from tasks t where t.status = 'blocked' order by t.id
            """,
        )
        return [{"id": r["id"], "title": r["title"], "attempts": int(r["attempts"]), "last_release_reason": r["last_release_reason"]} for r in rows]

    @staticmethod
    def _alerts(conn, window_start) -> list[dict[str, Any]]:
        rows = fetch_all(conn, "select id, author_id, body, created_at from messages where kind = 'alert' and created_at >= %s order by id",
                         (window_start,))
        return [{"id": int(r["id"]), "author_id": r["author_id"], "body": r["body"], "created_at": r["created_at"].isoformat()} for r in rows]

    def _reports(self, conn, round_id: int) -> list[dict[str, Any]]:
        """A round's reports are the report messages written while it was open; the supervisors of
        a round are the only supervisors alive then, so time is identity enough."""
        rnd = fetch_one(conn, "select started_at, ended_at from rounds where id = %s", (round_id,))
        if rnd is None:
            return []
        rows = fetch_all(
            conn,
            "select id, author_id, body, created_at from messages where kind = 'report' and created_at >= %s "
            "and created_at <= coalesce(%s, now()) order by id",
            (rnd["started_at"], rnd["ended_at"]),
        )
        return [self._shape_report(r) for r in rows]

    @staticmethod
    def _shape_report(r) -> dict[str, Any]:
        body = r["body"]
        trigger = body[len(PING_PREFIX):body.index("]")] if body.startswith(PING_PREFIX) and "]" in body else None
        return {"id": int(r["id"]), "author_id": r["author_id"], "body": body, "created_at": r["created_at"].isoformat(),
                "ping": trigger is not None, "trigger": trigger}

    @staticmethod
    def _active_tunes(conn) -> list[dict[str, Any]]:
        rows = fetch_all(
            conn,
            "select knob, value, set_by, reason, expires_at from config where set_by is not null "
            "and (expires_at is null or expires_at > now()) order by knob",
        )
        return [{"knob": r["knob"], "value": float(r["value"]), "seed": float(CONFIG_SEEDS[r["knob"]][0]) if r["knob"] in CONFIG_SEEDS else None,
                 "set_by": r["set_by"], "reason": r["reason"],
                 "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None} for r in rows]

    @staticmethod
    def _pending_tunes(conn) -> list[dict[str, Any]]:
        rows = fetch_all(
            conn,
            "select p.knob, p.value, p.proposed_by, r.number from tune_proposals p join rounds r on r.id = p.round_id "
            "where p.committed_by is null order by p.id",
        )
        return [{"knob": r["knob"], "value": float(r["value"]), "proposed_by": r["proposed_by"], "round_number": int(r["number"])} for r in rows]

    @staticmethod
    def _verifier_progress(conn) -> dict[str, Any]:
        totals = fetch_one(conn, "select count(*) as total, count(*) filter (where status = 'done') as done, avg(score) as mean from tasks")
        lowest = fetch_all(conn, "select id, score, notes from tasks where status = 'done' and score is not null order by score asc, id limit %s",
                           (BRIEF_LOWEST_SCORES,))
        return {"done": int(totals["done"]), "total": int(totals["total"]),
                "mean_score": float(totals["mean"]) if totals["mean"] is not None else None,
                "lowest": [{"id": r["id"], "score": float(r["score"]), "notes": r["notes"]} for r in lowest]}

    # Merge gate (M6): what the brief shows and what the merger reads and writes

    @staticmethod
    def _shape_merge(r) -> dict[str, Any]:
        return {"id": int(r["id"]), "proposal_id": int(r["proposal_id"]), "requested_by": r["requested_by"],
                "round_id": int(r["round_id"]) if r["round_id"] is not None else None, "diff_ref": r["diff_ref"],
                "paths": list(r["paths"] or []), "status": r["status"], "detail": r["detail"],
                "version_before": r["version_before"], "version_after": r["version_after"], "commit_sha": r["commit_sha"],
                "requested_at": r["requested_at"].isoformat(),
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None}

    _MERGE_COLUMNS = ("id, proposal_id, requested_by, round_id, diff_ref, paths, status, detail, version_before, "
                      "version_after, commit_sha, requested_at, finished_at")

    def _recent_merges(self, conn, limit: int = BRIEF_RECENT_MERGES) -> list[dict[str, Any]]:
        rows = fetch_all(conn, f"select {self._MERGE_COLUMNS} from merges order by id desc limit %s", (limit,))
        out = []
        for r in rows:
            shaped = self._shape_merge(r)
            # The detail is a test tail or a refusal; the brief wants the gist, the row keeps it all.
            shaped["detail"] = (shaped["detail"] or "")[:BRIEF_DETAIL_CHARS] or None
            out.append({k: shaped[k] for k in ("id", "proposal_id", "requested_by", "status", "paths", "detail", "version_after", "requested_at")})
        return out

    @staticmethod
    def _pending_proposals(conn, limit: int = BRIEF_RECENT_MERGES) -> list[dict[str, Any]]:
        """Proposals no merge has spoken for, best scored first, so a supervisor sees what waits for
        review. The decay formula is the board's own, with the seeds as a fallback for a wiped config."""
        rows = fetch_all(
            conn,
            """
            select m.id, m.author_id, m.body, m.refs, m.created_at,
                   coalesce((select sum(v.value * v.weight) from votes v where v.message_id = m.id), 0)
                   / power(extract(epoch from (now() - m.created_at)) / 3600.0
                           + coalesce((select value from config where knob = 'decay_offset_h'), 2),
                           coalesce((select value from config where knob = 'decay_power'), 1.5)) as score
            from messages m
            where m.kind = 'proposal'
              and not exists (select 1 from merges g where g.proposal_id = m.id
                              and g.status in ('merged', 'queued', 'running', 'queued_for_operator'))
            order by score desc, m.id desc limit %s
            """,
            (limit,),
        )
        return [{"id": int(r["id"]), "author_id": r["author_id"], "body": (r["body"] or "")[:BRIEF_BODY_CHARS],
                 "refs": list(r["refs"] or []), "score": round(float(r["score"]), 4), "created_at": r["created_at"].isoformat()}
                for r in rows]

    def merge_queue(self, statuses: tuple[str, ...] = ("queued",)) -> list[dict[str, Any]]:
        """Merges in the given states, oldest first: the merger's work list."""
        rows = self.db.fetch_all(f"select {self._MERGE_COLUMNS} from merges where status = any(%s) order by id", (list(statuses),))
        return [self._shape_merge(r) for r in rows]

    def merge_start(self, merge_id: int) -> bool:
        """Take a queued merge: queued to running. False when it is no longer queued, so two mergers
        cannot both apply one diff."""
        row = self.db.fetch_one("update merges set status = 'running' where id = %s and status = 'queued' returning id", (int(merge_id),))
        return row is not None

    def merge_finish(self, merge_id: int, status: str, detail: str | None, version_before: str | None = None,
                     version_after: str | None = None, commit_sha: str | None = None) -> None:
        """Record how a merge ended and log merge.<status>; the merger logs nothing else about it."""
        with self.db.transaction() as conn:
            row = fetch_one(
                conn,
                """
                update merges set status = %s, detail = %s, version_before = %s, version_after = %s, commit_sha = %s,
                       finished_at = now()
                where id = %s returning proposal_id, requested_by, paths
                """,
                (status, detail, version_before, version_after, commit_sha, int(merge_id)),
            )
            if row is None:
                raise ValueError(f"unknown merge {merge_id}")
            self._event(conn, row["requested_by"], f"merge.{status}",
                        {"merge_id": int(merge_id), "proposal_id": int(row["proposal_id"]), "paths": list(row["paths"] or []),
                         "detail": (detail or "")[:BRIEF_DETAIL_CHARS], "version_before": version_before,
                         "version_after": version_after, "commit_sha": commit_sha})

    def merges_since(self, ts) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(f"select {self._MERGE_COLUMNS} from merges where requested_at >= %s order by id", (ts,))
        return [self._shape_merge(r) for r in rows]

    # Operator controls (M5): reads for the governor and the civ command line

    def tokens_per_minute(self) -> int:
        """Tokens all agents spent in the last minute, from the per-turn deltas the orchestrator logs."""
        row = self.db.fetch_one(
            """
            select coalesce(sum((payload->>'tokens_in')::bigint + (payload->>'tokens_out')::bigint), 0) as n
            from events where type = 'agent.turn' and ts > now() - interval '1 minute'
            """
        )
        return int(row["n"])

    def spend_usd(self, hours: float) -> float:
        """Dollars spent in the last `hours`: ended sessions by the cost the SDK reported (or the
        orchestrator estimated), plus an estimate for sessions still running. A live session's
        tokens_in is the sum of its context sizes, which a long session reads almost entirely
        from cache, so it is priced at the cache-read rate plus output, for the ceilings' model."""
        ended = self.db.fetch_one(
            """
            select coalesce(sum((payload->>'cost_usd')::float), 0) as usd from events
            where type = 'agent.ended' and payload->>'cost_usd' is not null
              and ts > now() - make_interval(secs => %s)
            """,
            (float(hours) * 3600.0,),
        )
        total = float(ended["usd"])
        live = self.db.fetch_all(
            "select tokens_in, tokens_out from agents where status in ('live', 'handing_off') "
            "and spawned_at > now() - make_interval(secs => %s)",
            (float(hours) * 3600.0,),
        )
        if live:
            # Imported here so the board package does not load the orchestrator at import time.
            from ..orchestrator.monitor import estimate_cost_usd

            model = self.ceilings.get("model")
            for row in live:
                estimate = estimate_cost_usd(model, 0, int(row["tokens_out"]), int(row["tokens_in"]), 0)
                total += estimate or 0.0
        return total

    def live_agents(self) -> dict[str, int]:
        with self.db.transaction() as conn:
            return live_counts(conn)

    def epoch(self) -> int:
        """The current epoch from control_state; 1 before migration 0004 or when the row is gone."""
        value = self.control_get("epoch")
        return int(value) if value is not None else 1

    def control_get(self, key: str) -> Any | None:
        with self.db.transaction() as conn:
            present = fetch_one(conn, "select to_regclass('control_state') is not null as present")
            if not present or not present["present"]:
                return None
            row = fetch_one(conn, "select value from control_state where key = %s", (key,))
        return row["value"] if row else None

    def control_set(self, key: str, value: Any) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                insert into control_state (key, value, updated_at) values (%s, %s, now())
                on conflict (key) do update set value = excluded.value, updated_at = now()
                """,
                (key, json.dumps(value, default=str)),
            )

    def lineages_to_resume(self) -> list[dict[str, Any]]:
        """Every lineage whose latest handoff has no successor yet: what --resume spawns after a
        restore. A lineage whose last session ended on its own has a successor on its last handoff
        (or no handoff at all) and is left alone. Solver lineages only: a supervisor is a round
        session, and a round interrupted by a loss is simply lost."""
        rows = self.db.fetch_all(
            """
            select h.lineage_id, h.from_agent, h.generation, h.id, h.stub, h.body from handoffs h
            join (select lineage_id, max(id) as id from handoffs group by lineage_id) latest on latest.id = h.id
            where h.to_agent is null
              and exists (select 1 from agents a where a.lineage_id = h.lineage_id and a.role = 'solver')
            order by h.lineage_id
            """
        )
        return [
            {"lineage_id": r["lineage_id"], "from_agent": r["from_agent"], "generation": int(r["generation"]),
             "handoff_id": int(r["id"]), "stub": bool(r["stub"]), "body": r["body"]}
            for r in rows
        ]

    def last_round_number(self) -> int:
        """The highest supervisor round number so far, 0 when none. A restarted orchestrator
        continues from here, because round numbers are also supervisor generations and ids."""
        row = self.db.fetch_one("select coalesce(max(number), 0) as n from rounds")
        return int(row["n"]) if row else 0

    def agents_without_ack(self, message_id: int) -> list[str]:
        """Agents that were live while the operator's vote on message_id was pinned and never
        acknowledged it: the receipt the design promises the operator after the window."""
        rows = self.db.fetch_all(
            """
            select a.id from agents a,
                 (select v.created_at as pinned_at, p.pinned_until
                    from votes v join operator_pins p on p.message_id = v.message_id
                   where v.message_id = %s and v.voter_id = %s) w
            where a.spawned_at < w.pinned_until
              and (a.ended_at is null or a.ended_at > w.pinned_at)
              and not exists (select 1 from operator_acks k where k.message_id = %s and k.agent_id = a.id)
            order by a.id
            """,
            (int(message_id), OPERATOR_ID, int(message_id)),
        )
        return [r["id"] for r in rows]

    def inbox(self, since_seq: int, limit: int = 50) -> list[dict[str, Any]]:
        """Reports and alerts newer than since_seq, pings first, then newest first."""
        rows = self.db.fetch_all(
            """
            select id, kind, author_id, body, created_at from messages
            where kind in ('report', 'alert') and id > %s
            order by (body like %s) desc, id desc limit %s
            """,
            (int(since_seq), PING_PREFIX + "%", int(limit)),
        )
        out = []
        for r in rows:
            shaped = self._shape_report(r)
            out.append({"seq": shaped["id"], "kind": r["kind"], "author_id": r["author_id"], "body": r["body"],
                        "trigger": shaped["trigger"], "ping": shaped["ping"], "created_at": shaped["created_at"]})
        return out

    # Board writes

    def create_task(self, task_id: str, title: str, description: str, priority_base: float = 0.0) -> int:
        """Create the task's thread root (a kind=task message) and the tasks row. Returns thread_id."""
        with self.db.transaction() as conn:
            root = fetch_one(
                conn,
                "insert into messages (author_id, kind, body) values (%s, 'task', %s) returning id",
                (OPERATOR_ID, f"{task_id}: {title}\n\n{description}"),
            )
            thread_id = int(root["id"])
            conn.execute(
                """
                insert into tasks (id, thread_id, title, description, priority_base)
                values (%s, %s, %s, %s, %s)
                """,
                (task_id, thread_id, title, description, priority_base),
            )
            self._event(conn, OPERATOR_ID, "task.created", {"task_id": task_id, "thread_id": thread_id})
        return thread_id

    def broadcast(self, body: str) -> int:
        with self.db.transaction() as conn:
            row = fetch_one(
                conn,
                "insert into messages (author_id, kind, body) values (%s, 'broadcast', %s) returning id",
                (OPERATOR_ID, body),
            )
            self._event(conn, OPERATOR_ID, "broadcast", {"id": int(row["id"])})
        return int(row["id"])

    def vote(self, message_id: int, value: int, reason: str, pin_minutes: int = 5) -> int:
        """An operator vote: counted at supervisor weight, and pinned so every agent must acknowledge it."""
        if value not in (-1, 1):
            raise ValueError("value must be +1 or -1")
        with self.db.transaction() as conn:
            weight = vote_weight_for(conn, "operator")
            conn.execute(
                """
                insert into votes (message_id, voter_id, value, weight, reason) values (%s, %s, %s, %s, %s)
                on conflict (message_id, voter_id) do update set value = excluded.value, reason = excluded.reason
                """,
                (message_id, OPERATOR_ID, value, weight, reason),
            )
            conn.execute(
                """
                insert into operator_pins (message_id, pinned_until) values (%s, now() + make_interval(mins => %s))
                on conflict (message_id) do update set pinned_until = excluded.pinned_until
                """,
                (message_id, pin_minutes),
            )
            self._event(conn, OPERATOR_ID, "operator.vote", {"message_id": message_id, "value": value})
        return message_id

    # Reads for scripts and the dashboard

    def tasks_summary(self) -> dict[str, Any]:
        rows = self.db.fetch_all("select status, count(*) as n from tasks group by status")
        counts = {"open": 0, "leased": 0, "done": 0, "blocked": 0}
        for r in rows:
            counts[r["status"]] = int(r["n"])
        scores = self.db.fetch_one("select avg(score) as mean, count(score) as n from tasks where status = 'done'")
        return {"counts": counts, "mean_score": scores["mean"], "scored": int(scores["n"])}

    def board_transcript(self) -> list[dict[str, Any]]:
        """Every message with its votes, ordered by thread then sequence, for board_dump.py."""
        with self.db.transaction() as conn:
            messages = fetch_all(
                conn,
                """
                select id, author_id, kind, thread_id, parent_id, body, refs, read_seq, created_at
                from messages order by coalesce(thread_id, id), id
                """,
            )
            votes = fetch_all(conn, "select message_id, voter_id, value, weight, reason from votes order by created_at")
        by_message: dict[int, list[dict]] = {}
        for v in votes:
            by_message.setdefault(int(v["message_id"]), []).append(
                {"voter_id": v["voter_id"], "value": int(v["value"]), "weight": float(v["weight"]), "reason": v["reason"]}
            )
        out = []
        for m in messages:
            m = dict(m)
            m["id"] = int(m["id"])
            m["created_at"] = m["created_at"].isoformat()
            m["votes"] = by_message.get(m["id"], [])
            out.append(m)
        return out

    @staticmethod
    def _event(conn, agent_id: str | None, type: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "insert into events (agent_id, type, payload) values (%s, %s, %s)",
            (agent_id, type, json.dumps(payload)),
        )
