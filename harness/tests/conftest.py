"""Shared fixtures: a migrated civ_test database, truncated between tests, and the service objects.

The schema is rebuilt from scratch once per session (drop and re-migrate) so that a schema change
during development never leaves tests running against a stale table. Each test then starts from
empty tables with the config seeds in place.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from harness.board_mcp.auth import AgentIdentity, resolve_token  # noqa: E402
from harness.board_mcp.db import Db  # noqa: E402
from harness.board_mcp.limits import Knobs  # noqa: E402
from harness.board_mcp.operator import Operator  # noqa: E402
from harness.board_mcp.seed import seed_config  # noqa: E402
from harness.board_mcp.tools.board import BoardTools  # noqa: E402
from harness.board_mcp.tools.civ import CivTools  # noqa: E402
from harness.board_mcp.tools.task import TaskTools  # noqa: E402

TEST_URL = os.environ.get("CIV_TEST_DATABASE_URL", "postgresql://127.0.0.1:5432/civ_test")

DATA_TABLES = (
    "operator_acks", "operator_pins", "broadcast_seen", "session_reads", "handoffs",
    "merges", "tune_proposals", "rounds", "events", "votes", "tasks", "messages", "agents",
)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    import migrate  # scripts/migrate.py

    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("drop schema public cascade; create schema public")
    migrate.apply(TEST_URL, log=lambda *_: None)
    return TEST_URL


@pytest.fixture(scope="session")
def db(migrated_url) -> Db:
    # A generous pool so the concurrency test can hold many connections at once.
    pool = Db(migrated_url, min_size=1, max_size=60)
    yield pool
    pool.close()


@pytest.fixture(autouse=True)
def clean_tables(db: Db):
    with db.transaction() as conn:
        conn.execute(f"truncate {', '.join(DATA_TABLES)} restart identity cascade")
        conn.execute("truncate config")
        seed_config(conn)
        # control_state is truncated and re-seeded rather than listed in DATA_TABLES, because its
        # three seed rows (epoch, paused, inbox_last_seq) are what a fresh database starts with.
        conn.execute("truncate control_state")
        conn.execute("insert into control_state (key, value) values ('epoch', '1'), ('paused', 'false'), ('inbox_last_seq', '0')")
    yield


@pytest.fixture
def op(db) -> Operator:
    return Operator(db)


@pytest.fixture
def shared_dir(tmp_path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    return d


@pytest.fixture
def knobs(db) -> Knobs:
    # ttl 0 so a tune inside a test is visible to the next call without waiting.
    return Knobs(db, ttl_s=0.0)


@pytest.fixture
def board(db, shared_dir, knobs) -> BoardTools:
    return BoardTools(db, shared_dir, knobs)


@pytest.fixture
def fake_verifier():
    calls: list[tuple[str, str]] = []

    def verify(task_id: str, path: str):
        calls.append((task_id, path))
        return 0.75, f"checked {task_id}"

    verify.calls = calls
    return verify


@pytest.fixture
def tasks(db, shared_dir, knobs, fake_verifier) -> TaskTools:
    return TaskTools(db, shared_dir, knobs, fake_verifier)


@pytest.fixture
def civ(db, knobs, shared_dir) -> CivTools:
    return CivTools(db, knobs, shared_dir)


@pytest.fixture
def make_agent(db, op, tmp_path):
    """Register an agent with its own directory and return (identity, token)."""
    counter = {"n": 0}

    def _make(role: str = "solver", agent_id: str | None = None, lineage: str | None = None,
              generation: int = 1) -> tuple[AgentIdentity, str]:
        counter["n"] += 1
        agent_id = agent_id or f"{role[:3]}-{counter['n']:03d}"
        cwd = tmp_path / "agents" / agent_id
        (cwd / "work").mkdir(parents=True)
        token = op.register_agent(agent_id, role, lineage or f"L-{counter['n']:03d}", str(cwd), generation)
        with db.transaction() as conn:
            identity = resolve_token(conn, token)
        return identity, token

    return _make


@pytest.fixture
def solver(make_agent) -> AgentIdentity:
    return make_agent("solver")[0]


@pytest.fixture
def other(make_agent) -> AgentIdentity:
    return make_agent("solver")[0]


@pytest.fixture
def supervisor(make_agent) -> AgentIdentity:
    return make_agent("supervisor")[0]


@pytest.fixture
def task(op) -> dict:
    thread_id = op.create_task("T-001", "Count the ledger", "Add up the 1852 ledger", priority_base=1.0)
    return {"id": "T-001", "thread_id": thread_id}
