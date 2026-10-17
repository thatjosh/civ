"""M5: the ceilings loader and the two rules civ_tune takes from the operator's files.

The ceiling check and the TTL default each have a test that fails if the rule is removed."""

from __future__ import annotations

import os
import time
from pathlib import Path

from harness.board_mcp.ceilings import Ceilings, control_dir
from harness.board_mcp.tools.civ import CivTools, DEFAULT_TTL_S

CEILINGS = """
max_live_agents = 8
max_tokens_per_minute = 3000000
max_spend_per_day_usd = 5.0
max_handoff_tokens = {handoff}
model = "claude-opus-5-5"
"""


def write_ceilings(path: Path, handoff: int) -> Path:
    path.write_text(CEILINGS.format(handoff=handoff), encoding="utf-8")
    return path


def test_loader_reads_the_file_and_reloads_on_mtime(tmp_path):
    path = write_ceilings(tmp_path / "ceilings.toml", 4500)
    ceilings = Ceilings(path)
    assert ceilings.available and ceilings.get("max_handoff_tokens") == 4500 and ceilings.get("model") == "claude-opus-5-5"
    assert ceilings.as_dict()["max_live_agents"] == 8
    # An operator edit takes effect without a restart: a newer mtime triggers a reload.
    write_ceilings(path, 6000)
    later = time.time() + 5
    os.utime(path, (later, later))
    assert ceilings.get("max_handoff_tokens") == 6000


def test_missing_file_reads_as_none(tmp_path):
    ceilings = Ceilings(tmp_path / "absent.toml")
    assert not ceilings.available and ceilings.get("max_handoff_tokens") is None and ceilings.as_dict() == {}


def test_control_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CIV_CONTROL_DIR", str(tmp_path / "ctl"))
    assert control_dir() == tmp_path / "ctl"
    monkeypatch.delenv("CIV_CONTROL_DIR")
    monkeypatch.setenv("CIV_ROOT", str(tmp_path))
    assert control_dir() == tmp_path / "control"


# --- civ_tune against the ceiling -----------------------------------------------------------------


def _round(op, n: int = 3) -> int:
    for i in range(n):
        op.log_event("sol-001", "task.claimed", {"task_id": f"T-{i:03d}"})
    rid = op.open_round(1)
    op.round_brief(rid)
    return rid


def test_tune_refuses_a_value_above_the_ceiling(db, knobs, shared_dir, make_agent, op, tmp_path):
    sup = make_agent("supervisor")[0]
    civ = CivTools(db, knobs, shared_dir, Ceilings(write_ceilings(tmp_path / "ceilings.toml", 4400)))
    _round(op)
    # One step up (4000 -> 4500) is within the knob's own bounds, but above the operator's ceiling.
    refused = civ.tune(sup, "max_handoff_tokens", 4500, "handoff_success_rate", "longer handoffs")
    assert refused["error"] == "above the ceiling in control/ceilings.toml (4400)"
    assert db.fetch_one("select value from config where knob = 'max_handoff_tokens'")["value"] == 4000.0


def test_tune_at_or_below_the_ceiling_passes_and_no_file_means_no_check(db, knobs, shared_dir, make_agent, op, tmp_path):
    sup = make_agent("supervisor")[0]
    with_ceiling = CivTools(db, knobs, shared_dir, Ceilings(write_ceilings(tmp_path / "ceilings.toml", 4500)))
    _round(op)
    # blocked_failed_rate has n = 3 claims in the window; handoff_success_rate would have n = 0.
    assert with_ceiling.tune(sup, "max_handoff_tokens", 4500, "blocked_failed_rate", "at the ceiling")["ok"]
    op.close_round(op.current_round()["id"], {})
    # Without a ceilings file the check is skipped: the knob's own bounds still apply.
    without = CivTools(db, knobs, shared_dir, Ceilings(tmp_path / "absent.toml"))
    db.execute("update config set set_by = null, set_at = now() - interval '1 hour' where knob = 'max_handoff_tokens'")
    for i in range(3):
        op.log_event("sol-002", "task.claimed", {"task_id": f"T-1{i:02d}"})
    rid = op.open_round(2)
    op.round_brief(rid)
    assert without.tune(sup, "max_handoff_tokens", 5000, "blocked_failed_rate", "no ceiling file")["ok"]


# --- the TTL default from the shape -------------------------------------------------------------------


def test_tune_ttl_defaults_to_the_shape_when_not_given(db, knobs, shared_dir, make_agent, op, tmp_path):
    sup = make_agent("supervisor")[0]
    civ = CivTools(db, knobs, shared_dir, Ceilings(tmp_path / "absent.toml"))
    op.control_set("shape", {"governance": {"ttl_default_s": 900}})
    _round(op)
    out = civ.tune(sup, "top_k", 15, "blocked_failed_rate", "shape ttl")
    assert out["ok"]
    assert db.fetch_one("select ttl_s from config where knob = 'top_k'")["ttl_s"] == 900


def test_tune_ttl_falls_back_to_the_default_without_a_shape(db, knobs, shared_dir, make_agent, op, tmp_path):
    sup = make_agent("supervisor")[0]
    civ = CivTools(db, knobs, shared_dir, Ceilings(tmp_path / "absent.toml"))
    _round(op)
    assert civ.tune(sup, "top_k", 15, "blocked_failed_rate", "default ttl")["ok"]
    assert db.fetch_one("select ttl_s from config where knob = 'top_k'")["ttl_s"] == DEFAULT_TTL_S
    # An explicit ttl still wins over both.
    op.close_round(op.current_round()["id"], {})
    db.execute("update config set set_by = null, set_at = now() - interval '1 hour' where knob = 'top_k'")
    for i in range(3):
        op.log_event("sol-003", "task.claimed", {"task_id": f"T-2{i:02d}"})
    rid = op.open_round(2)
    op.round_brief(rid)
    assert civ.tune(sup, "top_k", 20, "blocked_failed_rate", "explicit", ttl_s=1800)["ok"]
    assert db.fetch_one("select ttl_s from config where knob = 'top_k'")["ttl_s"] == 1800
