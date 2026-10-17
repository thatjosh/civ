"""Hard ceilings from control/ceilings.toml, as the board server sees them.

The ceilings file is operator-owned and outside the harness: the orchestrator's governor reads it
before every spawn, and the server reads it here when validating a tune, so a knob can never be
set above a ceiling even by two supervisors acting together. The file is re-read when its mtime
changes, because an operator edit must take effect without a restart. A missing file is tolerated
on the server side (the check is skipped with a warning) so that a board can run without a
control directory, as the tests and the replay do; the orchestrator is stricter and refuses to
start without one.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("civ.ceilings")

CEILING_NAMES = ("max_live_agents", "max_tokens_per_minute", "max_spend_per_day_usd", "max_handoff_tokens", "model")
CEILINGS_FILE = "ceilings.toml"


def control_dir(root: Path | None = None) -> Path:
    """CIV_CONTROL_DIR wins; otherwise <CIV_ROOT or the repo>/control."""
    env = os.environ.get("CIV_CONTROL_DIR")
    if env:
        return Path(env)
    base = root or Path(os.environ.get("CIV_ROOT", Path(__file__).resolve().parents[2]))
    return Path(base) / "control"


class Ceilings:
    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path is not None else control_dir() / CEILINGS_FILE
        self._values: dict[str, Any] = {}
        self._mtime: float | None = None
        self._warned = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def available(self) -> bool:
        self._refresh()
        return self._mtime is not None

    def get(self, name: str) -> Any | None:
        """The ceiling, or None when the file is missing or does not set it."""
        self._refresh()
        return self._values.get(name)

    def as_dict(self) -> dict[str, Any]:
        self._refresh()
        return dict(self._values)

    def _refresh(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            if not self._warned:
                log.warning("no ceilings file at %s; ceiling checks are skipped", self._path)
                self._warned = True
            self._values, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        with self._path.open("rb") as handle:
            self._values = tomllib.load(handle)
        self._mtime = mtime
        self._warned = False
