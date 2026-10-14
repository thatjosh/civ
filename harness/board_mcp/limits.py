"""Soft knobs and rate limits.

Knobs are read from the config table with a short cache, because every tool call consults several
of them and a supervisor's tune should take effect within seconds, not require a restart.
Rate limits count rows rather than events so that the limit is exactly what the agent did.
"""

from __future__ import annotations

import time

import psycopg
from psycopg.rows import dict_row

from .db import Db, fetch_all, fetch_one
from .seed import CONFIG_SEEDS


class Knobs:
    def __init__(self, db: Db, ttl_s: float = 5.0):
        self._db = db
        self._ttl_s = ttl_s
        self._cache: dict[str, float] = {}
        self._loaded_at = 0.0

    def _refresh(self, conn: psycopg.Connection | None) -> None:
        # Refresh on the caller's connection when there is one; otherwise open a short direct
        # connection rather than borrowing a second pooled one, because a tool already holding a
        # pooled connection would deadlock the pool under load.
        if conn is not None:
            rows = fetch_all(conn, "select knob, value from config")
        else:
            with psycopg.connect(self._db.url, row_factory=dict_row) as direct:
                rows = fetch_all(direct, "select knob, value from config")
        self._cache = {r["knob"]: float(r["value"]) for r in rows}
        self._loaded_at = time.monotonic()

    def get(self, knob: str, conn: psycopg.Connection | None = None) -> float:
        if time.monotonic() - self._loaded_at > self._ttl_s or knob not in self._cache:
            self._refresh(conn)
        if knob in self._cache:
            return self._cache[knob]
        # Falling back to the seed keeps the server working on a database whose config was wiped.
        return float(CONFIG_SEEDS[knob][0])

    def invalidate(self) -> None:
        self._loaded_at = 0.0


def posts_in_last_hour(conn: psycopg.Connection, agent_id: str) -> int:
    row = fetch_one(
        conn,
        "select count(*) as n from messages where author_id = %s and created_at > now() - interval '1 hour'",
        (agent_id,),
    )
    return int(row["n"])


def votes_in_last_hour(conn: psycopg.Connection, agent_id: str) -> int:
    row = fetch_one(
        conn,
        "select count(*) as n from votes where voter_id = %s and created_at > now() - interval '1 hour'",
        (agent_id,),
    )
    return int(row["n"])


def live_counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = fetch_all(
        conn,
        "select role, count(*) as n from agents where status in ('live', 'handing_off') group by role",
    )
    counts = {"solver": 0, "supervisor": 0}
    for r in rows:
        counts[r["role"]] = int(r["n"])
    return counts


def vote_weight_for(conn: psycopg.Connection, role: str) -> float:
    """A solver vote weighs one; a supervisor vote weighs live solvers over live supervisors, so the
    two blocs always carry equal total weight. Operator votes use the supervisor weight."""
    if role == "solver":
        return 1.0
    counts = live_counts(conn)
    return float(counts["solver"]) / float(max(counts["supervisor"], 1))
