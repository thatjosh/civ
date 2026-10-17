import pytest

from harness.orchestrator.monitor import Monitor, StaticKnobs
from harness.tests.orchestrator_fakes import FakeOperator, assistant, init_message, result


def test_context_pct_sums_all_input_fields_and_writes_once_per_turn():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=1_000)
    m.observe(assistant(context_tokens=250, output_tokens=5))
    assert m.totals.context_pct == 25.0
    assert m.totals.tokens_in == 250 and m.totals.tokens_out == 5
    assert op.usage == [("sol-001", 250, 5, 25.0)]
    assert [e[1] for e in op.events] == ["agent.turn"]


def test_tokens_accumulate_and_context_tracks_latest_turn():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=1_000)
    m.observe(assistant(100, 1))
    m.observe(assistant(400, 2))
    assert m.totals.turns == 2
    assert m.totals.tokens_in == 500 and m.totals.tokens_out == 3
    # Context fill is the size of the latest prompt, not the running total.
    assert m.totals.context_pct == 40.0


def test_threshold_fires_once_per_crossing_in_order():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=100)
    fired: list[tuple[float, str, float]] = []
    m.on_threshold(80, lambda agent, pct: fired.append((80, agent, pct)))
    m.on_threshold(90, lambda agent, pct: fired.append((90, agent, pct)))
    # Small steps, because a first turn straight to 50 would now trigger the growth-aware early
    # handoff (50 + 50 >= 90); this test is about the plain thresholds firing once, in order.
    for size in (30, 55, 70, 81, 85, 92, 95):
        m.observe(assistant(size))
    assert [f[0] for f in fired] == [80, 90]
    assert fired[0][1] == "sol-001" and fired[0][2] == 81.0


def test_result_records_cost_and_marks_ended():
    op = FakeOperator()
    m = Monitor(op, "sol-001")
    m.observe(result(num_turns=4, cost=0.42))
    assert m.totals.ended and m.totals.cost_usd == 0.42
    assert op.status == [("sol-001", "ended")]
    assert op.events[-1][1] == "agent.ended" and op.events[-1][2]["num_turns"] == 4


def test_messages_without_usage_are_ignored():
    op = FakeOperator()
    m = Monitor(op, "sol-001")
    m.observe(init_message())
    a = assistant(10)
    a.usage = None
    m.observe(a)
    assert m.totals.turns == 0 and op.usage == []


def test_thresholds_come_from_the_knobs():
    m = Monitor(FakeOperator(), "sol-001", knobs=StaticKnobs({"handoff_pct": 60, "kill_pct": 75}))
    assert m.handoff_pct == 60.0 and m.kill_pct == 75.0
    assert Monitor(FakeOperator(), "sol-001").handoff_pct == 80.0


def test_forced_handoff_fires_the_handoff_level_at_the_turn_count_only_once():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=1_000_000, force_handoff_after_turns=3)
    fired: list[float] = []
    m.on_threshold(m.handoff_pct, lambda _a, pct: fired.append(pct))
    m.on_threshold(m.kill_pct, lambda _a, pct: fired.append(-1))
    for _ in range(5):
        m.observe(assistant(10))
    # The handoff level fired exactly once, at turn 3, and the kill level never did.
    assert len(fired) == 1 and fired[0] < 1
    forced = [e for e in op.events if e[1] == "agent.handoff_forced"]
    assert len(forced) == 1 and forced[0][2]["turn"] == 3


def test_probe_is_due_every_five_turns_and_once_past_seventy_percent():
    m = Monitor(FakeOperator(), "sol-001", context_window=100)
    due: list[int] = []
    for i, size in enumerate((10, 10, 10, 10, 10, 10, 75, 76, 77, 10), start=1):
        m.observe(assistant(size))
        if m.probe_due():
            due.append(i)
            m.apply_probe({"totalTokens": 0})
    # Turns 5 and 10 by cadence, turn 7 for the first crossing of seventy percent, not 8 or 9 again.
    assert due == [5, 7, 10]


def test_probe_raises_the_estimate_but_never_lowers_it():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=1_000)
    m.observe(assistant(500))
    m.apply_probe({"totalTokens": 300, "percentage": 0.03})
    assert m.totals.context_pct == 50.0 and m.totals.probe_pct == 30.0
    m.apply_probe({"totalTokens": 900, "percentage": 0.09})
    assert m.totals.context_pct == 90.0
    assert op.usage[-1] == ("sol-001", 500, 10, 90.0)
    assert [e[1] for e in op.events].count("agent.context_probe") == 2


def test_compaction_state_is_a_problem_only_when_it_would_fire_first():
    m = Monitor(FakeOperator(), "sol-001")
    assert m.note_compaction_state({"isAutoCompactEnabled": False}) is False
    # Threshold in tokens against the window: 950k of 1M is 95 percent, above our 80, so no problem.
    assert m.note_compaction_state({"isAutoCompactEnabled": True, "autoCompactThreshold": 950_000, "maxTokens": 1_000_000}) is False
    assert m.note_compaction_state({"isAutoCompactEnabled": True, "autoCompactThreshold": 700_000, "maxTokens": 1_000_000}) is True
    # Enabled with no threshold reported is treated as a problem, because nothing proves it fires late.
    assert m.note_compaction_state({"isAutoCompactEnabled": True}) is True
    assert m.note_compaction_state(None) is False


