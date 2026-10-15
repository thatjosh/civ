"""Helpers shared by the tool modules: refusals, events, message shaping, and the one insert path.

Every message the board stores goes through insert_message so that read_seq stamping and the
long-body file-ref rule apply uniformly, whether the message came from board_post or from a tool
that posts on the agent's behalf (task_complete, task_release, civ_alert).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import psycopg

from ..auth import AgentIdentity
from ..db import fetch_all, fetch_one
from ..limits import Knobs
from ..notices import pending_pin
from ..ranking import SCORE_SQL

BODY_PREVIEW_CHARS = 300


def refuse(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}


def log_event(conn: psycopg.Connection, agent_id: str | None, type: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "insert into events (agent_id, type, payload) values (%s, %s, %s)",
        (agent_id, type, json.dumps(payload, default=str)),
    )


HANDOFF_LOCKOUT = "hand off now: call civ_handoff with the five headings"


def pin_block(conn: psycopg.Connection, identity: AgentIdentity, *, during_handoff: bool = False) -> dict[str, Any] | None:
    """The write gate every write tool passes through first. Two things close it: an operator vote
    awaiting this agent's ack, and a handoff request. Once the orchestrator has asked for a handoff
    the only write that makes sense is the handoff itself, so every other write is refused with the
    same instruction; a prompt alone cannot be trusted to steer an agent whose context is nearly
    full, but a tool result it has to read can. civ_handoff passes during_handoff so it is exempt
    from the lockout while still honouring a pending pin."""
    if not during_handoff:
        row = fetch_one(conn, "select status from agents where id = %s", (identity.id,))
        # Status is read fresh rather than taken from the identity, because the orchestrator flips
        # it mid-session and the token was resolved before that.
        if row is not None and row["status"] == "handing_off":
            return refuse(HANDOFF_LOCKOUT)
    pin = pending_pin(conn, identity)
    if pin is None:
        return None
    return refuse(f"acknowledge operator vote on message {int(pin['message_id'])} first")


def decay_params(knobs: Knobs, conn: psycopg.Connection | None = None) -> dict[str, float]:
    return {"decay_offset": knobs.get("decay_offset_h", conn), "decay_power": knobs.get("decay_power", conn)}


MESSAGE_SELECT = f"""
select m.id, m.author_id, m.kind, m.thread_id, m.parent_id, m.body, m.refs, m.created_at,
       {SCORE_SQL} as score,
       (select count(*) from votes v where v.message_id = m.id and v.value = 1) as votes_up,
       (select count(*) from votes v where v.message_id = m.id and v.value = -1) as votes_down,
       exists (select 1 from operator_pins p where p.message_id = m.id and p.pinned_until > now()) as pinned
from messages m
"""


def shape_message(row: dict) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "seq": int(row["id"]),
        "author_id": row["author_id"],
        "kind": row["kind"],
        "thread_id": int(row["thread_id"]) if row["thread_id"] is not None else None,
        "parent_id": int(row["parent_id"]) if row["parent_id"] is not None else None,
        "body": row["body"],
        "refs": list(row["refs"] or []),
        "score": round(float(row["score"]), 4),
        "votes_up": int(row["votes_up"]),
        "votes_down": int(row["votes_down"]),
        "created_at": row["created_at"].isoformat(),
    }


def select_messages(conn: psycopg.Connection, knobs: Knobs, where: str, order: str,
                    params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Run the standard message query. Pinned operator votes always come first."""
    sql = f"{MESSAGE_SELECT} where {where} order by pinned desc, {order} limit %(limit)s"
    rows = fetch_all(conn, sql, {**params, **decay_params(knobs, conn), "limit": limit})
    return [shape_message(r) for r in rows]


def touch_session_read(conn: psycopg.Connection, identity: AgentIdentity, head: int) -> None:
    conn.execute(
        """
        insert into session_reads (agent_id, last_seq, updated_at) values (%s, %s, now())
        on conflict (agent_id) do update set last_seq = excluded.last_seq, updated_at = now()
        """,
        (identity.id, head),
    )


def last_read_seq(conn: psycopg.Connection, identity: AgentIdentity) -> int | None:
    row = fetch_one(conn, "select last_seq from session_reads where agent_id = %s", (identity.id,))
    return int(row["last_seq"]) if row else None


def resolve_under_cwd(identity: AgentIdentity, rel_or_abs: str) -> Path | str:
    """A file an agent hands to the board must live inside its own directory (symlinks and '..'
    resolved first), because that directory is the only thing the agent is allowed to write.
    Returns the resolved path, or the refusal text."""
    cwd = Path(identity.cwd).resolve()
    given = Path(rel_or_abs)
    candidate = (given if given.is_absolute() else cwd / given).resolve()
    if not candidate.is_relative_to(cwd):
        return "path must be inside your own directory"
    if not candidate.is_file():
        return f"file not found: {rel_or_abs}"
    return candidate


def copy_into_shared(shared_dir: Path, source: Path) -> tuple[str, Path]:
    """Copy a file into shared/ under its content hash. Identical bytes share one file, so two
    agents submitting the same result do not double the store. Returns (ref, absolute path)."""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    shared_dir.mkdir(parents=True, exist_ok=True)
    dest = shared_dir / f"{digest}-{source.name}"
    if not dest.exists():
        shutil.copyfile(source, dest)
    return f"shared/{dest.name}", dest


def spill_long_body(body: str, shared_dir: Path, limit_chars: int) -> tuple[str, list[str]]:
    """Bodies over the limit are written to shared/ by hash; the stored body keeps a preview.
    Returns (stored_body, extra_refs)."""
    if len(body) <= limit_chars:
        return body, []
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    shared_dir.mkdir(parents=True, exist_ok=True)
    path = shared_dir / f"msg-{digest}.md"
    path.write_text(body, encoding="utf-8")
    ref = f"shared/{path.name}"
    return f"{body[:BODY_PREVIEW_CHARS]} [full text: {ref}]", [ref]


def insert_message(conn: psycopg.Connection, identity: AgentIdentity, kind: str, body: str, *,
                   thread_id: int | None, parent_id: int | None, refs: list[str] | None,
                   shared_dir: Path, knobs: Knobs) -> dict[str, Any]:
    stored, extra = spill_long_body(body, shared_dir, int(knobs.get("long_body_chars", conn)))
    all_refs = list(refs or []) + extra
    row = fetch_one(
        conn,
        """
        insert into messages (author_id, kind, thread_id, parent_id, body, refs, read_seq)
        values (%s, %s, %s, %s, %s, %s, %s) returning id
        """,
        (identity.id, kind, thread_id, parent_id, stored, all_refs, last_read_seq(conn, identity)),
    )
    return {"id": int(row["id"]), "seq": int(row["id"]), "refs": all_refs}
