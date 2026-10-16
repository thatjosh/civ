"""The brief renderer: deterministic markdown, bounded size, graceful with missing data."""

from __future__ import annotations

from harness.orchestrator.brief import MIN_BODY_CHARS, TUNED_LINE, render_brief


def incident(i: int, classification: str = "no_chance") -> dict:
    return {"agent_id": f"sol-{i:03d}", "lineage_id": f"L-{i:03d}", "generation": 1, "reason": "kill threshold reached",
            "requested": classification != "never_asked", "turns_since_request": 0 if classification == "no_chance" else None,
            "seconds_since_request": 2.7, "grace_turns": 0, "context_pct_at_request": 81.2,
            "tool_calls_after_request": ["tool.board_read"], "handoff_refusals": [], "task": f"T-{i:03d}",
            "classification": classification}


def full_brief(incidents: int = 2) -> dict:
    slo = lambda v, n: {"value": v, "n": n}  # noqa: E731
    return {
        "round": {"id": 3, "number": 3, "final": False, "window_start": "2026-10-16T10:00:00", "window_end": "2026-10-16T10:10:00", "supervisors": 1},
        "status": {"live_agents": {"solver": 3, "supervisor": 1}, "tokens_per_min": 1200, "tasks": {"open": 1, "leased": 2, "done": 4, "blocked": 1}},
        "slo": {"current": {"completions_per_mtok": slo(12.5, 4), "blocked_failed_rate": slo(0.25, 8), "duplicate_rate": slo(0.1, 10),
                            "lease_wait_median_s": slo(3.2, 8), "handoff_success_rate": slo(0.333, 3)},
                "previous": {"completions_per_mtok": slo(10.0, 3), "blocked_failed_rate": slo(0.0, 6), "duplicate_rate": slo(0.0, 7),
                             "lease_wait_median_s": slo(2.0, 6), "handoff_success_rate": slo(None, 0)},
                "min_sample": 3,
                "history": [{"round": 1, "handoff_success_rate": None}, {"round": 2, "handoff_success_rate": 1.0}]},
        "succession": {"requests": 3, "handoffs": 1, "stubs": 2, "kills": 2, "success_rate": 0.333, "median_turns_to_handoff": 2,
                       "median_seconds_to_handoff": 5.4, "refusals": {"handoff is missing headings: ## Open threads": 1},
                       "generations": {"L-001": 2, "L-002": 3}, "incidents": [incident(i) for i in range(1, incidents + 1)]},
        "blockers": [{"id": 17, "thread_id": 4, "task_id": "T-004", "author_id": "sol-002", "body": "Cannot find the payees. " * 30, "score": 1.25, "created_at": "2026-10-16T10:05:00"}],
        "blocked_tasks": [{"id": "T-005", "title": "Rebuilding", "attempts": 3, "last_release_reason": "no source for the rebuilding cost"}],
        "alerts": [{"id": 21, "author_id": "sol-003", "body": "The ledger has two 1852 sections", "created_at": "2026-10-16T10:06:00"}],
        "previous_reports": [{"id": 30, "author_id": "sup-001-r2", "body": "## Window\nRound 2. " + "x" * 1000}],
        "tunes": {"active": [{"knob": "kill_grace_turns", "value": 1, "seed": 2, "set_by": "sup-001-r2", "reason": "two kills with 0 turns", "expires_at": "2026-10-16T11:00:00"}],
                  "pending": [{"knob": "handoff_pct", "value": 65, "proposed_by": "sup-002-r2", "round_number": 2}],
                  "round_tuned": True},
        "verifier": {"done": 4, "total": 8, "mean_score": 0.9375, "lowest": [{"id": "T-005", "score": 0.67, "notes": "missed the cost"}]},
    }


def test_sections_appear_in_the_contract_order_with_their_content():
    out = render_brief(full_brief())
    headings = ["## Round 3", "## Status", "## SLOs", "## Succession", "## Blockers", "## Alerts", "## Previous round's reports", "## Knobs", "## Verifier progress"]
    positions = [out.index(h) for h in headings]
    assert positions == sorted(positions)
    # SLO table has current and previous with n.
    assert "| completions_per_mtok | 12.5 | 4 | 10 | 3 |" in out
    assert "| handoff_success_rate | 0.333 | 3 | none | 0 |" in out
    assert "min_sample is 3" in out
    # Each incident is its own subsection with the classification in bold.
    assert out.count("### Kill:") == 2 and "**no_chance**" in out and "seconds since request: 2.7" in out
    assert "handoff is missing headings" in out and "L-002 at 3" in out
    # Blockers carry seq, task id and score; blocked tasks their last reason.
    assert "seq 17 [T-004] by sol-002 in thread 4 (score 1.25)" in out and "T-005 (Rebuilding), attempts 3" in out
    assert "seq 21 by sol-003" in out and "Report seq 30 by sup-001-r2" in out
    assert "kill_grace_turns = 1 (seed 2)" in out and "handoff_pct to 65, proposed by sup-002-r2 in round 2" in out
    assert out.rstrip().splitlines()[-1].startswith("- Done 4 of 8, mean score 0.938") or "Done 4 of 8" in out
    assert TUNED_LINE in out


def test_rendering_is_deterministic_and_handles_an_empty_brief():
    assert render_brief(full_brief()) == render_brief(full_brief())
    out = render_brief({})
    for h in ("## Round ?", "## Status", "## SLOs", "## Succession", "No kills this window.", "## Blockers", "No blockers this window.",
              "## Alerts", "None.", "## Previous round's reports", "## Knobs", "Every knob is at its seed.", "## Verifier progress"):
        assert h in out
    assert TUNED_LINE not in out
    assert render_brief(None) == render_brief({})


def test_first_round_says_there_is_no_previous_window():
    brief = full_brief()
    brief["slo"]["previous"] = None
    brief["tunes"]["round_tuned"] = False
    out = render_brief(brief)
    assert "No previous window" in out and "| none |  |" in out
    assert TUNED_LINE not in out


def test_final_round_is_marked():
    brief = full_brief()
    brief["round"]["final"] = True
    assert "(final round" in render_brief(brief)


def test_ten_incidents_stay_under_six_thousand_tokens_and_the_rest_is_truncated():
    brief = full_brief(incidents=12)
    brief["blockers"] = brief["blockers"] * 15
    brief["alerts"] = brief["alerts"] * 15
    out = render_brief(brief)
    assert len(out) / 4 < 6000
    assert out.count("### Kill:") == 10 and "And 2 more kills not shown" in out
    assert "- and 5 more" in out  # blockers and alerts both cut at ten


def test_bodies_are_clipped_but_never_below_two_hundred_characters():
    brief = full_brief()
    long_body = "word " * 200
    brief["blockers"][0]["body"] = long_body
    brief["previous_reports"][0]["body"] = long_body
    out = render_brief(brief)
    blocker_line = next(line for line in out.splitlines() if "seq 17" in line)
    assert blocker_line.endswith("...") and len(blocker_line) < 420
    # The report keeps its first 600 characters; the blocker its first 300.
    assert ("word " * 110).strip()[:550] in out
    assert MIN_BODY_CHARS == 200
