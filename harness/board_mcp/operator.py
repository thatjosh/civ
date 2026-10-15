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
