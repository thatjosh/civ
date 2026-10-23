#!/usr/bin/env bash
# A base backup (M7): the database, the shared store and the operator's control files, with a
# manifest. Everything durable is in Postgres and shared/ (the per-agent directories are a cache
# a lineage rebuilds from its last handoff), so this is the whole of what a restore needs.
#
#   scripts/backup.sh [dest]        default dest: backups/<YYYYmmdd-HHMMSS>
#
# Write-ahead log archiving is not configured here (design doc: nightly base backup plus WAL);
# the loss window is therefore everything since the last run of this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
export CIV_DATABASE_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"

DEST="${1:-$ROOT/backups/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$DEST/shared" "$DEST/control"

pg_dump -Fc "$CIV_DATABASE_URL" >"$DEST/civ.dump"
rsync -a --delete "$ROOT/shared/" "$DEST/shared/"
cp "$ROOT"/control/*.toml "$DEST/control/"

{
  echo "civ backup $(date '+%F %T')"
  echo "database: $CIV_DATABASE_URL"
  echo "git head: $(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "harness version: $(cat "$ROOT/harness/VERSION" 2>/dev/null || echo unknown)"
  echo "epoch: $(psql "$CIV_DATABASE_URL" -Atc "select value from control_state where key = 'epoch'" 2>/dev/null || echo unknown)"
  echo "last event: $(psql "$CIV_DATABASE_URL" -Atc "select max(ts) from events" 2>/dev/null || echo unknown)"
  echo "dump bytes: $(wc -c <"$DEST/civ.dump" | tr -d ' ')"
  echo "shared files: $(find "$DEST/shared" -type f | wc -l | tr -d ' ')"
  echo "shared bytes: $(du -sk "$DEST/shared" | cut -f1)k"
} >"$DEST/manifest.txt"

cat "$DEST/manifest.txt"
echo "backup: written to $DEST"
