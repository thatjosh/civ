"""Render a round's brief (the dict the operator builds) as markdown for the supervisor prompt.

The orchestrator reads the board so the supervisor does not have to: the window's SLOs next to
the previous window's, every succession incident as a packet with its classification, the
blockers, the alerts, the last round's reports, the knobs currently moved. Rendering is
deterministic and bounded, because the brief is the largest variable part of a supervisor's
prompt and a ten-incident window must still leave room for the round itself.
"""

from __future__ import annotations

from typing import Any

# Lists are cut at these lengths, bodies at these characters. Bodies are never cut below
# 200 characters: a blocker shortened past its reason is worse than no blocker at all.
MAX_INCIDENTS = 10
MAX_BLOCKERS = 10
MAX_ALERTS = 10
MAX_REPORTS = 5
MAX_HISTORY = 5
BODY_CHARS = 300
REPORT_CHARS = 600
MIN_BODY_CHARS = 200

SLO_NAMES = ("completions_per_mtok", "blocked_failed_rate", "duplicate_rate", "lease_wait_median_s", "handoff_success_rate")
TUNED_LINE = "This round has already tuned a knob."


def render_brief(brief: dict[str, Any]) -> str:
    """The brief as markdown sections in a fixed order. Missing sections render as 'none'."""
    brief = brief or {}
    sections = [
        _round(brief.get("round") or {}),
        _status(brief.get("status") or {}),
        _slos(brief.get("slo") or {}),
        _succession(brief.get("succession") or {}),
        _blockers(brief.get("blockers") or [], brief.get("blocked_tasks") or []),
        _alerts(brief.get("alerts") or []),
        _previous_reports(brief.get("previous_reports") or []),
        _tunes(brief.get("tunes") or {}),
        _verifier(brief.get("verifier") or {}),
    ]
    return "\n\n".join(s for s in sections if s).strip() + "\n"


# --- sections ------------------------------------------------------------------


def _round(r: dict[str, Any]) -> str:
    number = r.get("number", "?")
    final = " (final round: the solvers have finished, write the post-mortem)" if r.get("final") else ""
    window = f"{_s(r.get('window_start'))} to {_s(r.get('window_end'))}"
    return (f"## Round {number}{final}\n\n"
            f"Window: {window}. Supervisors this round: {r.get('supervisors', '?')}.")


def _status(st: dict[str, Any]) -> str:
    live = st.get("live_agents") or {}
    tasks = st.get("tasks") or {}
    return ("## Status\n\n"
            f"- Live agents: {live.get('solver', 0)} solvers, {live.get('supervisor', 0)} supervisors\n"
            f"- Tasks: open {tasks.get('open', 0)}, leased {tasks.get('leased', 0)}, done {tasks.get('done', 0)}, "
            f"blocked {tasks.get('blocked', 0)}\n"
            f"- Tokens per minute (last minute): {st.get('tokens_per_min', 0)}")


def _slos(slo: dict[str, Any]) -> str:
    current = slo.get("current") or {}
    previous = slo.get("previous")
    lines = ["## SLOs (this window against the previous one)", "",
             "| SLO | this window | n | previous | n |", "|---|---|---|---|---|"]
    for name in SLO_NAMES:
        cur = _slo_cell(current.get(name))
        prev = _slo_cell(previous.get(name)) if previous else ("none", "")
        lines.append(f"| {name} | {cur[0]} | {cur[1]} | {prev[0]} | {prev[1]} |")
    if previous is None:
        lines.append("")
        lines.append("No previous window: this is the first round, so nothing can be compared yet.")
    if slo.get("min_sample") is not None:
        lines.append("")
        lines.append(f"min_sample is {_n(slo['min_sample'])}: a tune naming an SLO with n below that is refused.")
    history = (slo.get("history") or [])[-MAX_HISTORY:]
    if history:
        lines.append("")
        lines.append("History (older rounds first): " + "; ".join(
            f"round {h.get('round', '?')}: " + ", ".join(f"{k}={_v(h.get(k))}" for k in SLO_NAMES if k in h)
            for h in history))
    return "\n".join(lines)


def _slo_cell(entry: Any) -> tuple[str, str]:
    """An SLO entry is {"value", "n"} or a bare number; returns (value, n) as strings."""
    if isinstance(entry, dict):
        return _v(entry.get("value")), _n(entry.get("n"))
    return _v(entry), ""


def _succession(su: dict[str, Any]) -> str:
    lines = ["## Succession", ""]
    lines.append(f"- Handoff requests: {_n(su.get('requests'))}, real handoffs: {_n(su.get('handoffs'))}, "
                 f"stubs: {_n(su.get('stubs'))}, kills: {_n(su.get('kills'))}")
    lines.append(f"- Handoff success rate: {_v(su.get('success_rate'))}")
    lines.append(f"- Median turns from request to handoff: {_v(su.get('median_turns_to_handoff'))}; "
                 f"median seconds: {_v(su.get('median_seconds_to_handoff'))}")
    refusals = su.get("refusals") or {}
    if refusals:
        lines.append("- Handoff refusals: " + ", ".join(f"{k} ({v})" for k, v in sorted(refusals.items())))
    generations = su.get("generations") or {}
    if generations:
        lines.append("- Lineage generations: " + ", ".join(f"{k} at {v}" for k, v in sorted(generations.items())))
    incidents = su.get("incidents") or []
    if not incidents:
        lines.append("")
        lines.append("No kills this window.")
        return "\n".join(lines)
    for packet in incidents[:MAX_INCIDENTS]:
        lines.append("")
        lines.append(_incident(packet))
    if len(incidents) > MAX_INCIDENTS:
        lines.append("")
        lines.append(f"And {len(incidents) - MAX_INCIDENTS} more kills not shown; the pattern above is the evidence.")
    return "\n".join(lines)


