"""The stub handoff: a handoff written from the board when an agent died without writing one.

An agent killed at the context ceiling leaves no handoff, but it leaves its posts, its lease and
its directory listing, and that is enough for a successor to avoid starting from nothing. This is
a template, not a model call, so it can run inside the orchestrator with no cost; in M4 the
supervisors take over writing stubs with judgement, and this stays as the fallback.
"""

from __future__ import annotations

from typing import Any

HEADINGS = ("State of work", "Open threads", "What to try next", "What not to retry", "Files worth reading")
MAX_POST_CHARS = 400


def _clip(text: str, limit: int = MAX_POST_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_stub(posts: list[dict[str, Any]], task: dict[str, Any] | None, listing: list[str]) -> str:
    """Assemble the five-heading handoff body.

    posts: the agent's recent board messages, any order (sorted here by id); each has id, kind,
    thread_id, body and created_at. task: the task the agent still held, or None. listing: the
    files under the agent's work/ and notes/ directories.
    """
    ordered = sorted(posts, key=lambda p: int(p["id"]))
    lines: list[str] = []

    lines.append(f"## {HEADINGS[0]}")
    lines.append("Stub handoff: this agent was ended by the orchestrator before it wrote a handoff, so "
                 "what follows was assembled from its board posts and directory, not by the agent. Verify "
                 "before trusting.")
    if task:
        lines.append(f"It held task {task['id']} ({task.get('title', '')}) in thread {task.get('thread_id')}; "
                     "the lease passes to you.")
    else:
        lines.append("It held no task at the end.")
    if ordered:
        lines.append(f"Its last {len(ordered)} posts, oldest first:")
        for p in ordered:
            where = f" in thread {p['thread_id']}" if p.get("thread_id") is not None else ""
            lines.append(f"- seq {p['id']} {p['kind']}{where}: {_clip(p['body'])}")
    else:
        lines.append("It posted nothing.")

    lines.append(f"## {HEADINGS[1]}")
    # The threads the agent touched, with the highest sequence it wrote in each, so the successor
    # knows where to read from.
    latest_by_thread: dict[int, int] = {}
    for p in ordered:
        if p.get("thread_id") is not None:
            latest_by_thread[int(p["thread_id"])] = int(p["id"])
    if latest_by_thread:
        for thread_id, seq in sorted(latest_by_thread.items()):
            lines.append(f"- thread {thread_id}: read from seq {seq}")
    else:
        lines.append("None recorded.")

    lines.append(f"## {HEADINGS[2]}")
    if task:
        lines.append(f"Finish {task['id']}: {_clip(task.get('description', ''), 600)}")
        lines.append("Read the thread first; the predecessor's posts above may already hold part of the answer.")
    else:
        lines.append("Claim the next task; nothing was in flight.")

    lines.append(f"## {HEADINGS[3]}")
    blockers = [p for p in ordered if p["kind"] == "blocker"]
    if blockers:
        for p in blockers:
            lines.append(f"- {_clip(p['body'])}")
    else:
        lines.append("No blockers were posted; nothing is known to have failed.")

    lines.append(f"## {HEADINGS[4]}")
    if listing:
        for path in listing:
            lines.append(f"- {path}")
    else:
        lines.append("The directory held no work files.")

    return "\n".join(lines) + "\n"
