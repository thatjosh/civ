"""Agent identity for the board server.

The spawn token (sent as the X-Civ-Token header) is the identity: it is issued by the orchestrator at
spawn, maps to exactly one agents row, and dies with the session. Tools take an AgentIdentity
explicitly so that tests can exercise them without HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .db import fetch_one

OPERATOR_ID = "operator"
ACTIVE_STATUSES = ("live", "handing_off")


@dataclass(frozen=True)
class AgentIdentity:
    id: str
    role: str
    cwd: str
    status: str
    generation: int
    lineage_id: str

    @property
    def is_supervisor(self) -> bool:
        return self.role == "supervisor"


def resolve_token(conn: psycopg.Connection, token: str | None) -> AgentIdentity | None:
    """Map a spawn token to a live agent, or None if unknown or no longer active."""
    if not token:
        return None
    row = fetch_one(
        conn,
        "select id, role, cwd, status, generation, lineage_id from agents where token = %s",
        (token,),
    )
    if row is None or row["status"] not in ACTIVE_STATUSES:
        return None
    return AgentIdentity(
        id=row["id"], role=row["role"], cwd=row["cwd"], status=row["status"],
        generation=row["generation"], lineage_id=row["lineage_id"],
    )


def load_identity(conn: psycopg.Connection, agent_id: str) -> AgentIdentity | None:
    """Reload an identity by id; used when status may have changed since the token was resolved."""
    row = fetch_one(
        conn,
        "select id, role, cwd, status, generation, lineage_id from agents where id = %s",
        (agent_id,),
    )
    if row is None:
        return None
    return AgentIdentity(
        id=row["id"], role=row["role"], cwd=row["cwd"], status=row["status"],
        generation=row["generation"], lineage_id=row["lineage_id"],
    )
