#!/usr/bin/env bash
# Bring a fresh checkout to the point where the board server can start:
# Postgres running, both databases present and migrated, runtime dirs in place.
# Idempotent, so demo.sh calls it every time.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PG_BIN="/opt/homebrew/opt/postgresql@17/bin"
PG_DATA="/opt/homebrew/var/postgresql@17"
export PATH="$PG_BIN:$PATH"

DB_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"
TEST_DB_URL="${CIV_TEST_DATABASE_URL:-postgresql://127.0.0.1:5432/civ_test}"
# The merge gate (M6) runs the harness tests on its own database so a merge in flight cannot
# collide with a developer's test run on civ_test.
MERGE_TEST_DB_URL="${CIV_MERGE_TEST_DATABASE_URL:-postgresql://127.0.0.1:5432/civ_merge_test}"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "bootstrap: no .venv found; create it with 'uv venv --python 3.12 .venv && uv pip install -e .[dev]'" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

# Postgres was started with pg_ctl rather than brew services (the launchd job
# did not come up on this machine), so we check and start it the same way.
if ! pg_isready -h 127.0.0.1 -q; then
  echo "bootstrap: starting postgres"
  pg_ctl -D "$PG_DATA" -l "$PG_DATA/server.log" start >/dev/null
  for _ in $(seq 1 20); do pg_isready -h 127.0.0.1 -q && break; sleep 0.5; done
  pg_isready -h 127.0.0.1 -q || { echo "bootstrap: postgres did not start" >&2; exit 1; }
fi

for db in civ civ_test civ_merge_test; do
  if ! psql -h 127.0.0.1 -d postgres -tAc "select 1 from pg_database where datname='$db'" | grep -q 1; then
    echo "bootstrap: creating database $db"
    createdb -h 127.0.0.1 "$db"
  fi
done

# migrate.py is WS-A's; it reads CIV_DATABASE_URL, so run it once per database.
for url in "$DB_URL" "$TEST_DB_URL" "$MERGE_TEST_DB_URL"; do
  CIV_DATABASE_URL="$url" python scripts/migrate.py
done

mkdir -p "$ROOT/agents" "$ROOT/shared"
echo "bootstrap: ready ($DB_URL)"
