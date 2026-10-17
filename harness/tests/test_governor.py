"""The governor: the pause flag, the three waiting ceilings, the spend halt, and the file cache."""

from __future__ import annotations

import os

import pytest

from harness.orchestrator.governor import CeilingsFile, Governor, SpendCeilingReached, is_paused
from harness.tests.control_fixtures import CEILINGS_TOML, write_control_dir
from harness.tests.orchestrator_fakes import FakeOperator


class Ticker:
    """A fake sleep that runs a script of side effects, one per poll, and counts the polls."""

    def __init__(self, *effects):
        self.effects = list(effects)
        self.polls = 0

    async def __call__(self, seconds: float) -> None:
        self.polls += 1
        if self.effects:
            self.effects.pop(0)()


def make(tmp_path, op: FakeOperator | None = None, sleep=None, ceilings: str = CEILINGS_TOML):
    op = op or FakeOperator()
    control = write_control_dir(tmp_path, ceilings=ceilings)
    gov = Governor(op, control / "ceilings.toml", poll_s=0.0, sleep=sleep or Ticker())
    return gov, op, control


def events(op: FakeOperator, type: str) -> list[dict]:
    return [e[2] for e in op.events if e[1] == type]


async def test_clear_path_returns_at_once_and_logs_nothing(tmp_path):
    gov, op, _ = make(tmp_path)
    await gov.acquire("solver", replaces_slot=False)
    assert events(op, "governor.wait") == [] and events(op, "governor.resumed") == []


async def test_pause_holds_a_spawn_until_resumed_with_one_wait_event(tmp_path):
    op = FakeOperator()
    op.control["paused"] = {"on": True, "reason": "operator looking at T-005"}
    # Three polls pass before the CLI flips the flag back.
    ticker = Ticker(lambda: None, lambda: None, lambda: op.control.update(paused=False))
    gov, op, _ = make(tmp_path, op, sleep=ticker)
    await gov.acquire("solver", replaces_slot=False)
    assert ticker.polls == 3
    waits = events(op, "governor.wait")
    assert len(waits) == 1 and waits[0]["reason"] == "paused" and waits[0]["role"] == "solver"
    assert events(op, "governor.resumed")[0]["reason"] == "paused"


def test_is_paused_accepts_both_encodings():
    assert is_paused(True) and is_paused({"on": True, "reason": "x"})
    assert not is_paused(False) and not is_paused({"on": False}) and not is_paused(None)


async def test_live_agent_ceiling_holds_a_new_agent_but_not_a_successor(tmp_path):
    op = FakeOperator()
    for i in range(8):  # max_live_agents is 8 in the contract's file
        op.register_agent(f"sol-{i:03d}", "solver", f"L-{i:03d}", "/tmp")
    ticker = Ticker(lambda: op.set_agent_status("sol-000", "ended"))
    gov, op, _ = make(tmp_path, op, sleep=ticker)
    # A successor replaces its predecessor's slot, so the full population does not hold it.
    await gov.acquire("solver", replaces_slot=True)
    assert ticker.polls == 0
    # A brand-new agent waits until one ends.
    await gov.acquire("solver", replaces_slot=False)
    assert ticker.polls == 1
    wait = events(op, "governor.wait")[0]
    assert wait == {"reason": "live_agents", "value": 8, "limit": 8, "role": "solver"}


async def test_supervisors_count_toward_the_live_ceiling(tmp_path):
    op = FakeOperator()
    for i in range(7):
        op.register_agent(f"sol-{i:03d}", "solver", f"L-{i:03d}", "/tmp")
    op.register_agent("sup-001-r1", "supervisor", "S-001", "/tmp")
    ticker = Ticker(lambda: op.set_agent_status("sup-001-r1", "ended"))
    gov, op, _ = make(tmp_path, op, sleep=ticker)
    await gov.acquire("supervisor", replaces_slot=False)
    assert ticker.polls == 1 and events(op, "governor.wait")[0]["reason"] == "live_agents"


async def test_token_ceiling_waits_for_the_minute_to_pass(tmp_path):
    op = FakeOperator()
    op.tpm = 3_000_001
    ticker = Ticker(lambda: None, lambda: setattr(op, "tpm", 10))
    gov, op, _ = make(tmp_path, op, sleep=ticker)
    await gov.acquire("solver", replaces_slot=True)
    assert ticker.polls == 2
    wait = events(op, "governor.wait")[0]
    assert wait["reason"] == "tokens_per_minute" and wait["value"] == 3_000_001 and wait["limit"] == 3_000_000


async def test_spend_ceiling_raises_and_logs_a_halt(tmp_path):
    op = FakeOperator()
    op.spend = 5.0
    gov, op, _ = make(tmp_path, op)
    with pytest.raises(SpendCeilingReached) as exc:
        await gov.acquire("solver", replaces_slot=False)
    assert "5.00" in str(exc.value)
    halt = events(op, "governor.halt")[0]
    assert halt["reason"] == "spend" and halt["limit"] == 5.0 and events(op, "governor.wait") == []


async def test_checks_run_in_the_contract_order(tmp_path):
    # Paused and over spend at once: the pause is reported, and the spend check never runs while paused.
    op = FakeOperator()
    op.control["paused"] = True
    op.spend = 99.0
    ticker = Ticker(lambda: op.control.update(paused=False), lambda: None)
    gov, op, _ = make(tmp_path, op, sleep=ticker)
    with pytest.raises(SpendCeilingReached):
        await gov.acquire("solver", replaces_slot=False)
    assert [e["reason"] for e in events(op, "governor.wait")] == ["paused"]
    assert ticker.polls == 1


async def test_ceilings_file_is_re_read_when_its_mtime_changes(tmp_path):
    gov, op, control = make(tmp_path)
    path = control / "ceilings.toml"
    assert gov.ceilings.max_live_agents == 8
    path.write_text(CEILINGS_TOML.replace("max_live_agents = 8", "max_live_agents = 1"), encoding="utf-8")
    # Force a visible mtime change even on a coarse filesystem clock.
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert gov.ceilings.max_live_agents == 1
    # And the new limit bites at the next acquire.
    op.register_agent("sol-001", "solver", "L-001", "/tmp")
    ticker = Ticker(lambda: op.set_agent_status("sol-001", "ended"))
    gov._sleep = ticker  # noqa: SLF001 - the test swaps the sleep to count polls
    await gov.acquire("solver", replaces_slot=False)
    assert ticker.polls == 1


def test_missing_or_incomplete_ceilings_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        Governor(FakeOperator(), tmp_path / "control" / "ceilings.toml")
    with pytest.raises(ValueError) as exc:
        CeilingsFile(write_control_dir(tmp_path, ceilings="max_live_agents = 8\n") / "ceilings.toml").read()
    assert "max_tokens_per_minute" in str(exc.value)
