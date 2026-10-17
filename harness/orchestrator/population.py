"""The population: the lineage slots the orchestrator keeps filled.

A slot is an index; slot i runs lineage L-00i starting from agent sol-00i. At start the
population fills `shape.solvers` slots, after a restore it first continues the lineages the
database says were mid-flight and then fills the rest. While it runs it watches the shape:
when `civ reshape` raises `solvers` it starts the new slots at the stagger, and when it lowers
them the lineages above the target are not given a successor at their next succession, so a
reshape is applied lineage by lineage and nothing already running is cut.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from harness.orchestrator.shape import ShapeReader

log = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]
LINEAGE_INDEX = re.compile(r"^L-(\d+)$")


def lineage_ids(index: int) -> tuple[str, str]:
    """(lineage id, first agent id) for a slot: L-003 and sol-003."""
    return f"L-{index:03d}", f"sol-{index:03d}"


def lineage_index(lineage_id: str) -> int:
    m = LINEAGE_INDEX.match(lineage_id)
    if not m:
        raise ValueError(f"not a solver lineage id: {lineage_id!r}")
    return int(m.group(1))


class Population:
    """Runs the solver lineages and keeps their number in step with the shape."""

    def __init__(
        self,
        runner: Any,
        shape_reader: ShapeReader,
        stagger_s: float = 2.0,
        resume_rows: list[dict[str, Any]] | None = None,
        poll_s: float = 5.0,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._runner = runner
        self._shape = shape_reader
        self._stagger_s = float(stagger_s)
        self._resume_rows = list(resume_rows or [])
        self._poll_s = float(poll_s)
        self._sleep = sleep
        self._tasks: dict[int, asyncio.Task[Any]] = {}

    @property
    def target(self) -> int:
        return int(self._shape.current().solvers)

    def slot_open(self, lineage_id: str) -> bool:
        """Whether a lineage may be given a successor: false once a reshape has moved its slot
        above the target, which is how a smaller population is reached without killing anyone."""
        try:
            return lineage_index(lineage_id) <= self.target
        except ValueError:
            return True

    async def run(self) -> list[Any]:
        for row in self._resume_rows:
            try:
                index = lineage_index(row["lineage_id"])
            except ValueError:
                # Not a solver slot (the restore drill once handed us S-001): nothing to resume.
                log.warning("not resuming %s: not a solver lineage", row["lineage_id"])
                continue
            self._tasks[index] = asyncio.create_task(
                self._runner.resume(row["lineage_id"], row["from_agent"], row))
            log.info("resuming lineage %s from %s", row["lineage_id"], row["from_agent"])
        await self._fill()
        while self._tasks:
            pending = [t for t in self._tasks.values() if not t.done()]
            if not pending:
                break
            # Wake on a lineage ending or on the poll, whichever is first, then check the shape.
            await asyncio.wait(pending, timeout=self._poll_s, return_when=asyncio.FIRST_COMPLETED)
            await self._fill()
        results = []
        for index in sorted(self._tasks):
            results.append(await self._tasks[index])
        return results

    async def _fill(self) -> None:
        """Start lineages for every slot up to the target that has none yet, at the stagger.

        Slots are only ever added here. A lineage whose task has finished keeps its slot
        occupied: restarting a slot whose lineage ended naturally would spawn an agent into an
        empty queue, and one that ended by error would loop on the error.
        """
        started = False
        for index in range(1, self.target + 1):
            if index in self._tasks:
                continue
            if started and self._stagger_s > 0:
                await self._sleep(self._stagger_s)
            lineage_id, agent_id = lineage_ids(index)
            # By keyword: a spawner and a runner take the two ids in different orders.
            self._tasks[index] = asyncio.create_task(self._runner.run(lineage_id=lineage_id, agent_id=agent_id))
            log.info("started lineage %s (slot %d of %d)", lineage_id, index, self.target)
            started = True