def test_estimate_cost_prices_each_token_category_from_the_table():
    from harness.orchestrator.monitor import estimate_cost_usd

    # 1M of each category on Haiku: 0.10 + 0.50 + 0.01 + 0.125.
    assert estimate_cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000) == pytest.approx(0.735)
    assert estimate_cost_usd("claude-opus-5-5-20270101", 1_000_000, 0, 0, 0) == pytest.approx(4.0)
    assert estimate_cost_usd("gpt-9", 1, 1, 1, 1) is None and estimate_cost_usd(None, 1, 1, 1, 1) is None


def test_finish_without_result_estimates_cost_and_logs_the_end_once():
    op = FakeOperator()
    m = Monitor(op, "sol-001")
    # assistant(100) splits as input 50, cache write 25, cache read 25, output 10.
    m.observe(assistant(100, model="claude-haiku-5-5"))
    m.observe(assistant(100, model="claude-haiku-5-5"))
    m.finish_without_result("interrupted")
    m.finish_without_result("interrupted")
    expected = (100 * 0.10 + 20 * 0.50 + 50 * 0.01 + 50 * 0.125) / 1_000_000
    assert m.totals.ended and m.totals.cost_estimated and m.totals.cost_usd == pytest.approx(expected)
    ends = [e for e in op.events if e[1] == "agent.ended"]
    assert len(ends) == 1 and ends[0][2]["estimated"] is True and ends[0][2]["num_turns"] == 2
    assert op.status == [("sol-001", "ended")]


def test_finish_without_result_on_the_error_path_does_not_mark_ended():
    op = FakeOperator()
    m = Monitor(op, "sol-001")
    m.observe(assistant(100))
    m.finish_without_result("no_result", mark_ended=False)
    assert op.status == [] and op.events[-1][2]["is_error"] is True


def _handoff_spy(m: Monitor):
    fired: list[float] = []
    m.on_threshold(m.handoff_pct, lambda _agent, pct: fired.append(pct))
    m.on_threshold(m.kill_pct, lambda _agent, pct: None)
    return fired


def test_early_handoff_fires_when_one_more_turn_like_the_last_would_cross_the_kill():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=100)
    fired = _handoff_spy(m)
    m.observe(assistant(40))   # 40 percent, delta 40: 80 < 90, no
    assert fired == []
    m.observe(assistant(70))   # 70 percent, delta 30: 100 >= 90 and 70 >= 40, yes
    assert fired == [70.0] and m.totals.handoff_early is True
    early = [e for e in op.events if e[1] == "agent.handoff_early"]
    assert len(early) == 1 and early[0][2] == {"pct": 70.0, "last_delta": 30.0, "kill_pct": 90.0}
    # Reaching the ordinary threshold later does not fire the same level twice.
    m.observe(assistant(85))
    assert fired == [70.0]


def test_early_handoff_does_not_fire_on_a_small_delta_or_below_half_the_threshold():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=100)
    fired = _handoff_spy(m)
    m.observe(assistant(60))   # delta 60: 120 >= 90 and 60 >= 40: fires... so start lower instead
    assert fired == [60.0]
    op2 = FakeOperator()
    m2 = Monitor(op2, "sol-002", context_window=100)
    fired2 = _handoff_spy(m2)
    m2.observe(assistant(30))  # delta 30: 60 < 90, no
    m2.observe(assistant(50))  # delta 20: 70 < 90, no
    m2.observe(assistant(75))  # delta 25: 100 >= 90, yes
    assert fired2 == [75.0]
    # The floor clause, with knobs where it can bind: kill at 30 and handoff at 80 (floor 40).
    op3 = FakeOperator()
    m3 = Monitor(op3, "sol-003", context_window=100, knobs=StaticKnobs({"kill_pct": 30.0}))
    fired3 = _handoff_spy(m3)
    m3.observe(assistant(35))  # 35 + 35 >= 30 but 35 < 40: no
    assert fired3 == []
    m3.observe(assistant(45))  # 45 + 10 >= 30 and 45 >= 40: yes
    assert fired3 == [45.0]


def test_ordinary_threshold_wins_over_early_when_both_are_true_in_one_turn():
    op = FakeOperator()
    m = Monitor(op, "sol-001", context_window=100)
    fired = _handoff_spy(m)
    m.observe(assistant(85))   # past 80 outright: the ordinary threshold fires, not the early one
    assert fired == [85.0] and m.totals.handoff_early is False
    assert not [e for e in op.events if e[1] == "agent.handoff_early"]


def test_kill_grace_s_comes_from_the_knobs():
    m = Monitor(FakeOperator(), "sol-001", knobs=StaticKnobs({"kill_grace_s": 45}))
    assert m.kill_grace_s == 45.0
    assert Monitor(FakeOperator(), "sol-002").kill_grace_s == 20.0
