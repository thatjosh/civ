"""Post a prepared diff onto the board as a solver's proposal, the way an agent would.

The merge drill (scripts/merge_drill.sh) needs proposals that exist before any model runs, so
this script plays the solver: it registers one if needed, drops the diff into that agent's own
directory under proposals/, and posts a kind=proposal message through BoardTools.post with the
diff as the ref. Going through the tool rather than inserting a row means the server's own path
runs: the ref is resolved under the agent's cwd, copied into shared/ by content hash, and the
message carries the shared ref that civ_merge later reads.

    python scripts/seed_proposal.py docs/eval/proposals/01-handoff-request-wording.diff --author sol-901

Prints `proposal #<id> ref <shared ref>`. The body names the file the diff touches and the
reason from the .md beside the diff (first paragraph), so a supervisor reading the proposal sees
what the author claims before opening the ref.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"


def paths_touched(diff_text: str) -> list[str]:
    """The b/ side of every file header, in order: what the proposal claims to change."""
    return [m.group(1) for m in re.finditer(r"^\+\+\+ b/(\S+)", diff_text, re.M)]


def reason_for(diff: Path) -> str:
    """The first paragraph of the .md beside the diff, or a plain fallback when there is none."""
    note = diff.with_suffix(".md")
    if not note.exists():
        return "No note accompanies this diff."
    paragraphs = [p.strip() for p in note.read_text(encoding="utf-8").split("\n\n") if p.strip()]
    body = [p for p in paragraphs if not p.startswith("#")]
    return " ".join(body[0].split()) if body else "No note accompanies this diff."


def proposal_body(diff: Path, diff_text: str) -> str:
    files = ", ".join(paths_touched(diff_text)) or "(no file headers)"
    return f"Proposal: change {files}.\n\n{reason_for(diff)}\n\nThe diff is attached as a ref; apply it with civ_merge."


def ensure_solver(op, db, agent_id: str, root: Path):
    """Register the solver once; later runs reuse the same row and token."""
    from harness.board_mcp.auth import load_identity

    cwd = root / "agents" / agent_id
    (cwd / "work").mkdir(parents=True, exist_ok=True)
    (cwd / "proposals").mkdir(parents=True, exist_ok=True)
    with db.transaction() as conn:
        identity = load_identity(conn, agent_id)
    if identity is None:
        # L-9xx keeps drill agents clear of the demo's L-001 to L-003 slots.
        digits = re.sub(r"\D", "", agent_id)[-3:] or "901"
        op.register_agent(agent_id, "solver", lineage_id=f"L-{digits}", cwd=str(cwd))
        with db.transaction() as conn:
            identity = load_identity(conn, agent_id)
    return identity, cwd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("diff", type=Path)
    parser.add_argument("--author", default="sol-901")
    parser.add_argument("--db-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("CIV_ROOT", ROOT)))
    args = parser.parse_args(argv)
    if not args.diff.exists():
        print(f"diff not found: {args.diff}", file=sys.stderr)
        return 1

    # Imported here so --help works without the harness or a database.
    from harness.board_mcp.db import Db
    from harness.board_mcp.limits import Knobs
    from harness.board_mcp.operator import Operator
    from harness.board_mcp.tools.board import BoardTools

    db = Db(args.db_url)
    try:
        op = Operator(db)
        identity, cwd = ensure_solver(op, db, args.author, args.root.resolve())
        diff_text = args.diff.read_text(encoding="utf-8")
        local = cwd / "proposals" / args.diff.name
        shutil.copyfile(args.diff, local)
        board = BoardTools(db, args.root.resolve() / "shared", Knobs(db))
        # The ref is relative to the agent's directory, which is how an agent would name it.
        result = board.post(identity, "proposal", proposal_body(args.diff, diff_text),
                            refs=[str(local.relative_to(cwd))])
        if not result.get("ok"):
            print(f"proposal refused: {result.get('error')}", file=sys.stderr)
            return 1
        shared_ref = db.fetch_one("select refs from messages where id = %s", (result["id"],))["refs"][0]
        print(f"proposal #{result['id']} ref {shared_ref}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
