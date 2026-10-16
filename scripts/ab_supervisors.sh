#!/usr/bin/env bash
# The M4 machinery demo: the Hollowmere pack twice under the same injected fault, once without
# supervisors (arm A) and once with one supervisor on a fifteen-second round (arm B).
#
# The fault is the old kill rule: kill_grace_turns preset to 0, so a solver that crosses kill_pct
# is cut the moment it does, with a 30k-token window to make that happen within a minute. The
# run is too short for a tune to show in the SLOs; what the two arms show is the round machinery
# end to end (round rows, the brief, a tune accepted under the round rules, a report, a refused
# second tune) and what supervision costs as a share of the bill.
#
#   SUPERVISOR_MODEL=claude-opus-5-5 scripts/ab_supervisors.sh
#
# Writes docs/demo/<date>-ab-<arm>-{run-summary,board-transcript,events,metrics.json} and prints
# a comparison table. Every flag here passes through scripts/demo.sh to the orchestrator.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
export CIV_DATABASE_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"
SUPERVISOR_MODEL="${SUPERVISOR_MODEL:-claude-opus-5-5}"
DATE="$(date +%F)"
OUT="$ROOT/docs/demo"
mkdir -p "$OUT"

# Shared by both arms. --preset kill_grace_turns=0 is two tokens and demo.sh appends unknown
# flags one token at a time in order, so the value stays attached to its flag.
COMMON=(--solvers 3 --model claude-haiku-5-5 --context-window 30000 --max-generations 4 --preset kill_grace_turns=0)
ARM_A=(--supervisors 0)
ARM_B=(--supervisors 1 --supervisor-interval 15 --supervisor-model "$SUPERVISOR_MODEL" --supervisor-max-turns 15)

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

collect_metrics() {
  # Everything the comparison needs is in the database at the end of an arm, before the next
  # arm's demo.sh resets it. Costs come from the agent.ended events joined to the agent's role.
  python - "$1" <<'PY'
import json, os, sys
import psycopg

arm = sys.argv[1]
url = os.environ["CIV_DATABASE_URL"]
with psycopg.connect(url) as conn:
    done, total, mean = conn.execute(
        "select count(*) filter (where status = 'done'), count(*), avg(score) filter (where status = 'done') from tasks"
    ).fetchone()
    count = lambda t: conn.execute("select count(*) from events where type = %s", (t,)).fetchone()[0]
    costs = {role: float(c) for role, c in conn.execute(
        "select a.role, coalesce(sum((e.payload->>'cost_usd')::float), 0) from events e "
        "join agents a on a.id = e.agent_id where e.type = 'agent.ended' group by a.role"
    ).fetchall()}
    try:
        rounds = [{"number": n, "tuned_knob": k, "summary": s} for n, k, s in conn.execute(
            "select number, tuned_knob, summary from rounds order by number").fetchall()]
    except psycopg.Error:
        rounds = []
    tunes = [p for (p,) in conn.execute("select payload from events where type = 'tool.civ_tune' order by seq").fetchall()]
    refused_tunes = [p for (p,) in conn.execute(
        "select payload from events where type = 'tool.refused' and payload->>'tool' like '%tune%' order by seq").fetchall()]
solver_cost = costs.get("solver", 0.0)
supervisor_cost = costs.get("supervisor", 0.0)
total_cost = solver_cost + supervisor_cost
print(json.dumps({
    "arm": arm,
    "tasks_done": done, "tasks_total": total, "mean_score": float(mean) if mean is not None else None,
    "handoffs": count("agent.handed_off"), "stubs": count("agent.stub_written"), "kills": count("agent.killed"),
    "solver_cost_usd": solver_cost, "supervisor_cost_usd": supervisor_cost, "total_cost_usd": total_cost,
    "supervisor_cost_share": (supervisor_cost / total_cost) if total_cost else 0.0,
    "rounds": rounds, "tunes": tunes, "refused_tunes": refused_tunes,
}, indent=2, default=str))
PY
}

run_arm() {
  local arm="$1"; shift
  echo
  echo "=== arm $arm: scripts/demo.sh ${COMMON[*]} $* ==="
  # A lineage that ends in error makes the orchestrator exit 1 and demo.sh stop before its dump;
  # the artefacts below are still worth keeping, so the exit code is reported, not fatal.
  scripts/demo.sh "${COMMON[@]}" "$@" || echo "arm $arm: demo.sh exited $?"
  cp "$ROOT/shared/orchestrator.log" "$OUT/$DATE-ab-$arm-run-summary.txt"
  python scripts/board_dump.py > "$OUT/$DATE-ab-$arm-board-transcript.txt"
  psql "$CIV_DATABASE_URL" -Atc "select seq, ts::time, agent_id, type, payload from events order by seq" \
    | sed 's/|/ | /g' > "$OUT/$DATE-ab-$arm-events.txt"
  collect_metrics "$arm" > "$OUT/$DATE-ab-$arm-metrics.json"
  echo "arm $arm: saved $OUT/$DATE-ab-$arm-{run-summary,board-transcript,events}.txt and metrics.json"
}

run_arm A "${ARM_A[@]}"
run_arm B "${ARM_B[@]}"

echo
python3 - "$OUT/$DATE-ab-A-metrics.json" "$OUT/$DATE-ab-B-metrics.json" <<'PY'
import json, sys

a, b = (json.load(open(p)) for p in sys.argv[1:3])
rows = [
    ("tasks done", lambda m: f"{m['tasks_done']}/{m['tasks_total']}"),
    ("mean score", lambda m: f"{m['mean_score']:.3f}" if m["mean_score"] is not None else "n/a"),
    ("handoffs", lambda m: str(m["handoffs"])),
    ("stubs", lambda m: str(m["stubs"])),
    ("kills", lambda m: str(m["kills"])),
    ("total cost $", lambda m: f"{m['total_cost_usd']:.4f}"),
    ("supervisor cost $", lambda m: f"{m['supervisor_cost_usd']:.4f}"),
    ("supervisor share", lambda m: f"{100 * m['supervisor_cost_share']:.1f}%"),
    ("rounds", lambda m: str(len(m["rounds"]))),
    ("tunes accepted", lambda m: str(len(m["tunes"]))),
    ("tunes refused", lambda m: str(len(m["refused_tunes"]))),
]
print(f"{'metric':<18} {'A: no supervisors':>18} {'B: one supervisor':>18}")
for label, cell in rows:
    print(f"{label:<18} {cell(a):>18} {cell(b):>18}")
for r in b["rounds"]:
    s = r["summary"] or {}
    print(f"round {r['number']}: tuned {r['tuned_knob'] or 'nothing'}, reports {s.get('reports', '?')}, pings {s.get('pings', '?')}, cost ${float(s.get('cost_usd') or 0):.4f}")
PY
