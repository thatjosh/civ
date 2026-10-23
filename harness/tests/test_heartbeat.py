"""The heartbeat row: written at start, on the interval, and once more as "stopping" at exit."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from harness.orchestrator.heartbeat import Heartbeat
from harness.tests.orchestrator_fakes import FakeOperator


def test_beat_writes_the_contract_shape():
    op = FakeOperator()
    op.register_agent("sol-001", "solver", "L-001", "/tmp/a")
    op.control["epoch"] = 3
    clock = lambda: datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    hb = Heartbeat(op, 30, pid=4242, harness_version=lambda: "0.1.4", clock=clock)
    row = hb.beat()
    assert row == {"at": "2026-10-23T12:00:00+00:00", "pid": 4242, "epoch": 3, "live": {"solver": 1, "supervisor": 0},
                   "harness_version": "0.1.4"}
    assert op.control["heartbeat"] == row and "stopping" not in row
    assert hb.beat(stopping=True)["stopping"] is True


async def test_run_until_beats_on_the_interval_and_marks_the_stop():
    op = FakeOperator()
    done = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            done.set()

    hb = Heartbeat(op, 7.5, pid=1, sleep=sleep)
    beats = await hb.run_until(done)
    # One beat before the first sleep, one after it, none after the sleep that saw done, then the
    # stopping beat: three rows written, the last of them flagged.
    assert beats == 3 and slept == [7.5, 7.5]
    assert op.control["heartbeat"]["stopping"] is True
    assert op.heartbeat_age_s() is not None and op.heartbeat_age_s() < 5


async def test_stopping_beat_is_written_even_when_the_loop_is_cancelled():
    op = FakeOperator()
    done = asyncio.Event()
    hb = Heartbeat(op, 1000, pid=1, sleep=lambda s: asyncio.sleep(s))
    task = asyncio.create_task(hb.run_until(done))
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert op.control["heartbeat"]["stopping"] is True


def test_heartbeat_age_is_none_without_a_row():
    assert FakeOperator().heartbeat_age_s() is None
