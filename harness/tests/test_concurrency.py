"""The single-writer rule under load: a hundred simultaneous claims never share a task."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from harness.board_mcp.limits import Knobs
from harness.board_mcp.tools.task import TaskTools


def _claim_all(db, shared_dir, identities):
    tools = TaskTools(db, shared_dir, Knobs(db))
    gate = threading.Barrier(len(identities))

    def go(identity):
        gate.wait()  # release every thread at once so the claims really overlap
        return tools.claim(identity)

    with ThreadPoolExecutor(max_workers=len(identities)) as pool:
        return list(pool.map(go, identities))


def test_hundred_claims_over_hundred_tasks(db, op, shared_dir, make_agent):
    for i in range(100):
        op.create_task(f"T-{i:03d}", f"task {i}", "work")
    agents = [make_agent("solver")[0] for _ in range(100)]
    results = _claim_all(db, shared_dir, agents)
    leased = [r["task"]["id"] for r in results if r.get("task")]
    assert len(leased) == 100 and len(set(leased)) == 100
    owners = db.fetch_all("select lease_owner, count(*) as n from tasks where status = 'leased' group by lease_owner")
    assert all(int(o["n"]) == 1 for o in owners) and len(owners) == 100


def test_hundred_claims_over_forty_tasks(db, op, shared_dir, make_agent):
    for i in range(40):
        op.create_task(f"T-{i:03d}", f"task {i}", "work")
    agents = [make_agent("solver")[0] for _ in range(100)]
    results = _claim_all(db, shared_dir, agents)
    outcomes = Counter("leased" if r.get("task") else r.get("reason") for r in results)
    assert outcomes == {"leased": 40, "no open tasks": 60}
    assert len({r["task"]["id"] for r in results if r.get("task")}) == 40
