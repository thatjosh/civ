"""The rules of the merge gate that both halves apply: which files a diff touches, which of them
the operator has set aside, and the merge section of control/civ.toml.

They live in one module because the server (civ_merge) decides where a proposal queues and the
orchestrator's merger re-checks the same diff before it touches the repository; two copies of the
path matching would be the kind of rule that drifts apart exactly where it matters.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_MAX_PER_ROUND = 2
DEFAULT_PROTECTED_PATHS = (
    "harness/orchestrator/spawner.py", "harness/orchestrator/governor.py", "harness/orchestrator/merger.py",
    "harness/board_mcp/ceilings.py", "harness/board_mcp/auth.py", "harness/board_mcp/tools/task.py",
    "harness/board_mcp/merge_rules.py", "control/", "task/", "scripts/",
)
DEFAULT_TEST_COMMAND = "python -m pytest harness/tests -q -x -p no:cacheprovider"
HARNESS_PREFIX = "harness/"

# Unified diff headers. `diff --git a/x b/x` names the file once, `+++ b/x` names the new side; a
# deleted file has `+++ /dev/null`, so the `---` side is read when nothing else named a file.
_GIT_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)\s*$", re.M)
_NEW_SIDE = re.compile(r"^\+\+\+ (?:b/)?(\S+)", re.M)
_OLD_SIDE = re.compile(r"^--- (?:a/)?(\S+)", re.M)


def paths_in_diff(text: str) -> list[str]:
    """Every file a unified diff touches, in order of first appearance, without a/ b/ prefixes."""
    found: list[str] = []

    def add(path: str) -> None:
        if path and path != "/dev/null" and path not in found:
            found.append(path)

    for m in _GIT_HEADER.finditer(text or ""):
        add(m.group(2))
    for m in _NEW_SIDE.finditer(text or ""):
        add(m.group(1))
    if not found:
        for m in _OLD_SIDE.finditer(text or ""):
            add(m.group(1))
    return found


def protected(paths: list[str], protected_paths: list[str] | tuple[str, ...]) -> list[str]:
    """The paths the operator has set aside: equal to an entry, or under an entry ending in a slash."""
    out = []
    for path in paths:
        for rule in protected_paths:
            if path == rule or (rule.endswith("/") and path.startswith(rule)):
                out.append(path)
                break
    return out


# The CLI appends this to a merge's detail when the operator approves a protected-path merge. The
# CLI imports nothing from the harness, so it carries the same literal; a test pins the two.
APPROVED_MARKER = "[approved by operator]"


def operator_approved(row: dict[str, Any]) -> bool:
    """Whether the operator has approved this merge, which is what lets a protected path land."""
    return APPROVED_MARKER in str(row.get("detail") or "")


def outside_harness(paths: list[str]) -> list[str]:
    """Anything a proposal names that is not under harness/: the agents' code ends there."""
    return [p for p in paths if not p.startswith(HARNESS_PREFIX)]


def load_merge_section(shape_json: dict[str, Any] | None) -> dict[str, Any]:
    """The [merge] table of civ.toml as stored in control_state.shape, with the contract's defaults
    for anything missing, so a run without the section still has a gate."""
    section = (shape_json or {}).get("merge") or {}
    paths = section.get("protected_paths")
    return {
        "max_per_round": int(section.get("max_per_round", DEFAULT_MAX_PER_ROUND)),
        "protected_paths": list(paths) if paths else list(DEFAULT_PROTECTED_PATHS),
        "test_command": str(section.get("test_command") or DEFAULT_TEST_COMMAND),
    }
