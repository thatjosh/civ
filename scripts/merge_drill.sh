#!/usr/bin/env bash
# The merge gate at small scale (M6), with no model in the loop.
#
# Three prepared proposals go onto the board as a solver would post them: a one-line prompt
# change that should merge, a change that breaks a harness test and must not, and a comment in
# the spawner, which is a protected path and must wait for the operator. A registered supervisor
# calls civ_merge on each through the tool class directly, the merger runs once over the queue,
# and the merges table shows the three outcomes. The operator then approves the protected one
# with `civ merge --approve`, the merger runs again, and the version has moved twice. At the end
# the drill's commits are reverted so the tree is as it was, because the point is the gate, not
# the changes.
#
#   scripts/merge_drill.sh
#
# Writes docs/demo/<date>-merge-drill-{summary,events}.txt. Refuses to start on a dirty harness/,
# which is the same refusal the merger itself makes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
export CIV_DATABASE_URL="${CIV_DATABASE_URL:-postgresql://127.0.0.1:5432/civ}"
export CIV_ROOT="$ROOT"
export CIV_CONTROL_DIR="${CIV_CONTROL_DIR:-$ROOT/control}"
DATE="$(date +%F)"
OUT="$ROOT/docs/demo"
PROPOSALS="$ROOT/docs/eval/proposals"
AUTHOR="sol-901"
SUPERVISOR="sup-901-r1"
mkdir -p "$OUT" "$ROOT/shared"

if [ -n "$(git status --porcelain -- harness/)" ]; then
  echo "drill: harness/ has uncommitted changes; the merge gate refuses a dirty tree, so commit or stash first" >&2
  exit 1
fi

scripts/bootstrap.sh
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

sql() { psql "$CIV_DATABASE_URL" -Atc "$1"; }
merges_table() {
  echo "merges:"
  sql "select id, proposal_id, status, array_to_string(paths, ' '), left(coalesce(detail, ''), 160), coalesce(version_before, ''), coalesce(version_after, ''), coalesce(left(commit_sha, 8), '') from merges order by id" \
    | sed 's/|/ | /g; s/^/  /'
}

SUMMARY="$OUT/$DATE-merge-drill-summary.txt"
HEAD_BEFORE="$(git rev-parse --short HEAD)"
VERSION_BEFORE="$(cat harness/VERSION 2>/dev/null || echo '(no VERSION file)')"

{
echo "merge drill, $DATE"
echo "head before: $HEAD_BEFORE; harness/VERSION before: $VERSION_BEFORE"
echo

python scripts/reset_db.py
python scripts/seed_tasks.py

# A round must be open for civ_merge, as for civ_tune: the merge cap is per round.
python - <<'PY'
from harness.board_mcp.db import Db
from harness.board_mcp.operator import Operator
db = Db()
try:
    print(f"round {Operator(db).open_round(1)} opened")
finally:
    db.close()
PY

echo
echo "seeding the three proposals as $AUTHOR:"
PROPOSAL_IDS=()
for diff in "$PROPOSALS"/01-handoff-request-wording.diff "$PROPOSALS"/02-breaks-a-test.diff "$PROPOSALS"/03-touches-spawner.diff; do
  line="$(python scripts/seed_proposal.py "$diff" --author "$AUTHOR")"
  echo "  $(basename "$diff"): $line"
  PROPOSAL_IDS+=("$(echo "$line" | sed -n 's/^proposal #\([0-9]*\).*/\1/p')")
done

echo
echo "civ_merge on each, as $SUPERVISOR:"
# The supervisor is registered once and the tool class is called directly: no model, no HTTP.
# shellcheck disable=SC2086
python - "$SUPERVISOR" ${PROPOSAL_IDS[@]+"${PROPOSAL_IDS[@]}"} <<'PY'
import json, sys
from pathlib import Path
from harness.board_mcp.auth import load_identity, resolve_token
from harness.board_mcp.db import Db
from harness.board_mcp.limits import Knobs
from harness.board_mcp.operator import Operator
from harness.board_mcp.tools.civ import CivTools

root = Path.cwd()
sup_id, proposal_ids = sys.argv[1], [int(x) for x in sys.argv[2:]]
db = Db()
try:
    op = Operator(db)
    cwd = root / "agents" / sup_id
    cwd.mkdir(parents=True, exist_ok=True)
    with db.transaction() as conn:
        identity = load_identity(conn, sup_id)
    if identity is None:
        token = op.register_agent(sup_id, "supervisor", lineage_id="S-901", cwd=str(cwd))
        with db.transaction() as conn:
            identity = resolve_token(conn, token)
    civ = CivTools(db, Knobs(db, ttl_s=0.0), root / "shared")
    for pid in proposal_ids:
        print(f"  proposal #{pid}: {json.dumps(civ.merge(identity, pid), default=str)}")
finally:
    db.close()
PY

echo
echo "merger, first pass:"
python -m harness.orchestrator.merger --once
echo
merges_table

echo
echo "civ merge --list:"
python -m control.cli.civ merge --list | sed 's/^/  /'

QUEUED_ID="$(sql "select id from merges where status = 'queued_for_operator' order by id limit 1")"
if [ -n "$QUEUED_ID" ]; then
  echo
  echo "operator approves merge #$QUEUED_ID:"
  python -m control.cli.civ merge --approve "$QUEUED_ID" | sed 's/^/  /'
  echo
  echo "merger, second pass:"
  python -m harness.orchestrator.merger --once
  echo
  merges_table
else
  echo
  echo "no merge was queued for the operator (expected one for the spawner diff)"
fi

echo
echo "harness/VERSION after: $(cat harness/VERSION 2>/dev/null || echo '(no VERSION file)')"
echo "git log after:"
git log --oneline -3 | sed 's/^/  /'

# Revert what the drill merged, newest first, so a second run starts from the same tree. The
# merges table keeps the record; the repository does not need to keep the changes.
MERGE_SHAS="$(sql "select string_agg(commit_sha, ' ' order by id desc) from merges where status = 'merged' and commit_sha is not null")"
if [ -n "$MERGE_SHAS" ]; then
  echo
  echo "reverting the drill's commits:"
  for sha in $MERGE_SHAS; do
    git revert --no-edit "$sha" >/dev/null && echo "  reverted ${sha:0:8}"
  done
fi

echo
echo "harness/VERSION final: $(cat harness/VERSION 2>/dev/null || echo '(no VERSION file)')"
echo "git log final:"
git log --oneline -3 | sed 's/^/  /'
} 2>&1 | tee "$SUMMARY"

sql "select seq, ts::time, agent_id, type, payload from events where type like 'merge.%' or type like 'harness.%' or type like 'operator.merge%' order by seq" \
  | sed 's/|/ | /g' > "$OUT/$DATE-merge-drill-events.txt"
echo
echo "drill: saved $SUMMARY and the events beside it"
