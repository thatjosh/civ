"""The civ command line against civ_test: every command, its output, and the restore's effects."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from control.cli import civ
from harness.tests.conftest import REPO_ROOT, TEST_URL


@pytest.fixture
def control_dir(tmp_path) -> Path:
    # A copy, so a test that edits civ.toml never touches the repo's file.
    target = tmp_path / "control"
    target.mkdir()
    for name in ("ceilings.toml", "civ.toml"):
        shutil.copy(REPO_ROOT / "control" / name, target / name)
    return target


@pytest.fixture
def run(control_dir, capsys):
    def _run(*args: str) -> tuple[int, str]:
        code = civ.main(["--db-url", TEST_URL, "--control-dir", str(control_dir), *args])
        return code, capsys.readouterr().out
    return _run


def state(db, key):
    row = db.fetch_one("select value from control_state where key = %s", (key,))
    return row["value"] if row else None


def events(db, type):
    return db.fetch_all("select payload from events where type = %s order by seq", (type,))


# --- independence -------------------------------------------------------------------------------


def test_cli_imports_nothing_from_harness():
    source = Path(civ.__file__).read_text()
    assert not re.search(r"^\s*(from|import)\s+harness", source, re.M)
    assert "psycopg" in source and "tomllib" in source


def test_cli_help_runs_as_a_module():
    out = subprocess.run([sys.executable, "-m", "control.cli.civ", "--help"], capture_output=True, text=True, cwd=REPO_ROOT)
    assert out.returncode == 0 and "say" in out.stdout and "restore" in out.stdout


def test_bad_usage_exits_two(run):
    with pytest.raises(SystemExit) as exc:
        run("vote", "1")
    assert exc.value.code == 2


# --- say and vote --------------------------------------------------------------------------------


def test_say_broadcasts_and_logs(run, db):
    code, out = run("say", "look", "at", "the", "ledger")
    assert code == 0
    seq = int(re.fullmatch(r"broadcast #(\d+)\n", out).group(1))
    row = db.fetch_one("select author_id, kind, body from messages where id = %s", (seq,))
    assert row == {"author_id": "operator", "kind": "broadcast", "body": "look at the ledger"}
    assert events(db, "operator.say")[0]["payload"]["id"] == seq


def test_vote_pins_at_supervisor_weight_and_reports_who_missed_it(run, db, op, make_agent, board):
    solvers = [make_agent("solver")[0] for _ in range(4)]
    supervisor, _ = make_agent("supervisor")
    msg = board.post(solvers[0], "finding", "the mill burned in 1853")["id"]

    code, out = run("vote", str(msg), "+1", "--reason", "everyone build on this")
    assert code == 0 and re.fullmatch(rf"vote on #{msg} pinned until \d\d:\d\d:\d\d\n", out)
    vote = db.fetch_one("select value, weight, reason from votes where message_id = %s and voter_id = 'operator'", (msg,))
    # Four live solvers over one live supervisor.
    assert vote == {"value": 1, "weight": 4.0, "reason": "everyone build on this"}
    pin = db.fetch_one("select pinned_until > now() + interval '4 minutes' as long_enough from operator_pins where message_id = %s", (msg,))
    assert pin["long_enough"]  # operator_pin_s is 300 in civ.toml

    # Two of the five acknowledge; the report names the other three, the author included.
    board.ack(solvers[1], msg)
    board.ack(supervisor, msg)
    code, out = run("vote", "--report", str(msg))
    assert code == 0
    assert out.splitlines() == sorted([solvers[0].id, solvers[2].id, solvers[3].id])
    for a in (solvers[0], solvers[2], solvers[3]):
        board.ack(a, msg)
    assert run("vote", "--report", str(msg)) == (0, "all acknowledged\n")


def test_vote_on_a_missing_message_is_refused(run, db):
    code, out = run("vote", "999", "-1", "--reason", "no such thing")
    assert code == 1 and out == "no message #999\n"
    assert db.fetch_one("select count(*) as n from votes")["n"] == 0


def test_vote_weight_is_floored_at_one_with_no_live_agents(run, db, op):
    msg = db.fetch_one("insert into messages (author_id, kind, body) values ('operator', 'broadcast', 'x') returning id")["id"]
    run("vote", str(msg), "+1", "--reason", "steer")
    assert db.fetch_one("select weight from votes where message_id = %s", (msg,))["weight"] == 1.0


# --- inbox ---------------------------------------------------------------------------------------


def test_inbox_lists_pings_first_then_newest_first_and_marks_read(run, db, make_agent, board, civ_tools_factory=None):
    sup, _ = make_agent("supervisor")
    sol, _ = make_agent("solver")
    ids = {}
    with db.transaction() as conn:
        for key, kind, author, body in (
            ("alert", "alert", sol.id, "need the fire year"),
            ("report", "report", sup.id, "## Window\nquiet"),
            ("ping", "report", sup.id, "[ping: succession_failing] kills two windows running"),
        ):
            ids[key] = conn.execute("insert into messages (author_id, kind, body) values (%s, %s, %s) returning id",
                                    (author, kind, body)).fetchone()["id"]
    code, out = run("inbox")
    assert code == 0
    heads = [line for line in out.splitlines() if line.startswith("#")]
    assert heads == [
        f"#{ids['ping']} " + heads[0].split(" ", 2)[1] + f" ping {sup.id}",
        f"#{ids['report']} " + heads[1].split(" ", 2)[1] + f" report {sup.id}",
        f"#{ids['alert']} " + heads[2].split(" ", 2)[1] + f" alert {sol.id}",
    ]
    assert "  need the fire year" in out and "  quiet" in out
    assert state(db, "inbox_last_seq") == ids["ping"]
    assert run("inbox") == (0, "inbox empty\n")
    # --all shows everything again and marks nothing.
    code, out = run("inbox", "--all")
    assert out.count("\n#") + out.startswith("#") == 3 and state(db, "inbox_last_seq") == ids["ping"]


def test_inbox_limit_never_skips_older_unread(run, db, make_agent):
    sol, _ = make_agent("solver")
    with db.transaction() as conn:
        for i in range(5):
            conn.execute("insert into messages (author_id, kind, body) values (%s, 'alert', %s)", (sol.id, f"alert {i}"))
    code, out = run("inbox", "--limit", "2")
    assert "alert 0" in out and "alert 1" in out and "alert 2" not in out
    assert "and 3 more unread; run again" in out
    code, out = run("inbox", "--limit", "2")
    assert "alert 2" in out and "alert 3" in out


def test_inbox_names_unacknowledged_steers_of_expired_pins(run, db, make_agent, board):
    sol, _ = make_agent("solver")
    other, _ = make_agent("solver")
    msg = board.post(sol, "finding", "x")["id"]
    run("vote", str(msg), "+1", "--reason", "read this")
    board.ack(other, msg)
    # Move the whole episode into the past: agents spawned ten minutes ago, the steer cast five
    # minutes ago, its pin expired a minute ago. Both agents were live for the window.
    db.execute("update agents set spawned_at = now() - interval '10 minutes'")
    db.execute("update votes set created_at = now() - interval '5 minutes' where message_id = %s", (msg,))
    db.execute("update operator_pins set pinned_until = now() - interval '1 minute' where message_id = %s", (msg,))
    code, out = run("inbox")
    assert f"unacknowledged steers (last 24 h):\n#{msg}: {sol.id}" in out


# --- status, pause, resume ------------------------------------------------------------------------


def test_status_shows_epoch_pause_agents_tasks_shape_and_knobs(run, db, make_agent, task):
    make_agent("solver")
    run("pause", "--reason", "looking at T-004")
    code, out = run("status")
    assert code == 0
    assert out.startswith("epoch 1  paused yes (looking at T-004)\n")
    assert "agents: 1 solvers, 0 supervisors live (ceiling 8)" in out
    assert "tasks: open 1, leased 0, done 0, blocked 0" in out
    assert "tokens/min: 0 (ceiling 3000000)" in out
    assert "spend 24h: $0.00 ended sessions (ceiling $5.00; live sessions not included)" in out
    assert "shape: solvers 3, supervisors 1, round 600s, max_generations 5 (from civ.toml)" in out
    assert re.search(r"^handoff_pct\s+80\s+80\s*$", out, re.M)
    assert re.search(r"^post_rate_per_h\s+30\s*$", out, re.M)  # no civ.toml seed for it


def test_pause_and_resume_write_control_state(run, db):
    assert run("pause") == (0, "paused\n")
    assert state(db, "paused") == {"on": True, "reason": ""}
    assert run("pause", "--reason", "cost") == (0, "paused (cost)\n")
    assert state(db, "paused") == {"on": True, "reason": "cost"}
    assert run("resume") == (0, "resumed\n")
    assert state(db, "paused") is False
    assert len(events(db, "operator.pause")) == 2 and len(events(db, "operator.resume")) == 1


# --- reshape ---------------------------------------------------------------------------------------


def test_reshape_writes_the_shape_bumps_the_epoch_and_diffs(run, db, control_dir):
    code, out = run("reshape")
    assert code == 0 and "solvers none -> 3" in out and out.rstrip().endswith("epoch 1 -> 2")
    shape = state(db, "shape")
    # Stored nested, the way civ.toml is written and the orchestrator reads it back.
    assert shape["population"]["solvers"] == 3 and shape["governance"]["min_sample"] == 3 and "written_at" in shape
    assert run("reshape") == (0, "shape unchanged; epoch stays 2\n")
    text = (control_dir / "civ.toml").read_text().replace("solvers = 3", "solvers = 5")
    (control_dir / "civ.toml").write_text(text)
    assert run("reshape") == (0, "solvers 3 -> 5\nepoch 2 -> 3\n")
    assert state(db, "epoch") == 3 and state(db, "shape")["population"]["solvers"] == 5
    change = events(db, "operator.reshape")[-1]["payload"]
    assert change == {"changes": [{"key": "solvers", "from": 3, "to": 5}], "epoch": 3}


# --- restore ---------------------------------------------------------------------------------------


@pytest.fixture
def mid_flight(db, op, make_agent, board, tasks):
    """A board caught mid-run: one lineage handed off cleanly and its successor is live with a lease,
    one lineage is live with no task, one ended naturally, and a knob has been tuned."""
    a1, _ = make_agent("solver", agent_id="sol-001", lineage="L-001")
    op.create_task("T-001", "Count", "count things")
    op.create_task("T-002", "Sum", "sum things")
    board.post(a1, "finding", "first fact")
    board.post(a1, "finding", "second fact")
    db.execute("update agents set status = 'ended', ended_at = now() where id = 'sol-001'")
    db.execute("insert into handoffs (lineage_id, from_agent, to_agent, generation, body) values ('L-001', 'sol-001', 'sol-001-g2', 1, 'real')")
    a1g2, _ = make_agent("solver", agent_id="sol-001-g2", lineage="L-001", generation=2)
    assert tasks.claim(a1g2)["task"]["id"] == "T-001"
    board.post(a1g2, "finding", "third fact")
    a2, _ = make_agent("solver", agent_id="sol-002", lineage="L-002")
    a3, _ = make_agent("solver", agent_id="sol-003", lineage="L-003")
    db.execute("update agents set status = 'ended', ended_at = now() where id = 'sol-003'")
    db.execute("update config set value = 85, set_by = 'sup-001-r1', reason = 'test', ttl_s = 3600, expires_at = now() + interval '1 hour' where knob = 'handoff_pct'")
    db.execute("update config set value = 10, set_by = 'sup-001-r1', reason = 'test', ttl_s = 3600, expires_at = now() + interval '1 hour' where knob = 'top_k'")
    db.execute("update tasks set attempts = 2 where id = 'T-001'")
    return {"g2": a1g2, "a2": a2}


def test_restore_is_refused_without_yes_and_changes_nothing(run, db, mid_flight):
    code, out = run("restore")
    assert code == 1 and "--yes" in out
    assert state(db, "epoch") == 1
    assert db.fetch_one("select count(*) as n from handoffs")["n"] == 1
    assert db.fetch_one("select status from tasks where id = 'T-001'")["status"] == "leased"


def test_restore_bumps_the_epoch(run, db, mid_flight):
    run("restore", "--yes")
    assert state(db, "epoch") == 2
    assert db.fetch_one("select epoch from events where type = 'operator.restore'")["epoch"] == 2


def test_restore_stubs_every_lineage_caught_in_flight_and_marks_it_dead(run, db, mid_flight):
    run("restore", "--yes")
    stubs = db.fetch_all("select lineage_id, from_agent, to_agent, generation, stub, body from handoffs where stub order by lineage_id")
    assert [(s["lineage_id"], s["from_agent"], s["to_agent"], s["generation"]) for s in stubs] == [
        ("L-001", "sol-001-g2", None, 2), ("L-002", "sol-002", None, 1),
    ]
    for heading in civ.STUB_HEADINGS:
        assert re.search(rf"^## {heading}$", stubs[0]["body"], re.M)
    assert "Held task: T-001." in stubs[0]["body"] and "seq" in stubs[0]["body"]
    assert "Held no task." in stubs[1]["body"]
    statuses = {r["id"]: r["status"] for r in db.fetch_all("select id, status from agents")}
    assert statuses == {"sol-001": "ended", "sol-001-g2": "dead", "sol-002": "dead", "sol-003": "ended"}


def test_restore_releases_leases_without_touching_attempts(run, db, mid_flight):
    run("restore", "--yes")
    row = db.fetch_one("select status, lease_owner, lease_expires, attempts, next_attempt_at <= now() as ready from tasks where id = 'T-001'")
    assert row == {"status": "open", "lease_owner": None, "lease_expires": None, "attempts": 2, "ready": True}


def test_restore_resets_seeded_knobs_and_clears_every_tune(run, db, mid_flight):
    run("restore", "--yes")
    handoff = db.fetch_one("select value, set_by, ttl_s, expires_at from config where knob = 'handoff_pct'")
    assert handoff == {"value": 80.0, "set_by": None, "ttl_s": None, "expires_at": None}
    # top_k is not in civ.toml: it keeps its value but loses the tune.
    top_k = db.fetch_one("select value, set_by, expires_at from config where knob = 'top_k'")
    assert top_k == {"value": 10.0, "set_by": None, "expires_at": None}


def test_restore_broadcasts_and_records_itself(run, db, mid_flight):
    code, out = run("restore", "--yes")
    assert code == 0
    assert re.fullmatch(r"Restored at \d\d:\d\d:\d\d into epoch 2: 2 lineages queued for stubs, 1 leases released, "
                        r"about [\d.]+ minutes of activity after \d\d:\d\d:\d\d are lost\n", out)
    broadcast = db.fetch_one("select body from messages where kind = 'broadcast' order by id desc limit 1")["body"]
    assert broadcast == out.strip()
    record = state(db, "last_restore")
    assert record["epoch"] == 2 and record["lineages_stubbed"] == 2 and record["leases_released"] == 1
    assert "minutes_lost" in record and "at" in record


def test_restore_retires_a_mid_round_supervisor_without_a_stub(run, db, make_agent, mid_flight):
    make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")
    code, out = run("restore", "--yes")
    assert code == 0
    row = db.fetch_one("select status from agents where id = 'sup-001-r1'")
    assert row["status"] == "dead"
    assert db.fetch_one("select count(*) as n from handoffs where lineage_id = 'S-001'")["n"] == 0
    assert state(db, "last_restore")["supervisors_retired"] == ["sup-001-r1"]


# --- merge (M6) -----------------------------------------------------------------------------------


@pytest.fixture
def waiting_merge(db, make_agent, op):
    """A proposal queued for the operator and a plain queued one, as civ_merge would leave them."""
    author = make_agent("solver", agent_id="sol-001", lineage="L-001")[0]
    sup = make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")[0]
    rid = op.open_round(1)
    with db.transaction() as conn:
        p1 = conn.execute("insert into messages (author_id, kind, body, refs) values (%s, 'proposal', 'touch the spawner', '{shared/aa-a.diff}') returning id",
                          (author.id,)).fetchone()["id"]
        p2 = conn.execute("insert into messages (author_id, kind, body, refs) values (%s, 'proposal', 'a prompt fix', '{shared/bb-b.diff}') returning id",
                          (author.id,)).fetchone()["id"]
        m1 = conn.execute(
            "insert into merges (proposal_id, requested_by, round_id, diff_ref, paths, status, detail) "
            "values (%s, %s, %s, 'shared/aa-a.diff', '{harness/orchestrator/spawner.py}', 'queued_for_operator', 'protected paths: harness/orchestrator/spawner.py') returning id",
            (p1, sup.id, rid)).fetchone()["id"]
        m2 = conn.execute(
            "insert into merges (proposal_id, requested_by, round_id, diff_ref, paths, status) "
            "values (%s, %s, %s, 'shared/bb-b.diff', '{harness/prompts/handoff_request.md,harness/prompts/roles/solver.md}', 'queued') returning id",
            (p2, sup.id, rid)).fetchone()["id"]
    return {"p1": p1, "p2": p2, "m1": m1, "m2": m2}


def test_merge_list_prints_every_merge_still_waiting(run, waiting_merge):
    w = waiting_merge
    assert run("merge", "--list") == (0, f"#{w['m1']} proposal #{w['p1']} queued_for_operator harness/orchestrator/spawner.py\n"
                                          f"#{w['m2']} proposal #{w['p2']} queued harness/prompts/handoff_request.md,harness/prompts/roles/solver.md\n")


def test_merge_list_when_nothing_waits(run):
    assert run("merge", "--list") == (0, "no merges waiting\n")


def test_merge_approve_hands_a_protected_merge_to_the_merger(run, db, waiting_merge):
    w = waiting_merge
    assert run("merge", "--approve", str(w["m1"])) == (0, f"merge #{w['m1']} approved; the merger will run it\n")
    row = db.fetch_one("select status, detail from merges where id = %s", (w["m1"],))
    assert row["status"] == "queued" and row["detail"].endswith("[approved by operator]")
    assert events(db, "operator.merge_approve")[-1]["payload"]["merge_id"] == w["m1"]
    # Only a merge waiting on the operator can be approved.
    code, out = run("merge", "--approve", str(w["m2"]))
    assert code == 1 and out == f"merge #{w['m2']} is not queued for the operator\n"


def test_merge_reject_records_the_reason(run, db, waiting_merge):
    w = waiting_merge
    assert run("merge", "--reject", str(w["m1"]), "--reason", "not now") == (0, f"merge #{w['m1']} rejected\n")
    row = db.fetch_one("select status, detail, finished_at from merges where id = %s", (w["m1"],))
    assert row["status"] == "rejected" and row["detail"] == "not now" and row["finished_at"] is not None
    assert events(db, "operator.merge_reject")[-1]["payload"]["reason"] == "not now"
    code, out = run("merge", "--reject", str(w["m1"]), "--reason", "again")
    assert code == 1 and "cannot be rejected" in out


def test_merge_usage_errors_exit_two(run, waiting_merge):
    with pytest.raises(SystemExit) as exc:
        run("merge")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        run("merge", "--reject", "1")
    assert exc.value.code == 2
