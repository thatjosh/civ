"""Check the shared store against itself and against the database (M7).

Two invariants hold for shared/ when nothing has been lost:

* every artifact's file name starts with the first sixteen hex digits of the sha256 of its bytes,
  because the server names files by content hash when it copies them in (spilled message bodies,
  the ``msg-<hash>.md`` files, hash the body text the server wrote, so they are listed but not
  re-hashed here);
* every ``shared/...`` reference the database holds, in ``messages.refs`` and ``tasks.result_ref``,
  names a file that exists.

Usage: python scripts/verify_shared.py [--shared shared/] [--database-url URL] [--no-db]

Exit 0 when both hold, 1 with the problems listed otherwise. The restore runbook runs this after
putting shared/ back and before ``civ restore``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"
HASH_PREFIX = re.compile(r"^([0-9a-f]{16})-")


def check_hashes(shared: Path) -> tuple[list[str], int]:
    """Problems and the count of files checked."""
    problems: list[str] = []
    checked = 0
    for path in sorted(p for p in shared.rglob("*") if p.is_file()):
        name = path.name
        if name.startswith("msg-") or name == ".gitkeep":
            continue
        m = HASH_PREFIX.match(name)
        if not m:
            # Not server-named; left alone rather than flagged, because the operator may keep notes here.
            continue
        checked += 1
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        if digest != m.group(1):
            problems.append(f"corrupt: {path.relative_to(shared.parent)} hashes to {digest}, name says {m.group(1)}")
    return problems, checked


def referenced_paths(db_url: str) -> list[str]:
    import psycopg  # imported here so --no-db works without the driver

    refs: list[str] = []
    with psycopg.connect(db_url) as conn:
        for (arr,) in conn.execute("select refs from messages where refs <> '{}'"):
            refs.extend(r for r in (arr or []) if isinstance(r, str) and r.startswith("shared/"))
        for (ref,) in conn.execute("select result_ref from tasks where result_ref is not null"):
            if isinstance(ref, str) and ref.startswith("shared/"):
                refs.append(ref)
    return sorted(set(refs))


def check_references(shared: Path, refs: list[str]) -> list[str]:
    missing = []
    for ref in refs:
        rel = ref[len("shared/"):]
        if not (shared / rel).is_file():
            missing.append(f"missing: {ref} is referenced by the database but is not in {shared}")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shared", type=Path, default=Path(os.environ.get("CIV_ROOT", ROOT)) / "shared")
    parser.add_argument("--database-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    parser.add_argument("--no-db", action="store_true", help="hash check only")
    args = parser.parse_args(argv)

    shared = args.shared.resolve()
    if not shared.is_dir():
        print(f"verify_shared: {shared} is not a directory", file=sys.stderr)
        return 1
    problems, checked = check_hashes(shared)
    refs: list[str] = []
    if not args.no_db:
        refs = referenced_paths(args.database_url)
        problems.extend(check_references(shared, refs))
    for line in problems:
        print(line)
    print(f"verify_shared: {checked} hashed files checked, {len(refs)} database references checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
