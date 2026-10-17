"""Migration 0004: control_state seeds, the current_epoch() function, and the epoch stamp."""

from __future__ import annotations


def test_control_state_seeds_exist(db):
    rows = {r["key"]: r["value"] for r in db.fetch_all("select key, value from control_state")}
    assert rows == {"epoch": 1, "paused": False, "inbox_last_seq": 0}


def test_current_epoch_follows_control_state(db):
    assert db.fetch_one("select current_epoch() as e")["e"] == 1
    db.execute("update control_state set value = '7' where key = 'epoch'")
    assert db.fetch_one("select current_epoch() as e")["e"] == 7
    # A missing row falls back to 1 rather than failing every insert that stamps the epoch.
    db.execute("delete from control_state where key = 'epoch'")
    assert db.fetch_one("select current_epoch() as e")["e"] == 1


def test_agents_and_events_are_stamped_with_the_epoch_at_write_time(db, op, make_agent):
    before, _ = make_agent("solver")
    op.log_event(before.id, "x.before", {})
    db.execute("update control_state set value = '2' where key = 'epoch'")
    after, _ = make_agent("solver")
    op.log_event(after.id, "x.after", {})
    epochs = {r["id"]: r["epoch"] for r in db.fetch_all("select id, epoch from agents")}
    assert epochs == {before.id: 1, after.id: 2}
    events = {r["type"]: r["epoch"] for r in db.fetch_all("select type, epoch from events where type like 'x.%'")}
    assert events == {"x.before": 1, "x.after": 2}
    # Old rows keep their epoch: the stamp records when a thing happened, it is not a live pointer.
    assert db.fetch_one("select epoch from agents where id = %s", (before.id,))["epoch"] == 1
