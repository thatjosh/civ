"""Thin Postgres access layer for the board server.

One pool per process. Every tool runs inside a single transaction so that a half-applied vote or a
double-claimed task cannot happen; callers get a connection from transaction() and use the helpers.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_URL = "postgresql://127.0.0.1:5432/civ"


class Db:
    def __init__(self, url: str | None = None, *, min_size: int = 1, max_size: int = 16):
        self.url = url or os.environ.get("CIV_DATABASE_URL", DEFAULT_URL)
        # dict rows everywhere, because tools build JSON responses straight from query results.
        self._pool = ConnectionPool(
            self.url, min_size=min_size, max_size=max_size, open=True,
            kwargs={"row_factory": dict_row},
        )

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Yield a connection inside a transaction; commit on exit, roll back on error."""
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn

    def close(self) -> None:
        self._pool.close()

    # Convenience helpers for one-shot queries outside a longer transaction.

    def fetch_all(self, sql: str, params: Any = None) -> list[dict]:
        with self.transaction() as conn:
            return fetch_all(conn, sql, params)

    def fetch_one(self, sql: str, params: Any = None) -> dict | None:
        with self.transaction() as conn:
            return fetch_one(conn, sql, params)

    def execute(self, sql: str, params: Any = None) -> None:
        with self.transaction() as conn:
            conn.execute(sql, params)


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def head_seq(conn: psycopg.Connection) -> int:
    row = fetch_one(conn, "select coalesce(max(id), 0) as head from messages")
    return int(row["head"]) if row else 0
