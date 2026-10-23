"""The shape: civ.toml parsing, knob seeds, the control_state round trip, and the reader's cache."""

from __future__ import annotations

import pytest

from harness.orchestrator.shape import KNOB_KEYS, Shape, ShapeReader, load_shape
from harness.tests.control_fixtures import write_control_dir
from harness.tests.orchestrator_fakes import FakeOperator


def test_load_shape_reads_the_contract_file(tmp_path):
    control = write_control_dir(tmp_path)
    shape = load_shape(control / "civ.toml")
    assert shape == Shape(solvers=3, supervisors=1, supervisor_round_s=600.0, max_generations=5, handoff_pct=80.0,
                          kill_pct=90.0, kill_grace_turns=2, decay_offset_h=2.0, decay_power=1.5, operator_pin_s=300,
                          vote_quota=1, min_sample=3, big_move_steps=2, ttl_default_s=3600)


def test_knob_seeds_are_exactly_the_knob_valued_keys():
    seeds = Shape().knob_seeds()
    assert set(seeds) == set(KNOB_KEYS) == {"handoff_pct", "kill_pct", "kill_grace_turns", "decay_offset_h",
                                             "decay_power", "vote_quota", "min_sample", "big_move_steps"}
    # Shape-only keys never reach the config table: the orchestrator and the CLI read them directly.
    assert "solvers" not in seeds and "supervisor_round_s" not in seeds and "operator_pin_s" not in seeds
    assert all(isinstance(v, float) for v in seeds.values())


def test_to_json_round_trips_through_from_mapping():
    shape = Shape(solvers=7, supervisor_round_s=15.0, min_sample=10)
    data = shape.to_json()
    assert data["population"] == {"solvers": 7, "supervisors": 1}
    assert data["cadence"] == {"supervisor_round_s": 15.0, "heartbeat_s": 30}
    # control_state.shape carries written_at next to the sections; it must not upset the parse.
    assert Shape.from_mapping({**data, "written_at": "2026-10-17T00:00:00Z"}) == shape


def test_unknown_keys_are_ignored_and_wrong_types_refused(tmp_path, caplog):
    (tmp_path / "civ.toml").write_text("[population]\nsolvers = 2\ncolour = 'red'\n[weather]\nrain = true\n")
    with caplog.at_level("WARNING"):
        shape = load_shape(tmp_path / "civ.toml")
    assert shape.solvers == 2 and shape.supervisors == 1
    assert "population.colour" in caplog.text and "[weather]" in caplog.text
    (tmp_path / "bad.toml").write_text("[population]\nsolvers = 'three'\n")
    with pytest.raises(ValueError):
        load_shape(tmp_path / "bad.toml")


def test_changes_from_lists_each_differing_field():
    before, after = Shape(), Shape(solvers=5, supervisor_round_s=15.0)
    assert after.changes_from(before) == {"solvers": (3, 5), "supervisor_round_s": (600.0, 15.0)}
    assert before.changes_from(before) == {}


def test_reader_caches_for_poll_s_and_then_sees_a_reshape():
    op = FakeOperator()
    now = {"t": 0.0}
    reader = ShapeReader(op, Shape(), poll_s=5.0, clock=lambda: now["t"])
    # Nothing in control_state yet: the initial shape, and the epoch the operator reports.
    assert reader.current().solvers == 3 and reader.epoch() == 1
    # A reshape lands from another process (the CLI). Inside poll_s the reader still says 3.
    op.control["shape"] = {**Shape(solvers=5).to_json(), "written_at": "now"}
    op.control["epoch"] = 2
    assert reader.current().solvers == 3
    now["t"] = 6.0
    assert reader.current().solvers == 5 and reader.epoch() == 2
    # invalidate() forces the next read, which the population uses after it has acted.
    op.control["shape"] = {**Shape(solvers=4).to_json()}
    reader.invalidate()
    assert reader.current().solvers == 4


def test_reader_keeps_the_last_shape_when_the_read_fails():
    class Broken(FakeOperator):
        def control_get(self, key):
            raise RuntimeError("db down")

    reader = ShapeReader(Broken(), Shape(solvers=2), poll_s=0.0)
    assert reader.current().solvers == 2


def test_merge_section_is_carried_through_the_shape(tmp_path):
    # The server's merge rules read [merge] from control_state.shape, so the orchestrator must not
    # drop it on the way there (the first live merge run warned "unknown section [merge] ignored").
    from harness.orchestrator.shape import Shape, load_shape

    path = tmp_path / "civ.toml"
    path.write_text('[population]\nsolvers = 2\nsupervisors = 1\n[merge]\nmax_per_round = 1\nprotected_paths = ["control/"]\n')
    shape = load_shape(path)
    assert shape.extra == {"merge": {"max_per_round": 1, "protected_paths": ["control/"]}}
    as_json = shape.to_json()
    assert as_json["merge"]["max_per_round"] == 1 and as_json["population"]["solvers"] == 2
    assert Shape.from_mapping(as_json) == shape
