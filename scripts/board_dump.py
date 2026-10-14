"""Print the board as a transcript a human can read after a run.

Grouped by task thread, each message with its author, kind, decayed score and
the votes it drew with their reasons, then the task table with verifier scores,
then anything outside a thread (alerts, broadcasts, reports). Reads the schema
in docs/CONTRACT.md directly, so it works even when the board server is down.

Usage: python scripts/board_dump.py [--database-url URL]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"

# The same decay the server ranks by (docs/CONTRACT.md, scoring), so the
# numbers here match what the agents saw.
SCORE_SQL = """
coalesce((select sum(v.value * v.weight) from votes v where v.message_id = m.id), 0)
  / power(extract(epoch from (now() - m.created_at)) / 3600.0
          + (select value from config where knob = 'decay_offset_h'),
          (select value from config where knob = 'decay_power'))
"""


def load(db_url: str) -> dict:
    import psycopg

    with psycopg.connect(db_url) as conn:
        messages = conn.execute(
            f"select m.id, m.author_id, m.kind, m.thread_id, m.parent_id, m.body, m.refs, m.read_seq, "
            f"m.created_at, {SCORE_SQL} as score from messages m order by m.id"
        ).fetchall()
        votes = conn.execute(
            "select message_id, voter_id, value, weight, reason from votes order by message_id, created_at"
        ).fetchall()
        tasks = conn.execute(
            "select id, title, status, thread_id, lease_owner, attempts, score, notes, completed_by, result_ref "
            "from tasks order by id"
        ).fetchall()
        agents = conn.execute(
            "select id, role, generation, status, tokens_in, tokens_out, context_pct from agents order by id"
        ).fetchall()
    return {"messages": messages, "votes": votes, "tasks": tasks, "agents": agents}


def _wrap(text: str, indent: str, width: int = 96) -> str:
    import textwrap

    return "\n".join(textwrap.fill(line, width=width, initial_indent=indent, subsequent_indent=indent) or indent
                     for line in text.splitlines()) or indent


def render(data: dict) -> str:
    votes_by_msg: dict[int, list] = defaultdict(list)
    for message_id, voter, value, weight, reason in data["votes"]:
        votes_by_msg[message_id].append((voter, value, weight, reason))

    by_thread: dict[int | None, list] = defaultdict(list)
    for row in data["messages"]:
        msg_id, _, kind, thread_id, *_ = row
        key = thread_id if thread_id is not None else (msg_id if kind == "task" else None)
        by_thread[key].append(row)

    task_by_thread = {t[3]: t for t in data["tasks"]}
    out = []
    for thread_id in sorted(k for k in by_thread if k is not None):
        task = task_by_thread.get(thread_id)
        header = f"=== thread {thread_id}"
        if task:
            header += f": {task[0]} {task[1]} [{task[2]}" + (f", score {task[6]:.2f}" if task[6] is not None else "") + "]"
        out.append(header)
        for msg_id, author, kind, _, parent_id, body, refs, read_seq, created_at, score in by_thread[thread_id]:
            tag = f"  #{msg_id} {created_at:%H:%M:%S} {author} {kind}"
            if parent_id:
                tag += f" (reply to #{parent_id})"
            if read_seq:
                tag += f" (had read to #{read_seq})"
            if score:
                tag += f" score {score:+.2f}"
            out.append(tag)
            out.append(_wrap(body, "      "))
            if refs:
                out.append(f"      refs: {', '.join(refs)}")
            for voter, value, weight, reason in votes_by_msg.get(msg_id, []):
                out.append(f"      vote {'+' if value > 0 else '-'}{weight:g} by {voter}: {reason}")
        out.append("")

    loose = by_thread.get(None, [])
    if loose:
        out.append("=== outside threads (alerts, broadcasts, reports)")
        for msg_id, author, kind, _, _, body, *_ in loose:
            out.append(f"  #{msg_id} {author} {kind}")
            out.append(_wrap(body, "      "))
        out.append("")

    out.append("=== tasks")
    for task_id, title, status, _, lease_owner, attempts, score, notes, completed_by, result_ref in data["tasks"]:
        line = f"  {task_id} {title:<26} {status:<7}"
        if score is not None:
            line += f" score {score:.2f}"
        if completed_by:
            line += f" by {completed_by}"
        if lease_owner and status == "leased":
            line += f" leased by {lease_owner}"
        if attempts:
            line += f" attempts {attempts}"
        out.append(line)
        if notes:
            out.append(f"      {notes}")
    out.append("")

    out.append("=== agents")
    for agent_id, role, generation, status, tokens_in, tokens_out, context_pct in data["agents"]:
        out.append(f"  {agent_id} {role} gen {generation} {status:<11} in {tokens_in:,} out {tokens_out:,} context {context_pct:.1f}%")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    args = parser.parse_args(argv)
    print(render(load(args.database_url)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
