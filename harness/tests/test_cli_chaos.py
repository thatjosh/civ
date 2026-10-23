"""civ chaos and the heartbeat line of civ status, against civ_test (M7)."""

from __future__ import annotations

import json

import pytest

from harness.tests.test_cli import control_dir, run, state, events  # noqa: F401  (fixtures and helpers)


def test_chaos_show_on_a_clean_board(run):
    code, out = run("chaos", "show")
    assert code == 0
    assert out == "kill: none\nboard_down_until: none\nlag_ms: 0\n"


def test_chaos_kill_queues_a_live_agent_and_logs(run, db, make_agent):
    sol, _ = make_agent("solver", agent_id="sol-001", lineage="L-001")
    code, out = run("chaos", "kill", "sol-001")
    assert code == 0 and out == "chaos: kill queued for sol-001\n"
    chaos = state(db, "chaos")
    assert chaos["kill"] == ["sol-001"] and chaos["board_down_until"] is None and chaos["lag_ms"] == 0
    assert events(db, "operator.chaos")[-1]["payload"] == {"action": "kill", "agent_id": "sol-001"}
    # Queued once, however many times the operator asks.
    run("chaos", "kill", "sol-001")
    assert state(db, "chaos")["kill"] == ["sol-001"]


def test_chaos_kill_refuses_an_unknown_or_ended_agent(run, db, make_agent, op):
    code, out = run("chaos", "kill", "sol-404")
    assert code == 1 and out == "no agent sol-404\n"
    sol, _ = make_agent("solver", agent_id="sol-002", lineage="L-002")
    op.set_agent_status("sol-002", "ended")
    code, out = run("chaos", "kill", "sol-002")
    assert code == 1 and "not live" in out
    assert state(db, "chaos") is None


def test_chaos_kill_random_solver_picks_a_live_solver_only(run, db, make_agent):
    make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")
    code, out = run("chaos", "kill", "random-solver")
    assert code == 1 and out == "no live solver to kill\n"
    make_agent("solver", agent_id="sol-003", lineage="L-003")
    code, out = run("chaos", "kill", "random-solver")
    assert code == 0 and out == "chaos: kill queued for sol-003\n"


def test_chaos_board_down_sets_a_deadline_and_merges_with_other_faults(run, db, make_agent):
    make_agent("solver", agent_id="sol-004", lineage="L-004")
    run("chaos", "kill", "sol-004")
    code, out = run("chaos", "board-down", "20")
    assert code == 0 and out.startswith("chaos: board down until ") and out.endswith("(20 s)\n")
    chaos = state(db, "chaos")
    assert chaos["kill"] == ["sol-004"], "a merge, not a replace"
    row = db.fetch_one("select %s::timestamptz > now() + interval '15 seconds' as later", (chaos["board_down_until"],))
    assert row["later"]
    assert events(db, "operator.chaos")[-1]["payload"]["action"] == "board-down"
    code, out = run("chaos", "board-down", "0")
    assert code == 1 and "positive" in out


def test_chaos_lag_sets_and_zero_clears(run, db):
    code, out = run("chaos", "lag", "250")
    assert code == 0 and out == "chaos: lag 250 ms on every tool result\n"
    assert state(db, "chaos")["lag_ms"] == 250
    code, out = run("chaos", "lag", "0")
    assert code == 0 and out == "chaos: lag cleared\n"
    assert state(db, "chaos")["lag_ms"] == 0


def test_chaos_clear_resets_everything(run, db, make_agent):
    make_agent("solver", agent_id="sol-005", lineage="L-005")
    run("chaos", "kill", "sol-005")
    run("chaos", "lag", "100")
    code, out = run("chaos", "clear")
    assert code == 0 and out == "chaos: cleared\n"
    assert state(db, "chaos") == {"kill": [], "board_down_until": None, "lag_ms": 0}
    code, out = run("chaos", "show")
    assert out == "kill: none\nboard_down_until: none\nlag_ms: 0\n"


def test_chaos_usage_errors_exit_two(run):
    for bad in (("chaos", "kill"), ("chaos", "lag", "fast"), ("chaos", "board-down")):
        with pytest.raises(SystemExit) as exc:
            run(*bad)
        assert exc.value.code == 2


# --- the heartbeat line ---------------------------------------------------------------------------


def test_status_heartbeat_none_when_the_orchestrator_never_beat(run):
    code, out = run("status")
    assert code == 0 and out.splitlines()[0] == "heartbeat: none"


def test_status_heartbeat_reports_age_pid_and_epoch(run, db):
    with db.transaction() as conn:
        beat = conn.execute("select (now() - interval '42 seconds') as at").fetchone()["at"].isoformat()
        conn.execute("insert into control_state (key, value) values ('heartbeat', %s::jsonb) "
                     "on conflict (key) do update set value = excluded.value",
                     (json.dumps({"at": beat, "pid": 4242, "epoch": 3, "live": {"solver": 2, "supervisor": 0}}),))
    code, out = run("status")
    first = out.splitlines()[0]
    assert first.startswith("heartbeat: 4") and first.endswith("s ago (pid 4242, epoch 3)")
    age = int(first.split()[1])
    assert 41 <= age <= 45
