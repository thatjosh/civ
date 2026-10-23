#!/usr/bin/env bash
# The out-of-stack watchdog (M7). It is not part of the civilisation: it reads the heartbeat row,
# the board's health endpoint and pg_isready every interval, logs one line per check, and pages
# the operator through a channel the civilisation does not own (a desktop notification on macOS,
# stderr elsewhere). With --restart it also brings a dead orchestrator back the only way SDK
# sessions allow: `civ restore --yes` (new epoch, stubs for every lineage caught mid-flight), then
# the orchestrator relaunched from the argument list it wrote at start, with --resume appended.
#
#   scripts/watchdog.sh [--interval 60] [--restart] [--once]
#
# Environment: CIV_DATABASE_URL, CIV_BOARD_URL, CIV_ROOT (defaults as in the other scripts).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
export CIV_DATABASE_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"
export CIV_BOARD_URL="${CIV_BOARD_URL:-http://127.0.0.1:8765}"
export CIV_ROOT="${CIV_ROOT:-$ROOT}"
export CIV_CONTROL_DIR="${CIV_CONTROL_DIR:-$ROOT/control}"

INTERVAL=60
RESTART=0
ONCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    --restart) RESTART=1; shift ;;
    --once) ONCE=1; shift ;;
    *) echo "watchdog: unknown argument $1" >&2; exit 2 ;;
  esac
done

LOG="$CIV_CONTROL_DIR/watchdog.log"
ARGS_FILE="$CIV_CONTROL_DIR/orchestrator.args"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

page() {
  # The channel the civilisation does not own. A failed notification must not stop the check.
  local text="$1"
  # Double quotes are dropped rather than escaped: bash 3.2 cannot parse a quote inside a
  # substitution that sits inside a double-quoted string.
  local safe="${text//\"/}"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$safe\" with title \"civ watchdog\"" >/dev/null 2>&1 || true
  fi
  echo "watchdog: PAGE $text" >&2
}

heartbeat_age() {
  # Seconds since the orchestrator's last beat by the database clock, or "none".
  psql "$CIV_DATABASE_URL" -Atc "select coalesce(
      (select round(extract(epoch from (now() - (value->>'at')::timestamptz)))::text
         from control_state where key = 'heartbeat' and value ? 'at'),
      'none')" 2>/dev/null || echo "db-error"
}

health() {
  # "ok", "board_down" (an injected outage, which is healthy for our purposes) or "down".
  local body
  body="$(curl -fsS --max-time 3 "$CIV_BOARD_URL/health" 2>/dev/null)" || { echo "down"; return; }
  case "$body" in
    *board_down*) echo "board_down" ;;
    *) echo "ok" ;;
  esac
}

orchestrator_running() {
  pgrep -f "harness.orchestrator.main" >/dev/null 2>&1
}

restart_orchestrator() {
  if [ ! -f "$ARGS_FILE" ]; then
    log "watchdog: cannot restart, $ARGS_FILE is missing"
    return 1
  fi
  log "watchdog: restoring before restart"
  "$PYTHON" -m control.cli.civ restore --yes >>"$LOG" 2>&1 || { log "watchdog: civ restore failed"; return 1; }
  # One argument per line, as the orchestrator wrote them; --resume picks up the stubbed lineages.
  local args=()
  while IFS= read -r line; do
    [ -n "$line" ] && args+=("$line")
  done <"$ARGS_FILE"
  # Drop an earlier --resume so the flag is not doubled across repeated restarts.
  local cleaned=()
  for a in ${args[@]+"${args[@]}"}; do
    [ "$a" = "--resume" ] || cleaned+=("$a")
  done
  nohup "$PYTHON" -m harness.orchestrator.main ${cleaned[@]+"${cleaned[@]}"} --resume >>"$ROOT/shared/orchestrator.log" 2>&1 &
  log "watchdog: restarted orchestrator (pid $!)"
}

STALE_AFTER=$((INTERVAL * 2))
while :; do
  age="$(heartbeat_age)"
  hb="$(health)"
  if pg_isready -q -h 127.0.0.1 2>/dev/null; then pg="ok"; else pg="down"; fi
  log "check heartbeat_age=${age}s health=${hb} postgres=${pg}"

  problems=()
  [ "$pg" = "ok" ] || problems+=("postgres is not accepting connections")
  [ "$hb" = "down" ] && problems+=("board health endpoint is not answering")
  case "$age" in
    none) problems+=("no heartbeat has ever been written") ;;
    db-error) problems+=("could not read the heartbeat") ;;
    *) [ "$age" -gt "$STALE_AFTER" ] 2>/dev/null && problems+=("heartbeat is ${age}s old (limit ${STALE_AFTER}s)") ;;
  esac
  if [ "${#problems[@]}" -gt 0 ]; then
    for p in "${problems[@]}"; do log "problem: $p"; done
    page "${problems[0]}"
    if [ "$RESTART" = "1" ] && [ "$pg" = "ok" ] && ! orchestrator_running; then
      # Only when the heartbeat is stale or absent and no orchestrator is alive: a slow beat from
      # a living process is a page, not a restart.
      case "$age" in
        none|db-error) stale=1 ;;
        *) if [ "$age" -gt "$STALE_AFTER" ] 2>/dev/null; then stale=1; else stale=0; fi ;;
      esac
      if [ "$stale" = "1" ]; then restart_orchestrator; fi
    fi
  fi
  [ "$ONCE" = "1" ] && break
  sleep "$INTERVAL"
done
