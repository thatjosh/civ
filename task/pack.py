"""Where a task pack lives: materials, tasks and the hidden answers.

Two layouts exist. The repo's own pack keeps its answers under ``task/verifier/`` because the
verifier is the one part of the pack agents never read, and a generated pack (``task/packs/<name>/``)
keeps ``materials/``, ``tasks.json`` and ``answers.json`` side by side because it is produced and
consumed as a unit. The server, the orchestrator and the seeder agree on which pack is in play
through one environment variable, ``CIV_PACK``, so the three processes cannot drift apart.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "CIV_PACK"
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Pack:
    root: Path

    @classmethod
    def default(cls) -> "Pack":
        """The repo's Hollowmere pack under ``task/``."""
        return cls(REPO_ROOT / "task")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Pack":
        """The pack named by ``CIV_PACK`` (a directory), else the default."""
        value = (env if env is not None else os.environ).get(ENV_VAR)
        if value:
            return cls(Path(value).expanduser().resolve())
        return cls.default()

    # A generated pack has answers.json at its root; the repo pack keeps them under verifier/.
    @property
    def generated(self) -> bool:
        return (self.root / "answers.json").exists() or not (self.root / "verifier" / "answers.json").exists()

    @property
    def materials_dir(self) -> Path:
        return self.root / "materials"

    @property
    def tasks_path(self) -> Path:
        return self.root / "tasks.json"

    @property
    def answers_path(self) -> Path:
        return self.root / "answers.json" if self.generated else self.root / "verifier" / "answers.json"

    @property
    def name(self) -> str:
        try:
            return str(json.loads(self.tasks_path.read_text(encoding="utf-8")).get("pack") or self.root.name)
        except (OSError, ValueError):
            return self.root.name

    def tasks(self) -> list[dict]:
        return list(json.loads(self.tasks_path.read_text(encoding="utf-8"))["tasks"])

    def answers(self) -> dict:
        return dict(json.loads(self.answers_path.read_text(encoding="utf-8")))
