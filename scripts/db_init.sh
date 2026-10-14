#!/usr/bin/env bash
# Create the civ and civ_test databases if missing and bring both up to the latest migration.
# Safe to re-run. Assumes Postgres 17 from Homebrew is running on 127.0.0.1:5432.
set -euo pipefail

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PGHOST="${PGHOST:-127.0.0.1}"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"

if ! pg_isready -h "$PGHOST" -q; then
  echo "postgres is not accepting connections on $PGHOST:5432; start it with:" >&2
  echo "  pg_ctl -D /opt/homebrew/var/postgresql@17 -l /opt/homebrew/var/postgresql@17/server.log start" >&2
  exit 1
fi

for db in civ civ_test; do
  if ! psql -h "$PGHOST" -d postgres -tAc "select 1 from pg_database where datname = '$db'" | grep -q 1; then
    createdb -h "$PGHOST" "$db"
    echo "created database $db"
  fi
  "$PYTHON" "$REPO_ROOT/scripts/migrate.py" --url "postgresql://$PGHOST:5432/$db"
done
