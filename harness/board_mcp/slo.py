"""Service level objectives and succession health, computed from the events table over a window.

Supervisors tune against numbers, not impressions, so the numbers are computed here by code and
handed to them in the brief; the same functions back the server's own checks (min_sample, the
ping triggers), which is what makes a supervisor's claim verifiable after the fact. Everything is
a pure function of a connection and a half-open window [window_start, window_end), so that two
consecutive rounds partition the timeline without double counting.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Any

import psycopg

from .db import fetch_all, fetch_one
from .limits import live_counts
from .seed import CONFIG_SEEDS

SLO_NAMES = ("completions_per_mtok", "blocked_failed_rate", "duplicate_rate", "lease_wait_median_s",
             "handoff_success_rate")
# Which direction is good, so "degraded for three windows" can be checked without a human.
HIGHER_IS_BETTER = {
    "completions_per_mtok": True,
    "blocked_failed_rate": False,
    "duplicate_rate": False,
    "lease_wait_median_s": False,
    "handoff_success_rate": True,
}

CLASSIFICATIONS = ("never_asked", "no_chance", "rejected", "ignored", "silent", "timed_out")

DUPLICATE_JACCARD = 0.6
DUPLICATE_MIN_WORDS = 8
# A kill recorded by the M3 runner carries no turn count. Under that runner the kill fired on the
# first observed turn past the threshold, so a kill within this many seconds of the request is
# treated as "no chance": the agent had no turn of its own in which to act.
LEGACY_NO_CHANCE_S = 5.0
TOOL_CALLS_KEPT = 20

WINDOW = "ts >= %(start)s and ts < %(end)s"


def _params(window_start: datetime, window_end: datetime, **extra: Any) -> dict[str, Any]:
    return {"start": window_start, "end": window_end, **extra}


def _count(conn: psycopg.Connection, type: str, window_start: datetime, window_end: datetime, extra_sql: str = "") -> int:
    row = fetch_one(conn, f"select count(*) as n from events where type = %(type)s and {WINDOW} {extra_sql}",
                    _params(window_start, window_end, type=type))
    return int(row["n"])


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _knob(conn: psycopg.Connection, knob: str) -> float:
    row = fetch_one(conn, "select value from config where knob = %s", (knob,))
    return float(row["value"]) if row else float(CONFIG_SEEDS[knob][0])


# --- the five SLOs ------------------------------------------------------------------------------


def compute_slos(conn: psycopg.Connection, window_start: datetime, window_end: datetime) -> dict[str, dict[str, Any]]:
    """Each SLO as {"value", "n"}: the number and the sample it rests on. A rate over nothing is 0
    with n = 0; a ratio with no denominator is None, so a renderer can tell "none happened" from
    "all failed"."""
    return {
        "completions_per_mtok": _completions_per_mtok(conn, window_start, window_end),
        "blocked_failed_rate": _blocked_failed_rate(conn, window_start, window_end),
        "duplicate_rate": _duplicate_rate(conn, window_start, window_end),
        "lease_wait_median_s": _lease_wait(conn, window_start, window_end),
        "handoff_success_rate": _handoff_success_rate(conn, window_start, window_end),
    }


def _completions_per_mtok(conn, window_start, window_end) -> dict[str, Any]:
    completions = _count(conn, "task.completed", window_start, window_end, "and payload->>'score' is not null")
    # Supervisor tokens are excluded (a turn whose agent row is unknown counts as solver spend).
    # Supervisor turns complete nothing, so counting them would make every round after the queue
    # drains look like a throughput collapse, as an Opus supervisor pointed out in the first replay.
    tokens = fetch_one(
        conn,
        f"""
        select coalesce(sum((e.payload->>'tokens_in')::bigint + (e.payload->>'tokens_out')::bigint), 0) as n
        from events e left join agents a on a.id = e.agent_id
        where e.type = 'agent.turn' and coalesce(a.role, 'solver') <> 'supervisor' and e.{WINDOW}
        """,
        _params(window_start, window_end),
    )
    spent = int(tokens["n"])
    value = completions / (spent / 1_000_000) if spent > 0 else None
    return {"value": value, "n": completions}


def _blocked_failed_rate(conn, window_start, window_end) -> dict[str, Any]:
    claims = _count(conn, "task.claimed", window_start, window_end)
    releases = _count(conn, "task.released", window_start, window_end)
    return {"value": releases / claims if claims else 0.0, "n": claims}


_WORD = re.compile(r"[a-z0-9]+")


def _words(body: str) -> set[str]:
    return set(_WORD.findall(body.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _duplicate_rate(conn, window_start, window_end) -> dict[str, Any]:
    """The fraction of this window's findings that restate an earlier finding in the same thread by
    someone else. Earlier means any time before, not only inside the window, because the
    duplicate of a day-old finding is still a duplicate. Completion summaries are excluded: two
    agents completing related tasks legitimately say similar things."""
    rows = fetch_all(
        conn,
        "select id, thread_id, author_id, body, created_at from messages "
        "where kind = 'finding' and created_at < %(end)s order by id",
        {"end": window_end},
    )
    seen_by_thread: dict[int | None, list[tuple[str, set[str]]]] = {}
    considered = duplicates = 0
    for r in rows:
        words = _words(r["body"])
        in_window = r["created_at"] >= window_start
        eligible = len(words) >= DUPLICATE_MIN_WORDS and not r["body"].startswith("[T-")
        if in_window and eligible:
            considered += 1
            earlier = seen_by_thread.get(r["thread_id"], [])
            if any(author != r["author_id"] and _jaccard(words, other) >= DUPLICATE_JACCARD for author, other in earlier):
                duplicates += 1
        if eligible:
            seen_by_thread.setdefault(r["thread_id"], []).append((r["author_id"], words))
    return {"value": duplicates / considered if considered else 0.0, "n": considered}


def _lease_wait(conn, window_start, window_end) -> dict[str, Any]:
    """Median seconds a task sat claimable before its claim, for claims inside the window. A task
    becomes claimable when created, when released back to open, or when a supervisor reopens it."""
    rows = fetch_all(
        conn,
        """
        select ts, type, payload->>'task_id' as task_id, payload->>'status' as status from events
        where type in ('task.created', 'task.released', 'task.reopened', 'task.claimed') and ts < %(end)s
        order by seq
        """,
        {"end": window_end},
    )
    claimable_since: dict[str, datetime] = {}
    waits: list[float] = []
    for r in rows:
        task_id = r["task_id"]
        if r["type"] == "task.claimed":
            if r["ts"] >= window_start and task_id in claimable_since:
                waits.append((r["ts"] - claimable_since.pop(task_id)).total_seconds())
            else:
                claimable_since.pop(task_id, None)
        elif r["type"] == "task.released" and r["status"] != "open":
            claimable_since.pop(task_id, None)  # blocked: not claimable until reopened
        else:
            claimable_since[task_id] = r["ts"]
    return {"value": _median(waits), "n": len(waits)}


def _handoff_success_rate(conn, window_start, window_end) -> dict[str, Any]:
    real = _count(conn, "agent.handed_off", window_start, window_end)
    stubs = _count(conn, "agent.killed", window_start, window_end)
    total = real + stubs
    return {"value": real / total if total else None, "n": total}


# --- succession health --------------------------------------------------------------------------


def compute_succession(conn: psycopg.Connection, window_start: datetime, window_end: datetime) -> dict[str, Any]:
    """The window's handoff story: counts, how long handoffs took, every refusal, where each
    lineage stands, and one root-cause packet per kill."""
    handoffs = _count(conn, "agent.handed_off", window_start, window_end)
    stubs = _count(conn, "agent.stub_written", window_start, window_end)
    handed = fetch_all(conn, f"select agent_id, ts, payload from events where type = 'agent.handed_off' and {WINDOW} order by seq",
                       _params(window_start, window_end))
    turns: list[float] = []
    seconds: list[float] = []
    for h in handed:
        detail = _since_request(conn, h["agent_id"], h["ts"], h["payload"])
        if detail["turns_since_request"] is not None:
            turns.append(detail["turns_since_request"])
        if detail["seconds_since_request"] is not None:
            seconds.append(detail["seconds_since_request"])
    refusals = fetch_all(
        conn,
        f"select payload->>'error' as error, count(*) as n from events where type = 'tool.refused' and {WINDOW} "
        "group by 1 order by 2 desc, 1",
        _params(window_start, window_end),
    )
    generations = fetch_all(conn, "select lineage_id, max(generation) as g from agents group by lineage_id order by lineage_id")
    kills = fetch_all(conn, f"select agent_id, ts, payload from events where type = 'agent.killed' and {WINDOW} order by seq",
                      _params(window_start, window_end))
    total = handoffs + stubs
    return {
        "requests": _count(conn, "agent.handoff_requested", window_start, window_end),
        "handoffs": handoffs,
        "stubs": stubs,
        "kills": len(kills),
        "success_rate": handoffs / total if total else None,
        "median_turns_to_handoff": _median(turns),
        "median_seconds_to_handoff": _median(seconds),
        "refusals": {r["error"] or "": int(r["n"]) for r in refusals},
        "generations": {r["lineage_id"]: int(r["g"]) for r in generations},
        "incidents": [_incident(conn, k["agent_id"], k["ts"], k["payload"]) for k in kills],
    }


def _since_request(conn, agent_id: str, end_ts: datetime, payload: dict[str, Any]) -> dict[str, Any]:
    """What the end event says about the request, filled in from the events when the payload
    predates the kill-gap rule (M3 runs stamped nothing)."""
    request = fetch_one(
        conn,
        "select ts from events where agent_id = %s and type = 'agent.handoff_requested' and ts <= %s order by seq desc limit 1",
        (agent_id, end_ts),
    )
    request_ts = request["ts"] if request else None
    requested = bool(payload["requested"]) if "requested" in payload else request_ts is not None
    # A legacy payload has no turn count and there is no honest way to recover one: the M3 runner's
    # turn counter was not in the events. It stays None and the seconds rule stands in for it.
    turns = payload.get("turns_since_request") if "turns_since_request" in payload else None
    if "seconds_since_request" in payload and payload["seconds_since_request"] is not None:
        seconds = float(payload["seconds_since_request"])
    elif request_ts is not None:
        seconds = round((end_ts - request_ts).total_seconds(), 3)
    else:
        seconds = None
    grace = payload.get("grace_turns") if "grace_turns" in payload else int(_knob(conn, "kill_grace_turns"))
    return {"requested": requested, "request_ts": request_ts, "turns_since_request": turns,
            "seconds_since_request": seconds, "grace_turns": grace}


def _incident(conn, agent_id: str, kill_ts: datetime, payload: dict[str, Any]) -> dict[str, Any]:
    agent = fetch_one(conn, "select lineage_id, generation from agents where id = %s", (agent_id,))
    detail = _since_request(conn, agent_id, kill_ts, payload)
    request_ts = detail.pop("request_ts")
    sent = fetch_one(
        conn,
        "select payload from events where agent_id = %s and type = 'agent.handoff_sent' and ts <= %s order by seq desc limit 1",
        (agent_id, kill_ts),
    )
    context_at_request = sent["payload"].get("context_pct") if sent else None
    if context_at_request is None and request_ts is not None:
        last_turn = fetch_one(
            conn,
            "select payload from events where agent_id = %s and type = 'agent.turn' and ts <= %s order by seq desc limit 1",
            (agent_id, request_ts),
        )
        context_at_request = last_turn["payload"].get("context_pct") if last_turn else None
    tool_calls: list[str] = []
    handoff_refusals: list[str] = []
    if request_ts is not None:
        after = fetch_all(
            conn,
            "select type, payload from events where agent_id = %s and type like 'tool.%%' and ts > %s and ts <= %s order by seq",
            (agent_id, request_ts, kill_ts),
        )
        for e in after:
            if e["type"] == "tool.refused":
                tool_calls.append(f"tool.refused({e['payload'].get('tool')})")
                if e["payload"].get("tool") == "civ_handoff":
                    handoff_refusals.append(str(e["payload"].get("error")))
            else:
                tool_calls.append(e["type"])
    stub = fetch_one(
        conn,
        "select payload from events where agent_id = %s and type = 'agent.stub_written' and ts >= %s order by seq limit 1",
        (agent_id, kill_ts),
    )
    packet = {
        "agent_id": agent_id,
        "lineage_id": agent["lineage_id"] if agent else None,
        "generation": int(agent["generation"]) if agent else payload.get("generation"),
        "reason": payload.get("reason"),
        "requested": detail["requested"],
        "turns_since_request": detail["turns_since_request"],
        "seconds_since_request": detail["seconds_since_request"],
        "grace_turns": detail["grace_turns"],
        "context_pct_at_request": context_at_request,
        "tool_calls_after_request": tool_calls[:TOOL_CALLS_KEPT],
        "handoff_refusals": handoff_refusals,
        "task": stub["payload"].get("task") if stub else None,
    }
    packet["classification"] = classify(packet)
    return packet


def classify(packet: dict[str, Any]) -> str:
    """The contract's rules in order. A heuristic: the supervisor is told to confirm or override it."""
    if not packet["requested"]:
        return "never_asked"
    if packet["reason"] == "handoff timed out":
        return "timed_out"
    if packet["handoff_refusals"]:
        return "rejected"
    turns = packet["turns_since_request"]
    grace = packet["grace_turns"]
    seconds = packet["seconds_since_request"]
    if turns is None:
        if seconds is not None and seconds < LEGACY_NO_CHANCE_S:
            return "no_chance"
    elif turns == 0 or (grace is not None and turns < grace):
        # turns == 0 covers grace 0, where "turns < grace" can never hold but the agent still had
        # no turn of its own after the request.
        return "no_chance"
    if packet["tool_calls_after_request"]:
        return "ignored"
    return "silent"


# --- the instantaneous numbers civ_status and the brief share -----------------------------------


def status_snapshot(conn: psycopg.Connection) -> dict[str, Any]:
    tasks = {"open": 0, "leased": 0, "done": 0, "blocked": 0}
    for r in conn.execute("select status, count(*) as n from tasks group by status").fetchall():
        tasks[r["status"]] = int(r["n"])
    # The orchestrator logs one agent.turn event per model turn with the turn's token deltas.
    tpm = fetch_one(
        conn,
        """
        select coalesce(sum((payload->>'tokens_in')::bigint + (payload->>'tokens_out')::bigint), 0) as n
        from events where type = 'agent.turn' and ts > now() - interval '1 minute'
        """,
    )
    return {"live_agents": live_counts(conn), "tokens_per_min": int(tpm["n"]), "tasks": tasks}
