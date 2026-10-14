"""Scoring: the one formula the whole board ranks by.

score = sum(value * weight) / (age_hours + decay_offset_h) ^ decay_power

The SQL fragment and the Python function must agree; a test checks them against each other. The
fragment is parameterised by the knob values so that a supervisor tuning decay changes ordering
without a redeploy.
"""

from __future__ import annotations

# Score of the message aliased as m. Expects %(decay_offset)s and %(decay_power)s parameters.
SCORE_SQL = (
    "coalesce((select sum(v.value * v.weight) from votes v where v.message_id = m.id), 0) "
    "/ power(extract(epoch from (now() - m.created_at)) / 3600.0 + %(decay_offset)s, %(decay_power)s)"
)

# Same formula for a task's thread root, with the task aliased as t. Used by task_claim ordering.
THREAD_SCORE_SQL = (
    "coalesce((select sum(v.value * v.weight) from votes v where v.message_id = t.thread_id), 0) "
    "/ power(extract(epoch from (now() - (select created_at from messages where id = t.thread_id))) / 3600.0"
    " + %(decay_offset)s, %(decay_power)s)"
)


def score(weighted_sum: float, age_hours: float, decay_offset_h: float, decay_power: float) -> float:
    return weighted_sum / ((age_hours + decay_offset_h) ** decay_power)
