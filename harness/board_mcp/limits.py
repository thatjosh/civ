"""Soft knobs and rate limits.

Knobs are read from the config table with a short cache, because every tool call consults several
of them and a supervisor's tune should take effect within seconds, not require a restart.
Rate limits count rows rather than events so that the limit is exactly what the agent did.
"""

from __future__ import annotations

import json
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

    _SELECT = "select knob, value, (expires_at is not null and expires_at < now()) as expired from config"

    def _refresh(self, conn: psycopg.Connection | None) -> None:
        # Refresh on the caller's connection when there is one; otherwise open a short direct
        # connection rather than borrowing a second pooled one, because a tool already holding a
        # pooled connection would deadlock the pool under load.
        if conn is not None:
            self._cache = self._load(conn)
        else:
            with psycopg.connect(self._db.url, row_factory=dict_row) as direct:
                self._cache = self._load(direct)
        self._loaded_at = time.monotonic()

    def _load(self, conn: psycopg.Connection) -> dict[str, float]:
        rows = fetch_all(conn, self._SELECT)
        expired = [r["knob"] for r in rows if r["expired"]]
        if expired:
            self._revert(conn, expired)
        # A tune past its TTL reverts to the seed, so a supervisor's experiment cannot outlive the
        # round that justified it. The revert is written back so dashboards and civ_status agree.
        return {r["knob"]: float(CONFIG_SEEDS[r["knob"]][0]) if r["expired"] and r["knob"] in CONFIG_SEEDS
                else float(r["value"]) for r in rows}

    @staticmethod
    def _revert(conn: psycopg.Connection, knobs: list[str]) -> None:
        for knob in knobs:
            if knob not in CONFIG_SEEDS:
                continue
            seed = CONFIG_SEEDS[knob][0]
            # set_at is left alone on purpose: a TTL revert is logged but starts no cooldown.
            conn.execute(
                "update config set value = %s, set_by = null, reason = null, ttl_s = null, expires_at = null where knob = %s",
                (seed, knob),
            )
            conn.execute(
                "insert into events (agent_id, type, payload) values (null, 'knob.reverted', %s)",
                (json.dumps({"knob": knob, "to": seed}),),
            )

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
    two blocs always carry equal total weight. Operator votes use the supervisor weight, floored at
    one so a steer cast before any agent is live still counts for something."""
    if role == "solver":
        return 1.0
    counts = live_counts(conn)
    weight = float(counts["solver"]) / float(max(counts["supervisor"], 1))
    # Floored at one for supervisors and the operator alike: a final-round supervisor voting after
    # the solvers have gone would otherwise cast weight zero, and a vote that cannot move the board
    # is a vote nobody will bother to reason about.
    return max(weight, 1.0)
