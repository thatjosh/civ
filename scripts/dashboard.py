"""Render the operator dashboard as one static HTML page from the database (M7).

    python scripts/dashboard.py --out docs/demo/2026-10-23-soak-dashboard.html [--db-url URL] [--title ...]

The design doc's dashboard is a follow-up document; this is the version the soak needs now: the
same numbers, read once from the events, agents, tasks, rounds, config and messages tables, drawn
as inline SVG with no external assets, in the warm palette of docs/civ-dashboard-mockup.html.
It renders sensibly on an empty database (every panel says so) and is sized for a run of a few
hours; the series are bucketed by minute so the page stays small.

Panels: heartbeat age and epoch boundaries; live agents over time; tasks done over time with the
verifier mean; handoff success per round with stubs and kills marked; tokens per minute; the knob
timeline (presets, tunes, reverts); chaos events as markers; the inbox, newest first, truncated;
spend by role.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"

# The mockup's palette. No blue anywhere: series are orange, green, violet and rose, states are
# the four semantic colours, and the chrome is warm greys on an off-white page.
PALETTE = {
    "page": "#f9f9f7", "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "grid": "#e1e0d9", "axis": "#c3c2b7", "ring": "rgba(11,11,11,0.10)",
    "s1": "#eb6834", "s2": "#1baf7a", "s3": "#6250d6", "s4": "#e87ba4",
    "good": "#0ca30c", "warn": "#fab219", "serious": "#ec835a", "crit": "#d03b3b", "track": "#ecebe6",
}
DARK = {
    "page": "#0d0d0d", "surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
    "grid": "#2c2c2a", "axis": "#383835", "ring": "rgba(255,255,255,0.10)",
    "s1": "#d95926", "s2": "#199e70", "s3": "#9085e9", "s4": "#d55181", "track": "#262624",
}


# --- data ---------------------------------------------------------------------------------------


class Source:
    """Everything the page needs, read in one connection. Each method tolerates an empty table."""

    def __init__(self, db_url: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(db_url, row_factory=dict_row)

    def close(self) -> None:
        self._conn.close()

    def rows(self, sql: str, params: Any = None) -> list[dict]:
        return list(self._conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Any = None) -> dict | None:
        return self._conn.execute(sql, params).fetchone()

    def has_table(self, name: str) -> bool:
        row = self.one("select 1 as ok from information_schema.tables where table_schema = 'public' and table_name = %s", (name,))
        return row is not None

    def control(self, key: str) -> Any:
        if not self.has_table("control_state"):
            return None
        row = self.one("select value, extract(epoch from (now() - updated_at)) as age_s from control_state where key = %s", (key,))
        return row

    def heartbeat(self) -> dict[str, Any] | None:
        row = self.control("heartbeat")
        if row is None or not isinstance(row["value"], dict):
            return None
        beat = dict(row["value"])
        at = beat.get("at")
        if at:
            parsed = self.one("select extract(epoch from (now() - %s::timestamptz)) as age_s", (at,))
            beat["age_s"] = float(parsed["age_s"])
        else:
            beat["age_s"] = float(row["age_s"])
        return beat

    def span(self) -> tuple[dt.datetime, dt.datetime] | None:
        row = self.one("select min(ts) as lo, max(ts) as hi from events")
        if row is None or row["lo"] is None:
            return None
        return row["lo"], row["hi"]

    def events(self, types: tuple[str, ...]) -> list[dict]:
        return self.rows("select ts, agent_id, type, payload, epoch from events where type = any(%s) order by seq", (list(types),))

    def epoch_boundaries(self) -> list[dict]:
        return self.rows("select epoch, min(ts) as ts from events group by epoch order by epoch")

    def agents(self) -> list[dict]:
        return self.rows("select id, role, generation, status, spawned_at, ended_at, harness_version from agents order by spawned_at")

    def tasks(self) -> list[dict]:
        return self.rows("select id, status, score, completed_at from tasks order by id")

    def rounds(self) -> list[dict]:
        if not self.has_table("rounds"):
            return []
        return self.rows("select id, number, started_at, ended_at, final, brief, tuned_knob from rounds order by id")

    def inbox(self, limit: int = 12) -> list[dict]:
        return self.rows(
            "select id, author_id, kind, body, created_at from messages where kind in ('report', 'alert', 'broadcast') "
            "order by id desc limit %s", (limit,))

    def spend_by_role(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.rows(
            "select a.role, coalesce(sum((e.payload->>'cost_usd')::float), 0) as usd from events e join agents a on a.id = e.agent_id "
            "where e.type = 'agent.ended' and e.payload->>'cost_usd' is not null group by a.role"):
            out[r["role"]] = float(r["usd"])
        return out

    def knobs(self) -> list[dict]:
        return self.rows("select knob, value, set_by, expires_at from config order by knob")


# --- shaping ------------------------------------------------------------------------------------


def minute_buckets(lo: dt.datetime, hi: dt.datetime) -> list[dt.datetime]:
    start = lo.replace(second=0, microsecond=0)
    end = hi.replace(second=0, microsecond=0)
    n = int((end - start).total_seconds() // 60) + 1
    return [start + dt.timedelta(minutes=i) for i in range(max(n, 1))]


def live_series(agents: list[dict], buckets: list[dt.datetime]) -> dict[str, list[int]]:
    """Live agents per role at the end of each minute, from spawn and end timestamps."""
    out = {"solver": [], "supervisor": []}
    for b in buckets:
        edge = b + dt.timedelta(minutes=1)
        for role in out:
            out[role].append(sum(1 for a in agents if a["role"] == role and a["spawned_at"] < edge
                                 and (a["ended_at"] is None or a["ended_at"] >= edge)))
    return out


def done_series(tasks: list[dict], buckets: list[dt.datetime]) -> list[int]:
    return [sum(1 for t in tasks if t["completed_at"] is not None and t["completed_at"] < b + dt.timedelta(minutes=1)) for b in buckets]


def tokens_series(turns: list[dict], buckets: list[dt.datetime]) -> list[int]:
    per_minute: Counter = Counter()
    for e in turns:
        key = e["ts"].replace(second=0, microsecond=0)
        p = e["payload"] or {}
        per_minute[key] += int(p.get("tokens_in", 0) or 0) + int(p.get("tokens_out", 0) or 0)
    return [per_minute.get(b, 0) for b in buckets]


def round_succession(rounds: list[dict]) -> list[dict]:
    out = []
    for r in rounds:
        brief = r["brief"] if isinstance(r["brief"], dict) else {}
        s = (brief.get("succession") or {})
        handoffs, stubs, kills = int(s.get("handoffs", 0) or 0), int(s.get("stubs", 0) or 0), int(s.get("kills", 0) or 0)
        rate = s.get("success_rate")
        out.append({"number": r["number"], "handoffs": handoffs, "stubs": stubs, "kills": kills,
                    "rate": float(rate) if rate is not None else None, "tuned": r["tuned_knob"], "final": bool(r["final"])})
    return out


# --- svg helpers --------------------------------------------------------------------------------


W, H, PAD_L, PAD_R, PAD_T, PAD_B = 720, 180, 44, 12, 12, 26


def _x(i: int, n: int) -> float:
    inner = W - PAD_L - PAD_R
    return PAD_L + (inner * i / max(n - 1, 1))


def _y(v: float, vmax: float) -> float:
    inner = H - PAD_T - PAD_B
    return PAD_T + inner - (inner * (v / vmax if vmax else 0))


def _nice_max(values: list[float]) -> float:
    top = max(values) if values else 0
    if top <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(top))
    for step in (1, 2, 5, 10):
        if top <= step * mag:
            return float(step * mag)
    return float(top)


def _fmt(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:g}"


def chart(series: dict[str, list[float]], labels: list[str], colours: list[str], markers: list[dict] | None = None,
          empty: str = "no data yet", kind: str = "line", ymax: float | None = None) -> str:
    """A line or step chart over minute buckets with gridlines, a left axis and optional vertical
    markers (epoch boundaries, chaos). Series share one y axis."""
    n = len(labels)
    if n == 0 or not any(any(v for v in vals) for vals in series.values()):
        return f'<div class="empty">{html.escape(empty)}</div>'
    vmax = ymax or _nice_max([v for vals in series.values() for v in vals])
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" role="img" aria-label="chart">']
    for frac in (0, 0.25, 0.5, 0.75, 1):
        y = _y(vmax * frac, vmax)
        parts.append(f'<line class="gridline" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{_fmt(vmax * frac)}</text>')
    for m in markers or []:
        i = m.get("index")
        if i is None or not (0 <= i < n):
            continue
        x = _x(i, n)
        colour = m.get("colour", PALETTE["crit"])
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{PAD_T}" y2="{H - PAD_B}" stroke="{colour}" stroke-width="1.5" stroke-dasharray="3 3"/>')
        parts.append(f'<text class="marker" x="{x + 3:.1f}" y="{PAD_T + 10}" fill="{colour}">{html.escape(str(m.get("label", "")))}</text>')
    for (name, vals), colour in zip(series.items(), colours):
        pts = []
        for i, v in enumerate(vals):
            x, y = _x(i, n), _y(v, vmax)
            if kind == "step" and i > 0:
                pts.append(f"{x:.1f},{_y(vals[i - 1], vmax):.1f}")
            pts.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline fill="none" stroke="{colour}" stroke-width="2" stroke-linejoin="round" points="{" ".join(pts)}"/>')
    step = max(1, n // 8)
    for i in range(0, n, step):
        parts.append(f'<text class="tick" x="{_x(i, n):.1f}" y="{H - 8}" text-anchor="middle">{html.escape(labels[i])}</text>')
    parts.append("</svg>")
    legend = "".join(f'<span class="key"><i style="background:{c}"></i>{html.escape(name)}</span>' for name, c in zip(series, colours))
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def bars(rounds: list[dict]) -> str:
    """Handoff success per round as a stacked bar: real handoffs in green, stubs in orange, kills marked."""
    if not rounds:
        return '<div class="empty">no supervisor rounds yet</div>'
    n = len(rounds)
    vmax = max(max(r["handoffs"] + r["stubs"], r["kills"]) for r in rounds) or 1
    bw = (W - PAD_L - PAD_R) / n
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" role="img" aria-label="handoffs per round">']
    for frac in (0, 0.5, 1):
        y = _y(vmax * frac, vmax)
        parts.append(f'<line class="gridline" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{_fmt(vmax * frac)}</text>')
    for i, r in enumerate(rounds):
        x0 = PAD_L + i * bw + bw * 0.2
        w = bw * 0.6
        y_h = _y(r["handoffs"], vmax)
        y_s = _y(r["handoffs"] + r["stubs"], vmax)
        base = _y(0, vmax)
        if r["handoffs"]:
            parts.append(f'<rect x="{x0:.1f}" y="{y_h:.1f}" width="{w:.1f}" height="{base - y_h:.1f}" fill="{PALETTE["s2"]}"/>')
        if r["stubs"]:
            parts.append(f'<rect x="{x0:.1f}" y="{y_s:.1f}" width="{w:.1f}" height="{y_h - y_s:.1f}" fill="{PALETTE["s1"]}"/>')
        if r["kills"]:
            parts.append(f'<text class="marker" x="{x0 + w / 2:.1f}" y="{y_s - 4:.1f}" text-anchor="middle" fill="{PALETTE["crit"]}">kill x{r["kills"]}</text>')
        label = f"r{r['number']}" + (" final" if r["final"] else "")
        parts.append(f'<text class="tick" x="{x0 + w / 2:.1f}" y="{H - 8}" text-anchor="middle">{label}</text>')
        if r["tuned"]:
            parts.append(f'<text class="marker" x="{x0 + w / 2:.1f}" y="{PAD_T + 10}" text-anchor="middle" fill="{PALETTE["s3"]}">tune {html.escape(r["tuned"])}</text>')
    parts.append("</svg>")
    legend = (f'<span class="key"><i style="background:{PALETTE["s2"]}"></i>real handoffs</span>'
              f'<span class="key"><i style="background:{PALETTE["s1"]}"></i>stubs</span>'
              f'<span class="key"><i style="background:{PALETTE["crit"]}"></i>kills</span>')
    return "".join(parts) + f'<div class="legend">{legend}</div>'


# --- page ---------------------------------------------------------------------------------------


def tile(label: str, value: str, note: str = "", tone: str = "") -> str:
    cls = f"tile {tone}".strip()
    return (f'<div class="{cls}"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(value)}</div>'
            f'<div class="note">{html.escape(note)}</div></div>')


def render(db_url: str, title: str) -> str:
    src = Source(db_url)
    try:
        beat = src.heartbeat()
        span = src.span()
        agents = src.agents()
        tasks = src.tasks()
        rounds = src.rounds()
        turns = src.events(("agent.turn",))
        knob_events = src.events(("knob.preset", "tool.civ_tune", "knob.reverted"))
        chaos_events = src.events(("operator.chaos", "chaos.killed", "chaos.board_down", "chaos.lag"))
        epochs = src.epoch_boundaries()
        inbox = src.inbox()
        spend = src.spend_by_role()
        knobs = src.knobs()
    finally:
        src.close()

    buckets = minute_buckets(*span) if span else []
    labels = [b.strftime("%H:%M") for b in buckets]

    def index_of(ts: dt.datetime) -> int | None:
        if not buckets:
            return None
        i = int((ts.replace(second=0, microsecond=0) - buckets[0]).total_seconds() // 60)
        return i if 0 <= i < len(buckets) else None

    epoch_markers = [{"index": index_of(e["ts"]), "label": f"epoch {e['epoch']}", "colour": PALETTE["s3"]} for e in epochs[1:]]
    chaos_markers = []
    for e in chaos_events:
        p = e["payload"] or {}
        label = p.get("action") or e["type"].split(".", 1)[1]
        if e["type"] == "operator.chaos" and p.get("agent_id"):
            label = f"kill {p['agent_id']}"
        chaos_markers.append({"index": index_of(e["ts"]), "label": label, "colour": PALETTE["crit"]})
    markers = epoch_markers + chaos_markers

    live = live_series(agents, buckets)
    done = done_series(tasks, buckets)
    tokens = tokens_series(turns, buckets)
    per_round = round_succession(rounds)

    # Tiles.
    if beat is None:
        hb_value, hb_note, hb_tone = "none", "the orchestrator has never written a heartbeat", "crit"
    else:
        age = beat["age_s"]
        hb_value = f"{int(round(age))} s"
        hb_note = f"pid {beat.get('pid', '?')}, epoch {beat.get('epoch', '?')}" + (", stopping" if beat.get("stopping") else "")
        hb_tone = "good" if age < 90 else "warn" if age < 300 else "crit"
    live_now = Counter(a["role"] for a in agents if a["status"] in ("live", "handing_off"))
    done_n = sum(1 for t in tasks if t["status"] == "done")
    scores = [float(t["score"]) for t in tasks if t["status"] == "done" and t["score"] is not None]
    mean = f"{sum(scores) / len(scores):.3f}" if scores else "n/a"
    handoffs_total = sum(r["handoffs"] for r in per_round)
    stubs_total = sum(r["stubs"] for r in per_round)
    rate = f"{handoffs_total / (handoffs_total + stubs_total):.0%}" if (handoffs_total + stubs_total) else "n/a"
    total_spend = sum(spend.values())
    current_epoch = epochs[-1]["epoch"] if epochs else (beat or {}).get("epoch", 1)

    tiles = "".join([
        tile("seconds since heartbeat", hb_value, hb_note, hb_tone),
        tile("live agents", f"{live_now.get('solver', 0)} / {live_now.get('supervisor', 0)}", "solvers / supervisors"),
        tile("tasks done", f"{done_n} of {len(tasks)}", f"verifier mean {mean}"),
        tile("handoff success", rate, f"{handoffs_total} real, {stubs_total} stubs, over {len(per_round)} rounds"),
        tile("spend", f"${total_spend:.2f}", ", ".join(f"{k} ${v:.2f}" for k, v in sorted(spend.items())) or "no ended sessions yet"),
        tile("epoch", str(current_epoch), f"{len(epochs)} seen in the events" if epochs else "no events yet"),
    ])

    # Knob timeline.
    knob_rows = []
    for e in knob_events[-40:]:
        p = e["payload"] or {}
        what = {"knob.preset": "preset", "tool.civ_tune": "tune", "knob.reverted": "revert"}[e["type"]]
        detail = p.get("knob", "")
        if e["type"] == "knob.reverted":
            detail += f" back to {p.get('to')}"
        else:
            detail += f" {p.get('from', '')} to {p.get('to', '')}"
        who = e["agent_id"] or "operator"
        knob_rows.append(f"<tr><td class=\"mono\">{e['ts']:%H:%M:%S}</td><td>{what}</td><td>{html.escape(str(detail))}</td>"
                         f"<td>{html.escape(str(who))}</td><td>{html.escape(str(p.get('reason', ''))[:80])}</td></tr>")
    knob_table = ("<table><tr><th>when</th><th>what</th><th>knob</th><th>by</th><th>reason</th></tr>" + "".join(knob_rows) + "</table>"
                  if knob_rows else '<div class="empty">no knob changes yet</div>')
    knob_now = "".join(f"<span class=\"pill\">{html.escape(k['knob'])} <b>{k['value']:g}</b>{' (' + html.escape(k['set_by']) + ')' if k['set_by'] else ''}</span>"
                       for k in knobs)

    # Chaos list.
    chaos_rows = "".join(
        f"<tr><td class=\"mono\">{e['ts']:%H:%M:%S}</td><td>{html.escape(e['type'])}</td>"
        f"<td>{html.escape(json.dumps(e['payload'], default=str)[:120])}</td></tr>" for e in chaos_events[-30:])
    chaos_table = ("<table><tr><th>when</th><th>event</th><th>detail</th></tr>" + chaos_rows + "</table>"
                   if chaos_rows else '<div class="empty">no chaos injected</div>')

    # Inbox.
    inbox_items = []
    for m in inbox:
        body = m["body"] or ""
        kind = "ping" if m["kind"] == "report" and body.startswith("[ping:") else m["kind"]
        inbox_items.append(f'<div class="msg"><div class="meta"><span class="pill {kind}">{kind}</span> '
                           f'<span class="mono">#{m["id"]} {m["created_at"]:%H:%M:%S}</span> {html.escape(m["author_id"])}</div>'
                           f'<pre>{html.escape(body[:700])}{"..." if len(body) > 700 else ""}</pre></div>')
    inbox_html = "".join(inbox_items) or '<div class="empty">nothing in the inbox</div>'

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    span_text = f"{span[0]:%Y-%m-%d %H:%M} to {span[1]:%H:%M}" if span else "no events"
    css_light = "; ".join(f"--{k}: {v}" for k, v in PALETTE.items())
    css_dark = "; ".join(f"--{k}: {v}" for k, v in DARK.items())

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; {css_light}; }}
  @media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ {css_dark}; }} }}
  :root[data-theme="dark"] {{ {css_dark}; }}
  body {{ margin: 0; padding: 0 16px 48px; background: var(--page); color: var(--ink);
         font: 14px/1.45 ui-sans-serif, -apple-system, "Helvetica Neue", Arial, sans-serif; }}
  header {{ max-width: 1180px; margin: 22px auto 14px; }}
  header h1 {{ font-size: 20px; font-weight: 650; margin: 0 0 2px; letter-spacing: -0.01em; }}
  header .sub {{ color: var(--muted); font-size: 12.5px; }}
  main {{ max-width: 1180px; margin: 0 auto; }}
  .tiles {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); margin-bottom: 14px; }}
  .tile {{ background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 12px 14px 10px; }}
  .tile .label {{ font-size: 11.5px; color: var(--ink2); }}
  .tile .value {{ font-size: 26px; font-weight: 650; letter-spacing: -0.02em; margin: 2px 0; }}
  .tile .note {{ font-size: 11.5px; color: var(--muted); }}
  .tile.good .value {{ color: var(--good); }} .tile.warn .value {{ color: var(--warn); }} .tile.crit .value {{ color: var(--crit); }}
  .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(12, 1fr); }}
  .card {{ grid-column: span 12; background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 14px 16px 12px; min-width: 0; }}
  .card.half {{ grid-column: span 6; }}
  @media (max-width: 860px) {{ .card.half {{ grid-column: span 12; }} }}
  .card h2 {{ font-size: 12.5px; font-weight: 600; color: var(--ink2); margin: 0 0 6px; }}
  .card h2 small {{ font-weight: 400; color: var(--muted); margin-left: 6px; }}
  .gridline {{ stroke: var(--grid); stroke-width: 1; }}
  .tick {{ font-size: 10.5px; fill: var(--muted); }}
  .marker {{ font-size: 10px; }}
  .legend {{ font-size: 11.5px; color: var(--ink2); margin-top: 4px; }}
  .key {{ margin-right: 12px; }} .key i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }}
  .empty {{ color: var(--muted); font-size: 12.5px; padding: 18px 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th {{ text-align: left; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--grid); padding: 4px 6px; }}
  td {{ padding: 4px 6px; border-bottom: 1px solid var(--grid); vertical-align: top; }}
  .mono {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }}
  .pill {{ display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px; background: var(--track); color: var(--ink2); margin: 2px 4px 2px 0; }}
  .pill.ping {{ background: var(--crit); color: #fff; }} .pill.alert {{ background: var(--serious); color: #fff; }}
  .pill.broadcast {{ background: var(--s3); color: #fff; }}
  .msg {{ border-bottom: 1px solid var(--grid); padding: 8px 0; }}
  .msg .meta {{ font-size: 12px; color: var(--ink2); margin-bottom: 2px; }}
  .msg pre {{ white-space: pre-wrap; margin: 0; font: 12px/1.4 ui-monospace, "SF Mono", Menlo, monospace; color: var(--ink); }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="sub">events {html.escape(span_text)} &middot; rendered {generated} &middot; reads the database, writes nothing</div>
</header>
<main>
  <div class="tiles">{tiles}</div>
  <div class="grid">
    <section class="card half"><h2>live agents <small>per minute; epoch and chaos markers</small></h2>
      {chart({"solvers": live["solver"], "supervisors": live["supervisor"]}, labels, [PALETTE["s1"], PALETTE["s3"]], markers, "no agents have run yet", kind="step")}</section>
    <section class="card half"><h2>tasks done <small>cumulative; verifier mean {html.escape(mean)}</small></h2>
      {chart({"done": done}, labels, [PALETTE["s2"]], markers, "no tasks completed yet", kind="step", ymax=float(max(len(tasks), 1)))}</section>
    <section class="card half"><h2>handoff success per round <small>real handoffs, stubs, kills</small></h2>{bars(per_round)}</section>
    <section class="card half"><h2>tokens per minute <small>all agents</small></h2>
      {chart({"tokens": tokens}, labels, [PALETTE["s4"]], markers, "no turns recorded yet")}</section>
    <section class="card"><h2>knobs <small>current values, then the timeline of presets, tunes and reverts</small></h2>
      <div style="margin-bottom:8px">{knob_now or '<span class="empty">no config rows</span>'}</div>{knob_table}</section>
    <section class="card half"><h2>chaos <small>operator faults and what the system did</small></h2>{chaos_table}</section>
    <section class="card half"><h2>inbox <small>reports, pings and alerts, newest first</small></h2>{inbox_html}</section>
  </div>
</main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--db-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    p.add_argument("--title", default="civ operator dashboard")
    args = p.parse_args(argv)
    page = render(args.db_url, args.title)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"dashboard: {args.out} ({len(page.encode('utf-8')) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
