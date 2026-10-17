#!/usr/bin/env bash
# The recovery runbook at small scale (M5): lose the orchestrator mid-run, restore, resume.
#
# A clean board, three Haiku solvers at a 30k-token window (so handoffs happen within a minute)
# with one supervisor on a fifteen-second round. About 25 seconds in, the orchestrator is killed
# with SIGKILL and the sessions it leaves behind are killed too, which is what a VM loss looks
# like from the database's side: agents still marked live, leases still held. `civ restore`
# then bumps the epoch, writes a stub for every lineage caught mid-flight, releases the leases
# and announces itself, and the orchestrator is restarted with --resume, which spawns generation
# N+1 for each of those lineages from its stub. The report at the end is what the runbook asks
# the operator to check.
#
#   KILL_AFTER_S=25 scripts/restore_drill.sh
#
# Writes docs/demo/<date>-restore-drill-{events,board-transcript,summary}.txt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
export CIV_DATABASE_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"
export CIV_BOARD_URL="${CIV_BOARD_URL:-http://127.0.0.1:8765}"
export CIV_ROOT="$ROOT"
export CIV_CONTROL_DIR="${CIV_CONTROL_DIR:-$ROOT/control}"
KILL_AFTER_S="${KILL_AFTER_S:-25}"
MODEL="${MODEL:-claude-haiku-5-5}"
DATE="$(date +%F)"
OUT="$ROOT/docs/demo"
mkdir -p "$OUT" "$ROOT/shared"

scripts/bootstrap.sh
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

python scripts/reset_db.py
python scripts/seed_tasks.py

SERVER_LOG="$ROOT/shared/board-server.log"
python -m harness.board_mcp.server >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
ORCH_PID=""
# Whatever happens below, neither the server nor a half-dead orchestrator may outlive the drill.
trap 'kill "$SERVER_PID" 2>/dev/null || true; [ -n "$ORCH_PID" ] && kill -9 "$ORCH_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -fsS "$CIV_BOARD_URL/health" >/dev/null 2>&1; then break; fi
  sleep 0.25
done
curl -fsS "$CIV_BOARD_URL/health" >/dev/null || { echo "drill: board server did not come up, see $SERVER_LOG" >&2; exit 1; }

ORCH_ARGS=(--solvers 3 --model "$MODEL" --context-window 30000 --max-generations 4
           --supervisors 1 --supervisor-interval 15 --supervisor-model "$MODEL" --supervisor-max-turns 15
           --board-url "$CIV_BOARD_URL" --control-dir "$CIV_CONTROL_DIR")

sql() { psql "$CIV_DATABASE_URL" -Atc "$1"; }

echo "drill: half one, the run that will be lost (killed after ${KILL_AFTER_S}s)"
T0=$(date +%s)
python -m harness.orchestrator.main "${ORCH_ARGS[@]}" >"$ROOT/shared/orchestrator-half1.log" 2>&1 &
ORCH_PID=$!
sleep "$KILL_AFTER_S"
# SIGKILL, not SIGTERM: a VM loss gives nobody a chance to clean up. The SDK's CLI processes
# survive their parent, so they are killed by name next.
kill -9 "$ORCH_PID" 2>/dev/null || true
wait "$ORCH_PID" 2>/dev/null || true
ORCH_PID=""
pkill -f "claude --output-format stream-json" 2>/dev/null || true
T1=$(date +%s)
LAST_EVENT="$(sql "select to_char(max(ts), 'HH24:MI:SS') from events")"
LIVE_AT_LOSS="$(sql "select string_agg(id || ':' || status, ', ' order by id) from agents where status in ('live', 'handing_off')")"
LEASED_AT_LOSS="$(sql "select string_agg(id || ' by ' || lease_owner, ', ' order by id) from tasks where status = 'leased'")"
echo "drill: killed at $(date +%T); last event $LAST_EVENT; live agents: ${LIVE_AT_LOSS:-none}; leased: ${LEASED_AT_LOSS:-none}"

echo "drill: civ restore"
RESTORE_OUT="$(python -m control.cli.civ restore --yes 2>&1 | tee /dev/stderr)"
T2=$(date +%s)

echo "drill: half two, the resumed run"
python -m harness.orchestrator.main "${ORCH_ARGS[@]}" --resume 2>&1 | tee "$ROOT/shared/orchestrator-half2.log" || echo "drill: resumed orchestrator exited $?"
T3=$(date +%s)

# --- the report the runbook asks for -------------------------------------------------------------
EPOCHS="$(sql "select string_agg('epoch ' || epoch || ': ' || n || ' events', '; ' order by epoch) from (select epoch, count(*) as n from events group by epoch) e" 2>/dev/null || echo "events carry no epoch column yet")"
RESUMED="$(sql "select string_agg(lineage_id || ' ' || from_agent || ' -> ' || coalesce(to_agent, '(none)') || ' gen ' || (generation + 1) || case when stub then ' from a stub' else ' from a handoff' end, E'\n' order by lineage_id)
                from handoffs where created_at between to_timestamp($T1) and to_timestamp($T2 + 1)")"
TASKS="$(sql "select count(*) filter (where status = 'done') || ' of ' || count(*) || ' done, mean score ' || coalesce(round(avg(score) filter (where status = 'done')::numeric, 3)::text, 'n/a') from tasks")"
BROADCAST="$(sql "select body from messages where kind = 'broadcast' and body like 'Restored at%' order by id desc limit 1")"
AGENTS="$(sql "select string_agg(id || ' gen ' || generation || ' ' || status || ' epoch ' || epoch, E'\n' order by spawned_at) from agents" 2>/dev/null || sql "select string_agg(id || ' gen ' || generation || ' ' || status, E'\n' order by spawned_at) from agents")"

SUMMARY="$OUT/$DATE-restore-drill-summary.txt"
{
  echo "restore drill, $DATE, model $MODEL, killed after ${KILL_AFTER_S}s"
  echo
  echo "half one (lost): $((T1 - T0))s wall; last event at $LAST_EVENT"
  echo "live at the loss: ${LIVE_AT_LOSS:-none}"
  echo "leased at the loss: ${LEASED_AT_LOSS:-none}"
  echo
  echo "restore: $((T2 - T1))s wall"
  echo "$RESTORE_OUT"
  echo
  echo "half two (resumed): $((T3 - T2))s wall"
  echo
  echo "epochs seen in events: $EPOCHS"
  echo
  echo "lineages resumed (handoffs written by the restore):"
  echo "${RESUMED:-none}"
  echo
  echo "agents:"
  echo "$AGENTS"
  echo
  echo "tasks: $TASKS"
  echo
  echo "restore broadcast: ${BROADCAST:-none}"
} | tee "$SUMMARY"

psql "$CIV_DATABASE_URL" -Atc "select seq, ts::time, agent_id, type, payload from events order by seq" \
  | sed 's/|/ | /g' > "$OUT/$DATE-restore-drill-events.txt"
python scripts/board_dump.py > "$OUT/$DATE-restore-drill-board-transcript.txt"
echo
echo "drill: saved $SUMMARY and the events and board transcript beside it"
