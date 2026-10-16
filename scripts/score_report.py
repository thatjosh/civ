"""Score a supervisor replay record against a rubric.

    python scripts/score_report.py docs/demo/2026-10-16-replay-opus-1.json [--rubric docs/eval/m3-kills.rubric.json]

The rubric is data (docs/eval/*.rubric.json) so a second recording gets a second rubric and the
same engine. Each item names a check:

  text              any of the regexes appears in the report
  text_near         any of the regexes appears within `window` characters of `anchor`; a
                    classification only counts next to the agent it is about, so swapping the
                    two agents' causes scores zero rather than two
  tune_direction    a tool.civ_tune event moved a succession knob the right way, or (with no
                    succession tune at all) the report explains why it did not tune
  no_ping           no report.ping event
  no_unrelated_tune no tool.civ_tune event on a knob outside the rubric's succession set
  headings          every listed `## heading` is present

Prints one line per item with the evidence and the total. Always exits 0: the score is the
output, not a verdict.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUBRIC = ROOT / "docs" / "eval" / "m3-kills.rubric.json"
FLAGS = re.IGNORECASE | re.MULTILINE


def _first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, FLAGS)
        if m:
            return _snippet(text, m.start(), m.end())
    return None


def _snippet(text: str, start: int, end: int, pad: int = 40) -> str:
    lo, hi = max(0, start - pad), min(len(text), end + pad)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def _scoped_report(item: dict, record: dict) -> str:
    """The report text a text check may look at. An item with "sections" sees only those headed
    sections, because a control-arm supervisor named sol-002 while praising its T-005 correction
    and that must not count as naming it in a diagnosis."""
    report = record.get("report") or ""
    sections = item.get("sections")
    if not sections:
        return report
    parts = []
    for name in sections:
        m = re.search(rf"^##\s+{re.escape(name)}\s*$\n(.*?)(?=^##\s|\Z)", report, re.M | re.S)
        if m:
            parts.append(m.group(1))
    return "\n".join(parts)


def check_text(item: dict, record: dict) -> tuple[bool, str]:
    hit = _first_match(item["any"], _scoped_report(item, record))
    where = " in " + "/".join(item["sections"]) if item.get("sections") else " in the report"
    return (hit is not None, hit or f"no match{where}")


def check_text_near(item: dict, record: dict) -> tuple[bool, str]:
    report = _scoped_report(item, record)
    window = int(item.get("window", 600))
    anchors = [m.start() for m in re.finditer(re.escape(item["anchor"]), report, FLAGS)]
    if not anchors:
        return False, f"{item['anchor']} is not mentioned"
    for pos in anchors:
        region = report[max(0, pos - window): pos + window]
        hit = _first_match(item["any"], region)
        if hit:
            return True, hit
    return False, f"{item['anchor']} is mentioned but no cause is given near it"


def _direction(tune: dict) -> str | None:
    try:
        before, after = float(tune.get("from")), float(tune.get("to"))
    except (TypeError, ValueError):
        return None
    return "up" if after > before else "down" if after < before else "flat"


def check_tune_direction(item: dict, record: dict, rubric: dict) -> tuple[bool, str]:
    right = rubric.get("right_direction", {})
    succession = set(rubric.get("succession_knobs", []))
    tunes = [t for t in record.get("tunes") or [] if t.get("knob") in succession]
    for tune in tunes:
        direction = _direction(tune)
        wanted = right.get(tune.get("knob"))
        if wanted and direction == wanted:
            return True, f"tuned {tune['knob']} {tune.get('from')} -> {tune.get('to')}"
        if wanted and direction and direction != wanted:
            return False, f"tuned {tune['knob']} the wrong way ({tune.get('from')} -> {tune.get('to')}, wanted {wanted})"
    if tunes:
        return False, f"tuned {tunes[0]['knob']} but the direction could not be read"
    hit = _first_match(item.get("no_tune_any", []), record.get("report") or "")
    if hit:
        return True, f"no tune, explained: {hit}"
    return False, "no succession tune and no explanation for holding off"


def check_no_ping(item: dict, record: dict) -> tuple[bool, str]:
    pings = record.get("pings") or []
    if pings:
        return False, f"{len(pings)} ping(s): {json.dumps(pings[0])[:120]}"
    return True, "no ping"


def check_no_unrelated_tune(item: dict, record: dict, rubric: dict) -> tuple[bool, str]:
    succession = set(rubric.get("succession_knobs", []))
    unrelated = [t for t in record.get("tunes") or [] if t.get("knob") not in succession]
    if unrelated:
        return False, "tuned " + ", ".join(str(t.get("knob")) for t in unrelated)
    return True, "no unrelated tune"


def check_headings(item: dict, record: dict) -> tuple[bool, str]:
    report = record.get("report") or ""
    missing = [h for h in item["headings"] if not re.search(rf"^##\s+{re.escape(h)}\s*$", report, FLAGS)]
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, "all present"


def score(record: dict, rubric: dict) -> list[dict[str, Any]]:
    results = []
    for item in rubric["items"]:
        kind = item["check"]
        if kind == "text":
            ok, evidence = check_text(item, record)
        elif kind == "text_near":
            ok, evidence = check_text_near(item, record)
        elif kind == "tune_direction":
            ok, evidence = check_tune_direction(item, record, rubric)
        elif kind == "no_ping":
            ok, evidence = check_no_ping(item, record)
        elif kind == "no_unrelated_tune":
            ok, evidence = check_no_unrelated_tune(item, record, rubric)
        elif kind == "headings":
            ok, evidence = check_headings(item, record)
        else:
            ok, evidence = False, f"unknown check {kind}"
        results.append({"id": item["id"], "points": int(item.get("points", 1)), "pass": ok,
                        "description": item["description"], "evidence": evidence})
    return results


def format_table(results: list[dict[str, Any]], record: dict, rubric: dict) -> str:
    width = max(len(r["id"]) for r in results)
    lines = [f"{'item':<{width}}  pass  evidence"]
    for r in results:
        lines.append(f"{r['id']:<{width}}  {'yes' if r['pass'] else 'no ':<4}  {r['evidence']}")
    got = sum(r["points"] for r in results if r["pass"])
    total = sum(r["points"] for r in results)
    label = f"{record.get('model', '?')} trial {record.get('trial', '?')}" + (" (control)" if record.get("control") else "")
    lines.append(f"score {got}/{total}  {rubric.get('name', '')}  {label}  cost ${float(record.get('cost_usd') or 0):.4f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("record", type=Path, nargs="+", help="replay json file(s)")
    p.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    p.add_argument("--json", action="store_true", help="print the per-item results as json instead of a table")
    args = p.parse_args(argv)
    rubric = json.loads(args.rubric.read_text(encoding="utf-8"))
    for path in args.record:
        record = json.loads(path.read_text(encoding="utf-8"))
        results = score(record, rubric)
        if args.json:
            print(json.dumps({"record": str(path), "results": results,
                              "score": sum(r["points"] for r in results if r["pass"]),
                              "total": sum(r["points"] for r in results)}, indent=2))
        else:
            print(path)
            print(format_table(results, record, rubric))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
