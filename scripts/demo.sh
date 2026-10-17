#!/usr/bin/env bash
# The M2 demo: a clean board, the Hollowmere tasks, the board server, N solvers
# working the queue through the board, then the transcript and the bill.
#
#   scripts/demo.sh [--solvers 3] [--model claude-opus-5-5] [--max-turns 40]
#
# Every flag after the script name is passed through to the orchestrator.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SOLVERS=3
MAX_TURNS=40
PASSTHROUGH=()
while [ $# -gt 0 ]; do
  case "$1" in
    --solvers) SOLVERS="$2"; shift 2 ;;
    --max-turns) MAX_TURNS="$2"; shift 2 ;;
    *) PASSTHROUGH+=("$1"); shift ;;
  esac
done

export CIV_DATABASE_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"
export CIV_BOARD_URL="${CIV_BOARD_URL:-http://127.0.0.1:8765}"
export CIV_ROOT="$ROOT"
# The operator's files (M5): the orchestrator's governor and the board server's tune check both read them.
export CIV_CONTROL_DIR="${CIV_CONTROL_DIR:-$ROOT/control}"

scripts/bootstrap.sh
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

python scripts/reset_db.py
python scripts/seed_tasks.py

LOG="$ROOT/shared/board-server.log"
python -m harness.board_mcp.server >"$LOG" 2>&1 &
SERVER_PID=$!
# Whatever happens below, the server must not outlive the demo.
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -fsS "$CIV_BOARD_URL/health" >/dev/null 2>&1; then break; fi
  sleep 0.25
done
curl -fsS "$CIV_BOARD_URL/health" >/dev/null || { echo "demo: board server did not come up, see $LOG" >&2; exit 1; }
echo "demo: board server up at $CIV_BOARD_URL (pid $SERVER_PID)"

START=$(date +%s)
# macOS bash 3.2 treats an empty array as unbound under set -u, hence the guarded expansion
python -m harness.orchestrator.main --solvers "$SOLVERS" --max-turns "$MAX_TURNS" --board-url "$CIV_BOARD_URL" --control-dir "$CIV_CONTROL_DIR" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} \
  | tee "$ROOT/shared/orchestrator.log"
END=$(date +%s)

echo
python scripts/board_dump.py
echo
echo "demo: wall time $((END - START))s, solvers $SOLVERS"
# The orchestrator prints the cost table; repeat the total line so it is the last thing on screen.
grep -i "total cost" "$ROOT/shared/orchestrator.log" || true
