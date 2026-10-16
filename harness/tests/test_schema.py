"""M0: the schema exists, the seeds match, and migrations are idempotent."""

from pathlib import Path

from harness.board_mcp.seed import CONFIG_SEEDS

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "agents", "messages", "votes", "tasks", "config", "events", "handoffs",
    "session_reads", "broadcast_seen", "operator_pins", "operator_acks", "schema_migrations",
    "rounds", "tune_proposals",
}


def test_all_tables_exist(db):
    rows = db.fetch_all("select table_name from information_schema.tables where table_schema = 'public'")
    assert EXPECTED_TABLES <= {r["table_name"] for r in rows}


def test_migration_is_recorded_and_idempotent(migrated_url):
    import migrate

    assert migrate.apply(migrated_url, log=lambda *_: None) == []
    import psycopg

    with psycopg.connect(migrated_url) as conn:
        names = [r[0] for r in conn.execute("select name from schema_migrations order by name")]
    assert names == ["0001_init.sql", "0002_kill_grace_turns.sql", "0003_supervisor_rounds.sql"]


def test_schema_sql_is_embedded_in_its_migrations():
    # schema.sql stays the one canonical copy: the part before the marker is applied verbatim by
    # 0001, and each later block by the migration its marker names.
    schema = (REPO_ROOT / "harness/board_mcp/schema.sql").read_text()
    migrations = REPO_ROOT / "harness/board_mcp/migrations"
    first, m4 = schema.split("-- ---- migration 0003 ----")
    assert first.strip() in (migrations / "0001_init.sql").read_text()
    body = m4.split("\n\n", 1)[1]  # drop the marker's own comment lines
    assert body.strip() in (migrations / "0003_supervisor_rounds.sql").read_text()


def test_config_seeds_match_python_copy(db):
    rows = db.fetch_all("select knob, value, min, max, step from config")
    seeded = {r["knob"]: (r["value"], r["min"], r["max"], r["step"]) for r in rows}
    expected = {k: tuple(float(x) for x in v) for k, v in CONFIG_SEEDS.items()}
    assert seeded == expected


def test_enums_are_enforced(db, op):
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into messages (author_id, kind, body) values ('operator', 'gossip', 'x')")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into agents (id, role, lineage_id, cwd, token) values ('x', 'god', 'L', '/tmp', 't')")
