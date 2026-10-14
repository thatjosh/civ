from harness.orchestrator.monitor import Monitor
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
    for size in (50, 81, 85, 92, 95):
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