def _incident(pk: dict[str, Any]) -> str:
    tools = pk.get("tool_calls_after_request") or []
    refusals = pk.get("handoff_refusals") or []
    return "\n".join([
        f"### Kill: {pk.get('agent_id', '?')} (lineage {pk.get('lineage_id', '?')}, generation {pk.get('generation', '?')})",
        f"- Classification (heuristic, confirm or override): **{pk.get('classification', 'unknown')}**",
        f"- Reason: {pk.get('reason', '?')}; task held: {pk.get('task') or 'none'}",
        f"- Requested: {_b(pk.get('requested'))}; turns since request: {_v(pk.get('turns_since_request'))}; "
        f"seconds since request: {_v(pk.get('seconds_since_request'))}; grace turns: {_v(pk.get('grace_turns'))}; "
        f"context at request: {_v(pk.get('context_pct_at_request'))}{'%' if pk.get('context_pct_at_request') is not None else ''}",
        f"- Tool calls after the request: {', '.join(tools) if tools else 'none'}",
        f"- Handoff refusals: {'; '.join(_clip(str(r), BODY_CHARS) for r in refusals) if refusals else 'none'}",
    ])


def _blockers(blockers: list[dict[str, Any]], blocked_tasks: list[dict[str, Any]]) -> str:
    lines = ["## Blockers", ""]
    if blockers:
        for b in blockers[:MAX_BLOCKERS]:
            task = f" [{b['task_id']}]" if b.get("task_id") else ""
            lines.append(f"- seq {b.get('id', '?')}{task} by {b.get('author_id', '?')} in thread {b.get('thread_id', '?')} "
                         f"(score {_v(b.get('score'))}): {_clip(b.get('body', ''))}")
        if len(blockers) > MAX_BLOCKERS:
            lines.append(f"- and {len(blockers) - MAX_BLOCKERS} more")
    else:
        lines.append("No blockers this window.")
    lines.append("")
    lines.append("Blocked tasks (attempts exhausted, waiting for a supervisor):")
    if blocked_tasks:
        for t in blocked_tasks:
            lines.append(f"- {t.get('id', '?')} ({_clip(t.get('title', ''), BODY_CHARS)}), attempts {_n(t.get('attempts'))}: "
                         f"last release reason: {_clip(t.get('last_release_reason') or 'none')}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def _alerts(alerts: list[dict[str, Any]]) -> str:
    lines = ["## Alerts since the window opened", ""]
    if not alerts:
        lines.append("None.")
        return "\n".join(lines)
    for a in alerts[:MAX_ALERTS]:
        lines.append(f"- seq {a.get('id', '?')} by {a.get('author_id', '?')} at {_s(a.get('created_at'))}: {_clip(a.get('body', ''))}")
    if len(alerts) > MAX_ALERTS:
        lines.append(f"- and {len(alerts) - MAX_ALERTS} more")
    return "\n".join(lines)


def _previous_reports(reports: list[dict[str, Any]]) -> str:
    lines = ["## Previous round's reports", ""]
    if not reports:
        lines.append("None (first round, or nobody reported).")
        return "\n".join(lines)
    for r in reports[:MAX_REPORTS]:
        lines.append(f"### Report seq {r.get('id', '?')} by {r.get('author_id', '?')}")
        lines.append(_clip(r.get("body", ""), REPORT_CHARS))
        lines.append("")
    return "\n".join(lines).rstrip()


def _tunes(tunes: dict[str, Any]) -> str:
    lines = ["## Knobs", ""]
    active = tunes.get("active") or []
    if active:
        lines.append("Currently moved from their seeds (each reverts at expires_at):")
        for t in active:
            lines.append(f"- {t.get('knob', '?')} = {_v(t.get('value'))} (seed {_v(t.get('seed'))}), set by {t.get('set_by', '?')} "
                         f"until {_s(t.get('expires_at'))}: {_clip(t.get('reason') or '', BODY_CHARS)}")
    else:
        lines.append("Every knob is at its seed.")
    pending = tunes.get("pending") or []
    if pending:
        lines.append("")
        lines.append("Big moves waiting for a second supervisor (call civ_tune with the same knob and value to commit):")
        for t in pending:
            lines.append(f"- {t.get('knob', '?')} to {_v(t.get('value'))}, proposed by {t.get('proposed_by', '?')} in round {t.get('round_number', '?')}")
    if tunes.get("round_tuned"):
        lines.append("")
        lines.append(TUNED_LINE)
    return "\n".join(lines)


def _verifier(v: dict[str, Any]) -> str:
    lines = ["## Verifier progress", ""]
    lines.append(f"- Done {_n(v.get('done'))} of {_n(v.get('total'))}, mean score {_v(v.get('mean_score'))}")
    lowest = v.get("lowest") or []
    if lowest:
        lines.append("- Lowest scores: " + "; ".join(
            f"{t.get('id', '?')} {_v(t.get('score'))}" + (f" ({_clip(t.get('notes'), MIN_BODY_CHARS)})" if t.get("notes") else "")
            for t in lowest))
    return "\n".join(lines)


# --- formatting helpers -------------------------------------------------------------


def _clip(text: Any, limit: int = BODY_CHARS) -> str:
    limit = max(limit, MIN_BODY_CHARS)
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _v(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, float):
        return f"{value:.3g}" if abs(value) < 1000 else f"{value:.0f}"
    return str(value)


def _n(value: Any) -> str:
    return "0" if value is None else str(value)


def _b(value: Any) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _s(value: Any) -> str:
    return "?" if value is None else str(value)
