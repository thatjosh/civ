#!/usr/bin/env bash
# Restore from a base backup made by scripts/backup.sh (M7), then run the recovery step.
#
#   scripts/restore_from_backup.sh <backup dir>
#
# This script stops nothing: stop the orchestrator and the board server first (the watchdog too).
# It drops and recreates the database, restores the dump, puts shared/ back, checks every shared
# file against its hash and every reference the database makes to one, and then runs
# `civ restore --yes`, which is the step that turns a restored database into a population that
# can be resumed. The last line printed is the runbook's next step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
export CIV_DATABASE_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"
export CIV_CONTROL_DIR="${CIV_CONTROL_DIR:-$ROOT/control}"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

SRC="${1:-}"
[ -n "$SRC" ] && [ -f "$SRC/civ.dump" ] || { echo "usage: $0 <backup dir containing civ.dump>" >&2; exit 2; }

DBNAME="${CIV_DATABASE_URL##*/}"
ADMIN_URL="${CIV_DATABASE_URL%/*}/postgres"

if pgrep -f "harness.orchestrator.main|harness.board_mcp.server" >/dev/null 2>&1; then
  echo "restore: stop the orchestrator and the board server first" >&2
  exit 1
fi

echo "restore: recreating database $DBNAME"
psql "$ADMIN_URL" -q -c "select pg_terminate_backend(pid) from pg_stat_activity where datname = '$DBNAME' and pid <> pg_backend_pid()" >/dev/null
psql "$ADMIN_URL" -q -c "drop database if exists \"$DBNAME\""
psql "$ADMIN_URL" -q -c "create database \"$DBNAME\""
pg_restore --no-owner --no-privileges -d "$CIV_DATABASE_URL" "$SRC/civ.dump"

echo "restore: shared/ from the backup"
mkdir -p "$ROOT/shared"
rsync -a --delete "$SRC/shared/" "$ROOT/shared/"

echo "restore: verifying the shared store"
"$PYTHON" scripts/verify_shared.py --shared "$ROOT/shared" || { echo "restore: shared store does not verify; stopping before civ restore" >&2; exit 1; }

echo "restore: the recovery step"
"$PYTHON" -m control.cli.civ restore --yes

echo
echo "next: start the orchestrator with --resume (the watchdog does this with --restart), for example:"
echo "  python -m harness.orchestrator.main --serve-board --resume"
