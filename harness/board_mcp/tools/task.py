"""The task tools: claim, heartbeat, complete, release.

claim is the one place the board uses SELECT ... FOR UPDATE SKIP LOCKED, so that a hundred agents
calling it at once each get a different task or an honest empty result.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

from ..auth import AgentIdentity
from ..db import Db, fetch_one
from ..limits import Knobs
from ..ranking import THREAD_SCORE_SQL
from .common import decay_params, insert_message, log_event, pin_block, refuse

Verifier = Callable[[str, str], tuple[float | None, str]]


class TaskTools:
    def __init__(self, db: Db, shared_dir: Path, knobs: Knobs, verifier: Verifier | None = None):
        self.db = db
        self.shared_dir = shared_dir
        self.knobs = knobs
        self.verifier = verifier

    def claim(self, identity: AgentIdentity) -> dict[str, Any]:
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            self._sweep_expired(conn)
            held = fetch_one(conn, "select id from tasks where lease_owner = %s and status = 'leased'", (identity.id,))
            if held:
                return refuse(f"you already hold task {held['id']}; complete or release it first")
            quota_error = self._vote_quota_error(conn, identity)
            if quota_error:
                return refuse(quota_error)
            row = fetch_one(
                conn,
                f"""
                select t.id from tasks t
                where t.status = 'open' and t.next_attempt_at <= now()
                order by t.priority_base + {THREAD_SCORE_SQL} desc, t.id asc
                for update skip locked
                limit 1
                """,
                decay_params(self.knobs, conn),
            )
            if row is None:
                return {"ok": True, "task": None, "reason": "no open tasks"}
            lease_s = int(self.knobs.get("lease_length_s", conn))
            task = fetch_one(
                conn,
                """
                update tasks set status = 'leased', lease_owner = %s,
                       lease_expires = now() + make_interval(secs => %s)
                where id = %s
                returning id, title, description, thread_id, lease_expires, attempts
                """,
                (identity.id, lease_s, row["id"]),
            )
            log_event(conn, identity.id, "task.claimed", {"task_id": task["id"]})
            return {"ok": True, "task": self._shape(task)}

    @staticmethod
    def _sweep_expired(conn) -> None:
        """A lease nobody heartbeats goes back to the queue; silence is self-punishing."""
        conn.execute(
            """
            update tasks set status = 'open', lease_owner = null, lease_expires = null
            where status = 'leased' and lease_expires < now()
            """
        )

    def _vote_quota_error(self, conn, identity: AgentIdentity) -> str | None:
        quota = int(self.knobs.get("vote_quota", conn))
        if quota <= 0:
            return None
        last = fetch_one(
            conn,
            "select max(ts) as ts from events where agent_id = %s and type = 'task.claimed'",
            (identity.id,),
        )
        if last is None or last["ts"] is None:
            return None  # the first claim is exempt: there is nothing to have voted on yet
        cast = fetch_one(
            conn,
            "select count(*) as n from votes where voter_id = %s and created_at > %s",
            (identity.id, last["ts"]),
        )
        if int(cast["n"]) < quota:
            return (f"cast {quota - int(cast['n'])} more vote(s) with reasons before claiming again "
                    f"(the server requires {quota} between claims)")
        return None

    def heartbeat(self, identity: AgentIdentity, task_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            if not self._holds(conn, identity, task_id):
                return refuse(f"you do not hold the lease on {task_id}")
            row = fetch_one(
                conn,
                "update tasks set lease_expires = now() + make_interval(secs => %s) where id = %s returning lease_expires",
                (int(self.knobs.get("lease_length_s", conn)), task_id),
            )
            log_event(conn, identity.id, "task.heartbeat", {"task_id": task_id})
            return {"ok": True, "lease_expires": row["lease_expires"].isoformat()}

    def complete(self, identity: AgentIdentity, task_id: str, result_path: str, summary: str) -> dict[str, Any]:
        if not summary or not summary.strip():
            return refuse("summary is empty")
        source = self._resolve_result(identity, result_path)
        if isinstance(source, str):
            return refuse(source)
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            if not self._holds(conn, identity, task_id):
                return refuse(f"you do not hold the lease on {task_id}")
            result_ref, abs_copy = self._copy_by_hash(source)
            score, notes = self._verify(task_id, abs_copy)
            task = fetch_one(
                conn,
                """
                update tasks set status = 'done', result_ref = %s, score = %s, notes = %s,
                       completed_by = %s, completed_at = now(), lease_owner = null, lease_expires = null
                where id = %s returning thread_id
                """,
                (result_ref, score, notes, identity.id, task_id),
            )
            insert_message(conn, identity, "finding", f"[{task_id} completed] {summary.strip()}",
                           thread_id=int(task["thread_id"]), parent_id=None, refs=[result_ref],
                           shared_dir=self.shared_dir, knobs=self.knobs)
            log_event(conn, identity.id, "task.completed", {"task_id": task_id, "result_ref": result_ref, "score": score})
            return {"ok": True, "result_ref": result_ref, "score": score, "notes": notes}

    def release(self, identity: AgentIdentity, task_id: str, reason: str) -> dict[str, Any]:
        if not reason or not reason.strip():
            return refuse("say why you are releasing the task")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            if not self._holds(conn, identity, task_id):
                return refuse(f"you do not hold the lease on {task_id}")
            max_attempts = int(self.knobs.get("max_attempts", conn))
            base = float(self.knobs.get("backoff_base_s", conn))
            current = fetch_one(conn, "select attempts, thread_id from tasks where id = %s", (task_id,))
            attempts = int(current["attempts"]) + 1
            status = "blocked" if attempts >= max_attempts else "open"
            backoff_s = base * (2 ** (attempts - 1))
            row = fetch_one(
                conn,
                """
                update tasks set status = %s, attempts = %s, lease_owner = null, lease_expires = null,
                       next_attempt_at = now() + make_interval(secs => %s)
                where id = %s returning next_attempt_at
                """,
                (status, attempts, backoff_s, task_id),
            )
            insert_message(conn, identity, "blocker", f"[{task_id} released, attempt {attempts}] {reason.strip()}",
                           thread_id=int(current["thread_id"]), parent_id=None, refs=None,
                           shared_dir=self.shared_dir, knobs=self.knobs)
            log_event(conn, identity.id, "task.released", {"task_id": task_id, "attempts": attempts, "status": status})
            return {"ok": True, "attempts": attempts, "next_attempt_at": row["next_attempt_at"].isoformat(), "status": status}

    # helpers

    @staticmethod
    def _holds(conn, identity: AgentIdentity, task_id: str) -> bool:
        row = fetch_one(conn, "select 1 from tasks where id = %s and lease_owner = %s and status = 'leased'",
                        (task_id, identity.id))
        return row is not None

    @staticmethod
    def _resolve_result(identity: AgentIdentity, result_path: str) -> Path | str:
        """Results must come from the agent's own directory; anything else is refused."""
        cwd = Path(identity.cwd).resolve()
        candidate = (cwd / result_path).resolve() if not Path(result_path).is_absolute() else Path(result_path).resolve()
        if not candidate.is_relative_to(cwd):
            return "result_path must be inside your own directory"
        if not candidate.is_file():
            return f"result file not found: {result_path}"
        return candidate

    def _copy_by_hash(self, source: Path) -> tuple[str, Path]:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        dest = self.shared_dir / f"{digest}-{source.name}"
        if not dest.exists():
            shutil.copyfile(source, dest)
        return f"shared/{dest.name}", dest

    def _verify(self, task_id: str, path: Path) -> tuple[float | None, str]:
        if self.verifier is None:
            return None, "no verifier configured"
        try:
            score, notes = self.verifier(task_id, str(path))
            return (None if score is None else float(score)), str(notes)
        except Exception as exc:  # a broken verifier must not lose the agent's work
            return None, f"verifier error: {exc}"

    @staticmethod
    def _shape(task: dict) -> dict[str, Any]:
        return {
            "id": task["id"], "title": task["title"], "description": task["description"],
            "thread_id": int(task["thread_id"]), "lease_expires": task["lease_expires"].isoformat(),
            "attempts": int(task["attempts"]),
        }
