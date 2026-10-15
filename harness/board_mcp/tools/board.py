"""The board tools: post, read, top, search, vote, ack.

Each method takes the caller's AgentIdentity explicitly and returns the JSON dict the contract
specifies. Rule refusals are returned, not raised, so the agent reads the reason and adapts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..auth import AgentIdentity
from ..db import Db, fetch_one, head_seq
from ..limits import Knobs, posts_in_last_hour, vote_weight_for, votes_in_last_hour
from .common import (copy_into_shared, insert_message, log_event, pin_block, refuse, resolve_under_cwd,
                     select_messages, touch_session_read)

AGENT_KINDS = {"finding", "question", "blocker", "proposal"}
ROUTED_KINDS = {"alert": "civ_alert", "report": "civ_report", "handoff": "civ_handoff"}
MAX_LIMIT = 50


class BoardTools:
    def __init__(self, db: Db, shared_dir: Path, knobs: Knobs):
        self.db = db
        self.shared_dir = shared_dir
        self.knobs = knobs

    def post(self, identity: AgentIdentity, kind: str, body: str, thread_id: int | None = None,
             parent_id: int | None = None, refs: list[str] | None = None) -> dict[str, Any]:
        if kind in ROUTED_KINDS:
            return refuse(f"post {kind} messages through {ROUTED_KINDS[kind]}")
        if kind not in AGENT_KINDS:
            return refuse(f"agents may post only {', '.join(sorted(AGENT_KINDS))}")
        if not body or not body.strip():
            return refuse("body is empty")
        if kind == "proposal":
            if not refs:
                return refuse("a proposal must carry a ref naming its diff")
            # The reviewing supervisor never reads another agent's directory, so the diff has to be
            # copied into shared/ (by hash, like long bodies) and the ref rewritten to point there.
            shared_refs = []
            for ref in refs:
                source = resolve_under_cwd(identity, ref)
                if isinstance(source, str):
                    return refuse(f"proposal ref {ref}: {source}")
                shared_refs.append(copy_into_shared(self.shared_dir, source)[0])
            refs = shared_refs
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            limit = int(self.knobs.get("post_rate_per_h", conn))
            if posts_in_last_hour(conn, identity.id) >= limit:
                return refuse(f"post rate limit of {limit} per hour reached; read and vote instead")
            resolved = self._resolve_thread(conn, thread_id, parent_id)
            if "error" in resolved:
                return refuse(resolved["error"])
            inserted = insert_message(
                conn, identity, kind, body, thread_id=resolved["thread_id"], parent_id=parent_id,
                refs=refs, shared_dir=self.shared_dir, knobs=self.knobs,
            )
            log_event(conn, identity.id, "tool.board_post",
                      {"id": inserted["id"], "kind": kind, "thread_id": resolved["thread_id"]})
            return {"ok": True, "id": inserted["id"], "seq": inserted["seq"]}

    @staticmethod
    def _resolve_thread(conn, thread_id: int | None, parent_id: int | None) -> dict[str, Any]:
        """A post names its thread root directly, or inherits it from its parent."""
        if parent_id is not None:
            parent = fetch_one(conn, "select id, thread_id from messages where id = %s", (parent_id,))
            if parent is None:
                return {"error": f"parent message {parent_id} does not exist"}
            inherited = int(parent["thread_id"]) if parent["thread_id"] is not None else int(parent["id"])
            if thread_id is None:
                thread_id = inherited
        if thread_id is not None:
            root = fetch_one(conn, "select id, kind, thread_id from messages where id = %s", (thread_id,))
            if root is None:
                return {"error": f"thread {thread_id} does not exist"}
            if root["thread_id"] is not None:
                return {"error": f"message {thread_id} is not a thread root; use thread_id={int(root['thread_id'])}"}
            # Alerts, broadcasts, reports and handoffs are roots too, but only a task opens a thread.
            if root["kind"] != "task":
                return {"error": "thread_id must be a task root"}
        return {"thread_id": thread_id}

    def read(self, identity: AgentIdentity, thread_id: int | None = None, kind: str | None = None,
             since_seq: int | None = None, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        clauses, params = ["true"], {}
        if thread_id is not None:
            clauses.append("(m.thread_id = %(thread_id)s or m.id = %(thread_id)s)")
            params["thread_id"] = thread_id
        if kind is not None:
            clauses.append("m.kind = %(kind)s")
            params["kind"] = kind
        if since_seq is not None:
            clauses.append("m.id > %(since_seq)s")
            params["since_seq"] = since_seq
        # A thread or a tail read is a conversation, so it goes in order; a kind read is a ranking.
        order = "m.id asc" if (thread_id is not None or since_seq is not None) else "score desc, m.id desc"
        return self._read(identity, " and ".join(clauses), order, params, limit, "tool.board_read")

    def top(self, identity: AgentIdentity, kind: str | None = None, window_hours: float = 24,
            limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        clauses = ["m.created_at > now() - make_interval(secs => %(window_s)s)"]
        params: dict[str, Any] = {"window_s": float(window_hours) * 3600.0}
        if kind is not None:
            clauses.append("m.kind = %(kind)s")
            params["kind"] = kind
        return self._read(identity, " and ".join(clauses), "score desc, m.id desc", params, limit, "tool.board_top")

    def search(self, identity: AgentIdentity, query: str, limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        if not query or not query.strip():
            return refuse("query is empty")
        fulltext = (
            "to_tsvector('english', m.body || ' ' || coalesce((select string_agg(v.reason, ' ') "
            "from votes v where v.message_id = m.id), '')) @@ plainto_tsquery('english', %(q)s)"
        )
        with self.db.transaction() as conn:
            messages = []
            if len(query.strip()) >= 3:
                messages = select_messages(conn, self.knobs, fulltext, "score desc, m.id desc", {"q": query}, limit)
            if not messages:
                # Short or unusual terms (ids, codes) miss the stemmer, so fall back to a substring match.
                messages = select_messages(conn, self.knobs, "m.body ilike %(like)s", "score desc, m.id desc",
                                           {"like": f"%{query.strip()}%"}, limit)
            head = head_seq(conn)
            touch_session_read(conn, identity, head)
            log_event(conn, identity.id, "tool.board_search", {"query": query, "n": len(messages)})
            return {"ok": True, "messages": messages, "head_seq": head}

    def _read(self, identity, where, order, params, limit, event_type) -> dict[str, Any]:
        with self.db.transaction() as conn:
            messages = select_messages(conn, self.knobs, where, order, params, limit)
            head = head_seq(conn)
            touch_session_read(conn, identity, head)
            log_event(conn, identity.id, event_type, {"n": len(messages), **{k: v for k, v in params.items()}})
            return {"ok": True, "messages": messages, "head_seq": head}

    def vote(self, identity: AgentIdentity, message_id: int, value: int, reason: str) -> dict[str, Any]:
        if value not in (-1, 1):
            return refuse("value must be +1 or -1")
        if not reason or not reason.strip():
            return refuse("a vote needs a reason")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            target = fetch_one(conn, "select author_id from messages where id = %s", (message_id,))
            if target is None:
                return refuse(f"message {message_id} does not exist")
            if target["author_id"] == identity.id:
                return refuse("you cannot vote on your own message")
            if fetch_one(conn, "select 1 from votes where message_id = %s and voter_id = %s", (message_id, identity.id)):
                return refuse("you have already voted on this message")
            limit = int(self.knobs.get("vote_rate_per_h", conn))
            if votes_in_last_hour(conn, identity.id) >= limit:
                return refuse(f"vote rate limit of {limit} per hour reached")
            weight = vote_weight_for(conn, identity.role)
            conn.execute(
                "insert into votes (message_id, voter_id, value, weight, reason) values (%s, %s, %s, %s, %s)",
                (message_id, identity.id, value, weight, reason.strip()),
            )
            log_event(conn, identity.id, "tool.board_vote", {"message_id": message_id, "value": value, "weight": weight})
            return {"ok": True, "weight": weight}

    def ack(self, identity: AgentIdentity, message_id: int) -> dict[str, Any]:
        with self.db.transaction() as conn:
            pin = fetch_one(conn, "select message_id from operator_pins where message_id = %s", (message_id,))
            if pin is None:
                return refuse(f"message {message_id} carries no operator vote to acknowledge")
            conn.execute(
                "insert into operator_acks (message_id, agent_id) values (%s, %s) on conflict do nothing",
                (message_id, identity.id),
            )
            log_event(conn, identity.id, "tool.board_ack", {"message_id": message_id})
            return {"ok": True}
