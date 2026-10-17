"""The civilisation's launch-time shape: control/civ.toml, read by the orchestrator.

The shape is the third settings tier. Ceilings cap what the system may ever do and knobs move
at runtime under the supervisors; the shape (how many of each role, how often a round runs,
where the handoff fires, how fast the board forgets) changes slowly enough that only the
operator moves it, with `civ reshape`. The orchestrator reads the file at start, writes it to
control_state so `civ status` can show the shape a run is using, seeds the knob-valued keys
into the config table, and then watches control_state for a reshape.
"""

from __future__ import annotations

import logging
import time
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Which section of civ.toml each field lives in; the file is nested, the dataclass is flat.
SECTIONS: dict[str, tuple[str, ...]] = {
    "population": ("solvers", "supervisors"),
    "cadence": ("supervisor_round_s",),
    "lifecycle": ("max_generations", "handoff_pct", "kill_pct", "kill_grace_turns"),
    "voting": ("decay_offset_h", "decay_power", "operator_pin_s"),
    "governance": ("vote_quota", "min_sample", "big_move_steps", "ttl_default_s"),
}
# The keys that are also soft knobs. These are seeded into the config table at launch; the
# rest are read by the orchestrator, the CLI or the server directly from the shape.
KNOB_KEYS = ("handoff_pct", "kill_pct", "kill_grace_turns", "decay_offset_h", "decay_power",
             "vote_quota", "min_sample", "big_move_steps")


@dataclass(frozen=True)
class Shape:
    solvers: int = 3
    supervisors: int = 1
    supervisor_round_s: float = 600.0
    max_generations: int = 5
    handoff_pct: float = 80.0
    kill_pct: float = 90.0
    kill_grace_turns: int = 2
    decay_offset_h: float = 2.0
    decay_power: float = 1.5
    operator_pin_s: int = 300
    vote_quota: int = 1
    min_sample: int = 3
    big_move_steps: int = 2
    ttl_default_s: int = 3600

    def knob_seeds(self) -> dict[str, float]:
        return {key: float(getattr(self, key)) for key in KNOB_KEYS}

    def to_json(self) -> dict[str, Any]:
        """The nested civ.toml structure, which is what control_state.shape holds."""
        flat = asdict(self)
        return {section: {key: flat[key] for key in keys} for section, keys in SECTIONS.items()}

    @classmethod
    def from_mapping(cls, data: dict[str, Any], source: str = "civ.toml") -> "Shape":
        """Build a shape from the nested mapping (a parsed toml file or control_state.shape).

        Unknown keys are logged and ignored rather than refused, because an operator adding a
        key the orchestrator does not know yet should not stop the run; a wrong type is an error.
        """
        values: dict[str, Any] = {}
        known = {f.name: f.type for f in fields(cls)}
        for section, keys in SECTIONS.items():
            block = data.get(section) or {}
            if not isinstance(block, dict):
                raise ValueError(f"{source}: [{section}] must be a table")
            for key, value in block.items():
                if key not in keys:
                    log.warning("%s: unknown key %s.%s ignored", source, section, key)
                    continue
                values[key] = _coerce(known[key], key, value, source)
        for section in data:
            if section not in SECTIONS and section != "written_at":
                log.warning("%s: unknown section [%s] ignored", source, section)
        return cls(**values)

    def changes_from(self, other: "Shape") -> dict[str, tuple[Any, Any]]:
        """key -> (old, new) for every field that differs, for the reshape report."""
        mine, theirs = asdict(self), asdict(other)
        return {k: (theirs[k], mine[k]) for k in mine if mine[k] != theirs[k]}

    def with_overrides(self, **overrides: Any) -> "Shape":
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})


def _coerce(annotation: Any, key: str, value: Any, source: str) -> Any:
    kind = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{source}: {key} must be a number, got {value!r}")
    return int(value) if kind == "int" else float(value)


def load_shape(path: Path | str) -> Shape:
    path = Path(path)
    with path.open("rb") as handle:
        return Shape.from_mapping(tomllib.load(handle), source=str(path))


class ShapeReader:
    """The shape and epoch as the database currently has them, re-read every poll_s seconds.

    `civ reshape` writes a new shape and bumps the epoch from another process; the population
    and the cron ask this reader at their decision points (a succession, the start of a round),
    so a reshape takes effect lineage by lineage without anything being restarted.
    """

    def __init__(self, operator: Any, initial: Shape, poll_s: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._op = operator
        self._initial = initial
        self._poll_s = float(poll_s)
        self._clock = clock
        self._shape = initial
        self._epoch: int | None = None
        self._read_at: float | None = None

    def current(self) -> Shape:
        self._refresh()
        return self._shape

    def epoch(self) -> int | None:
        self._refresh()
        return self._epoch

    def invalidate(self) -> None:
        self._read_at = None

    def _refresh(self) -> None:
        now = self._clock()
        if self._read_at is not None and now - self._read_at < self._poll_s:
            return
        self._read_at = now
        try:
            stored = self._op.control_get("shape")
            if stored:
                self._shape = Shape.from_mapping(dict(stored), source="control_state.shape")
            epoch = self._op.epoch()
            self._epoch = int(epoch) if epoch is not None else None
        except Exception as exc:  # noqa: BLE001 - a read failure keeps the last known shape
            log.warning("shape read failed, keeping the last shape: %s", exc)
