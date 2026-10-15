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
from .db import Db, fetch_all, fetch_one
from .limits import vote_weight_for


class Operator:
    def __init__(self, db: Db):
        self.db = db

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

    def succeed(self, from_agent: str, to_agent: str, cwd: str) -> str:
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
                (to_agent, pred["role"], generation, pred["lineage_id"], pred["harness_version"], cwd, token),
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
