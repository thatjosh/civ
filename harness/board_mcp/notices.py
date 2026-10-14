"""The piggyback: steers from the operator ride along on ordinary tool results.

Rather than make agents poll, the server checks on every call whether the caller has unseen
broadcasts or an unacknowledged operator vote, and attaches them. Broadcast marks advance when
included; operator pins stay until acknowledged or expired.
"""

from __future__ import annotations

import psycopg

from .auth import AgentIdentity
from .db import fetch_all, fetch_one


def pending_pin(conn: psycopg.Connection, identity: AgentIdentity) -> dict | None:
    """The oldest live operator pin this agent has not acknowledged, if any."""
    return fetch_one(
        conn,
        """
        select p.message_id, p.pinned_until, m.body
        from operator_pins p join messages m on m.id = p.message_id
        where p.pinned_until > now()
          and not exists (select 1 from operator_acks a where a.message_id = p.message_id and a.agent_id = %s)
        order by p.message_id
        limit 1
        """,
        (identity.id,),
    )


def collect(conn: psycopg.Connection, identity: AgentIdentity) -> list[dict]:
    notices: list[dict] = []
    seen = fetch_one(conn, "select last_seq from broadcast_seen where agent_id = %s", (identity.id,))
    last_seq = int(seen["last_seq"]) if seen else 0
    broadcasts = fetch_all(
        conn,
        "select id, body from messages where kind = 'broadcast' and id > %s order by id",
        (last_seq,),
    )
    for b in broadcasts:
        notices.append({"type": "broadcast", "seq": int(b["id"]), "body": b["body"]})
    if broadcasts:
        conn.execute(
            """
            insert into broadcast_seen (agent_id, last_seq) values (%s, %s)
            on conflict (agent_id) do update set last_seq = excluded.last_seq
            """,
            (identity.id, int(broadcasts[-1]["id"])),
        )
    pin = pending_pin(conn, identity)
    if pin:
        notices.append({
            "type": "operator_vote",
            "message_id": int(pin["message_id"]),
            "body": pin["body"],
            "pinned_until": pin["pinned_until"].isoformat(),
        })
    return notices
