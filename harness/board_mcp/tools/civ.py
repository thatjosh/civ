"""The civ tools: handoff, alert, status, and the supervisor-only tune and report.

handoff carries its full validation and ends the agent itself (M3). tune and report carry the
round rules (M4): a tune happens inside an open supervisor round, once per round, on a sample the
round's brief can vouch for, and a big move needs two supervisors; a report happens once per
supervisor per round, and a report that pings the operator names a trigger the server can find in
the data. merge (M6) queues a proposal's diff for the orchestrator's merge gate, never running
git or the tests itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..auth import OPERATOR_ID, AgentIdentity
from ..ceilings import Ceilings
from ..db import Db, fetch_one
from ..limits import Knobs, posts_in_last_hour
from ..merge_rules import load_merge_section, outside_harness, paths_in_diff, protected
from ..seed import CONFIG_SEEDS
from ..slo import HIGHER_IS_BETTER, SLO_NAMES, compute_slos, status_snapshot
from .common import insert_message, log_event, pin_block, refuse

HANDOFF_HEADINGS = ("State of work", "Open threads", "What to try next", "What not to retry", "Files worth reading")
SLOS = SLO_NAMES
TRIGGERS = ("succession_failing", "slo_degraded", "big_move_disputed", "ceiling_pinned")
NO_ROUND = "no supervisor round is open"
PING_PREFIX = "[ping: "
# A merges row in one of these states means the proposal is spoken for; a failed or rejected one may be queued again.
MERGE_LIVE_STATUSES = ("merged", "queued", "running", "queued_for_operator")
DIFF_SUFFIXES = (".diff", ".patch")
# How many stored briefs "degraded for three windows" needs, and how old a pending big move has to
# be before a disagreement over it counts as a dispute.
DEGRADED_WINDOWS = 3
DISPUTE_AGE_ROUNDS = 2


# Knobs with a hard ceiling in control/ceilings.toml: the tune check compares each against its ceiling.
CEILINGED_KNOBS = {"max_handoff_tokens": "max_handoff_tokens"}
DEFAULT_TTL_S = 3600


class CivTools:
    def __init__(self, db: Db, knobs: Knobs, shared_dir: Path, ceilings: Ceilings | None = None):
        self.db = db
        self.knobs = knobs
        self.shared_dir = shared_dir
        # Operator-owned ceilings (M5). Without a file every ceiling reads None and the check is skipped.
        self.ceilings = ceilings or Ceilings()

    def handoff(self, identity: AgentIdentity, body: str) -> dict[str, Any]:
        missing = [h for h in HANDOFF_HEADINGS if not re.search(rf"^##\s+{re.escape(h)}\s*$", body or "", re.I | re.M)]
        if missing:
            return refuse("handoff is missing headings: " + ", ".join(f"## {h}" for h in missing))
        max_tokens = int(self.knobs.get("max_handoff_tokens"))
        approx_tokens = len(body) / 4
        if approx_tokens > max_tokens:
            return refuse(f"handoff is about {int(approx_tokens)} tokens; the ceiling is {max_tokens}")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity, during_handoff=True)
            if blocked:
                return blocked
            status = fetch_one(conn, "select status from agents where id = %s", (identity.id,))
            if status is None or status["status"] != "handing_off":
                return refuse("handoff not requested; the orchestrator sets your status to handing_off first")
            row = fetch_one(
                conn,
                "insert into handoffs (lineage_id, from_agent, generation, body) values (%s, %s, %s, %s) returning id",
                (identity.lineage_id, identity.id, identity.generation, body),
            )
            insert_message(conn, identity, "handoff", body, thread_id=None, parent_id=None, refs=None,
                           shared_dir=self.shared_dir, knobs=self.knobs)
            # The handoff is the agent's last act: ending it here (rather than waiting for the
            # orchestrator) means its token stops working at once, so a confused agent cannot keep
            # writing after it has handed over, and the lease stays put for the successor to inherit.
            conn.execute("update agents set status = 'ended', ended_at = now() where id = %s", (identity.id,))
            log_event(conn, identity.id, "tool.civ_handoff", {"handoff_id": int(row["id"])})
            log_event(conn, identity.id, "agent.handoff_written",
                      {"handoff_id": int(row["id"]), "lineage_id": identity.lineage_id, "generation": identity.generation})
            return {"ok": True, "handoff_id": int(row["id"]), "stop": True}

    def alert(self, identity: AgentIdentity, body: str) -> dict[str, Any]:
        if not body or not body.strip():
            return refuse("alert body is empty")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            # An alert is a post by another name, so it draws on the same hourly allowance; otherwise
            # the one kind that reaches the supervisors would be the one kind an agent could flood.
            limit = int(self.knobs.get("post_rate_per_h", conn))
            if posts_in_last_hour(conn, identity.id) >= limit:
                return refuse(f"post rate limit of {limit} per hour reached; alerts count toward it")
            inserted = insert_message(conn, identity, "alert", body.strip(), thread_id=None, parent_id=None,
                                      refs=None, shared_dir=self.shared_dir, knobs=self.knobs)
            log_event(conn, identity.id, "tool.civ_alert", {"id": inserted["id"]})
            return {"ok": True, "id": inserted["id"]}

    def status(self, identity: AgentIdentity) -> dict[str, Any]:
        with self.db.transaction() as conn:
            snapshot = status_snapshot(conn)
            me = fetch_one(conn, "select generation, context_pct from agents where id = %s", (identity.id,))
            lease = fetch_one(conn, "select id from tasks where lease_owner = %s and status = 'leased'", (identity.id,))
            last_claim = fetch_one(
                conn, "select max(ts) as ts from events where agent_id = %s and type = 'task.claimed'", (identity.id,)
            )
            if last_claim and last_claim["ts"]:
                cast = fetch_one(conn, "select count(*) as n from votes where voter_id = %s and created_at > %s",
                                 (identity.id, last_claim["ts"]))
            else:
                cast = fetch_one(conn, "select count(*) as n from votes where voter_id = %s", (identity.id,))
            log_event(conn, identity.id, "tool.civ_status", {})
            return {
                "ok": True,
                **snapshot,
                "you": {
                    "id": identity.id,
                    "generation": int(me["generation"]) if me else identity.generation,
                    "context_pct": float(me["context_pct"]) if me else 0.0,
                    "lease": lease["id"] if lease else None,
                    "votes_since_claim": int(cast["n"]),
                },
            }

    def tune(self, identity: AgentIdentity, knob: str, value: float, slo: str, reason: str,
             ttl_s: int | None = None) -> dict[str, Any]:
        if not identity.is_supervisor:
            return refuse("supervisor only")
        if slo not in SLOS:
            return refuse("slo must be one of " + ", ".join(SLOS))
        if not reason or not reason.strip():
            return refuse("a tune needs a reason")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            rnd = self._open_round(conn)
            if rnd is None:
                return refuse(NO_ROUND)
            if rnd["tuned_knob"] is not None:
                # One knob change per round across all supervisors, so that a window's numbers can
                # be attributed to one change.
                return refuse(f"round already tuned ({rnd['tuned_knob']} by {rnd['tuned_by']})")
            row = fetch_one(conn, "select * from config where knob = %s for update", (knob,))
            if row is None:
                return refuse(f"unknown knob {knob}")
            value = float(value)
            if value < row["min"] or value > row["max"]:
                return refuse(f"{knob} must stay within [{row['min']}, {row['max']}]")
            ceiling = self.ceilings.get(CEILINGED_KNOBS[knob]) if knob in CEILINGED_KNOBS else None
            if ceiling is not None and value > float(ceiling):
                # The knob's own max is a seed the harness could change; the ceiling is the
                # operator's and lives outside the agents' reach, so it is checked separately.
                return refuse(f"above the ceiling in control/ceilings.toml ({ceiling:g})")
            if abs(value - float(row["value"])) > float(row["step"]) + 1e-9:
                return refuse(f"{knob} moves at most one step ({row['step']}) per tune; it is {row['value']}")
            if row["set_by"] is not None and row["set_by"] != OPERATOR_ID:
                # The cooldown damps supervisors oscillating a knob. An operator preset is launch
                # configuration (or an injected fault), and must stay tunable at once.
                cooling = fetch_one(conn, "select (%s + make_interval(secs => %s)) > now() as hot",
                                    (row["set_at"], int(row["cooldown_s"])))
                if cooling["hot"]:
                    return refuse(f"{knob} is inside its {row['cooldown_s']}s cooldown")
            n = self._sample_for(conn, rnd, slo)
            min_sample = int(self.knobs.get("min_sample", conn))
            if n < min_sample:
                # Observe before act: a window with too few observations is not evidence.
                return refuse(f"below min_sample: {slo} has n={n} in this window, needs {min_sample}")
            seed = float(CONFIG_SEEDS[knob][0]) if knob in CONFIG_SEEDS else float(row["value"])
            big_move = abs(value - seed) > float(self.knobs.get("big_move_steps", conn)) * float(row["step"]) + 1e-9
            if big_move:
                pending = self._matching_proposal(conn, rnd, identity, knob, value)
                if pending is None:
                    # First half of a two-supervisor commit: record it, change nothing, and leave
                    # the round's one tune unused so the second supervisor can still commit it.
                    # Repeating oneself records nothing new: one supervisor is one opinion.
                    mine = fetch_one(conn, "select 1 from tune_proposals where knob = %s and abs(value - %s) < 1e-9 "
                                           "and proposed_by = %s and committed_by is null", (knob, value, identity.id))
                    if mine is None:
                        fetch_one(conn, "insert into tune_proposals (knob, value, proposed_by, round_id) values (%s, %s, %s, %s) returning id",
                                  (knob, value, identity.id, rnd["id"]))
                        log_event(conn, identity.id, "tool.civ_tune_proposed",
                                  {"knob": knob, "value": value, "slo": slo, "reason": reason.strip(), "round_id": rnd["id"]})
                    return {"ok": True, "pending": True, "knob": knob, "value": value, "needs": "a second supervisor"}
                conn.execute("update tune_proposals set committed_by = %s, committed_at = now() where id = %s",
                             (identity.id, pending["id"]))
            if ttl_s is None:
                # The shape file sets the default TTL (M5); without one the M1 default stands.
                ttl_s = self._shape_value(conn, "governance", "ttl_default_s", DEFAULT_TTL_S)
            updated = fetch_one(
                conn,
                """
                update config set value = %s, set_by = %s, reason = %s, set_at = now(), ttl_s = %s,
                       expires_at = now() + make_interval(secs => %s)
                where knob = %s returning expires_at
                """,
                (value, identity.id, reason.strip(), int(ttl_s), int(ttl_s), knob),
            )
            conn.execute("update rounds set tuned_knob = %s, tuned_by = %s where id = %s", (knob, identity.id, rnd["id"]))
            log_event(conn, identity.id, "tool.civ_tune",
                      {"knob": knob, "from": float(row["value"]), "to": value, "slo": slo, "n": n, "reason": reason.strip(),
                       "big_move": big_move, "round_id": rnd["id"]})
            self.knobs.invalidate()
            return {"ok": True, "knob": knob, "value": value, "expires_at": updated["expires_at"].isoformat()}

    # --- civ_merge (M6) ------------------------------------------------------------------------------

    def merge(self, identity: AgentIdentity, proposal_id: int) -> dict[str, Any]:
        """Queue a proposal's diff for the merge gate. The server only decides where it queues: the
        orchestrator's merger applies the diff in a worktree and runs the tests, because a tool call
        has to return in milliseconds and the repository belongs to the process that owns it."""
        if not identity.is_supervisor:
            return refuse("supervisor only")
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            rnd = self._open_round(conn)
            if rnd is None:
                return refuse(NO_ROUND)
            proposal = fetch_one(conn, "select id, author_id, kind, refs from messages where id = %s", (int(proposal_id),))
            if proposal is None or proposal["kind"] != "proposal":
                return refuse(f"message {int(proposal_id)} is not a proposal")
            ref = next((r for r in (proposal["refs"] or []) if r.endswith(DIFF_SUFFIXES)), None)
            if ref is None:
                return refuse("proposal has no .diff ref")
            text = self._read_shared(ref)
            if text is None:
                return refuse(f"diff file missing: {ref}")
            paths = paths_in_diff(text)
            if not paths:
                return refuse("diff names no files")
            outside = outside_harness(paths)
            if outside:
                return refuse("a proposal may only change files under harness/: " + ", ".join(outside))
            existing = fetch_one(conn, "select status from merges where proposal_id = %s and status = any(%s) order by id desc limit 1",
                                 (proposal["id"], list(MERGE_LIVE_STATUSES)))
            if existing is not None:
                return refuse("already merged" if existing["status"] == "merged" else "already queued")
            rules = load_merge_section(self._shape_json(conn))
            used = fetch_one(conn, "select count(*) as n from merges where round_id = %s and status not in ('rejected', 'failed')",
                             (rnd["id"],))
            if int(used["n"]) >= rules["max_per_round"]:
                return refuse(f"round merge cap reached ({rules['max_per_round']})")
            held = protected(paths, rules["protected_paths"])
            status = "queued_for_operator" if held else "queued"
            detail = ("protected paths: " + ", ".join(held)) if held else None
            row = fetch_one(
                conn,
                """
                insert into merges (proposal_id, requested_by, round_id, diff_ref, paths, status, detail)
                values (%s, %s, %s, %s, %s, %s, %s) returning id
                """,
                (proposal["id"], identity.id, rnd["id"], ref, paths, status, detail),
            )
            merge_id = int(row["id"])
            payload = {"merge_id": merge_id, "proposal_id": int(proposal["id"]), "paths": paths, "round_id": rnd["id"]}
            if held:
                # The operator reads the inbox, not the merges table, so the wait is announced there.
                insert_message(conn, identity, "alert",
                               f"[merge queued for operator] proposal #{int(proposal['id'])} touches {', '.join(held)}",
                               thread_id=None, parent_id=None, refs=None, shared_dir=self.shared_dir, knobs=self.knobs)
                log_event(conn, identity.id, "merge.queued_for_operator", {**payload, "protected": held})
                return {"ok": True, "merge_id": merge_id, "status": status, "paths": paths}
            log_event(conn, identity.id, "merge.queued", payload)
            return {"ok": True, "merge_id": merge_id, "status": status}

    def _read_shared(self, ref: str) -> str | None:
        """The text behind a shared/<name> ref, or None when the file is gone."""
        name = ref.split("/", 1)[1] if ref.startswith("shared/") else ref
        path = self.shared_dir / name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _shape_json(conn) -> dict[str, Any] | None:
        """The whole shape from control_state (civ.toml as JSON), or None; the existence check comes
        first because a missing table would abort the surrounding transaction."""
        exists = fetch_one(conn, "select to_regclass('control_state') is not null as present")
        if not exists or not exists["present"]:
            return None
        row = fetch_one(conn, "select value from control_state where key = 'shape'")
        return row["value"] if row is not None and isinstance(row["value"], dict) else None

    @staticmethod
    def _shape_value(conn, section: str, key: str, default: Any) -> Any:
        """A value from the shape the operator wrote to control_state (civ.toml as JSON), or the
        default when the table, the row or the key is absent. The existence check comes first
        because a missing table would abort the surrounding transaction."""
        exists = fetch_one(conn, "select to_regclass('control_state') is not null as present")
        if not exists or not exists["present"]:
            return default
        row = fetch_one(conn, "select value from control_state where key = 'shape'")
        if row is None or not isinstance(row["value"], dict):
            return default
        found = (row["value"].get(section) or {}).get(key) if isinstance(row["value"].get(section), dict) else None
        return found if found is not None else default

    @staticmethod
    def _open_round(conn) -> dict[str, Any] | None:
        row = fetch_one(conn, "select id, number, started_at, window_start, tuned_knob, tuned_by, brief from rounds "
                              "where ended_at is null order by id desc limit 1 for update")
        return dict(row) if row else None

    @staticmethod
    def _sample_for(conn, rnd: dict[str, Any], slo: str) -> int:
        """The window sample behind an SLO, read from the brief the round's supervisors were shown.
        A round opened without a brief (tests, a crashed orchestrator) is measured live instead."""
        brief = rnd.get("brief") or {}
        if slo == "handoff_success_rate" and brief.get("succession"):
            succession = brief["succession"]
            return int(succession.get("handoffs", 0)) + int(succession.get("stubs", 0))
        current = (brief.get("slo") or {}).get("current")
        if current and slo in current:
            return int(current[slo].get("n", 0))
        live = compute_slos(conn, rnd["window_start"], fetch_one(conn, "select now() as now")["now"])
        return int(live[slo]["n"])

    @staticmethod
    def _matching_proposal(conn, rnd: dict[str, Any], identity: AgentIdentity, knob: str, value: float) -> dict[str, Any] | None:
        """A pending proposal for the same knob and value from a different supervisor, made in this
        round or the one before. Same supervisor twice is one opinion, not two."""
        return fetch_one(
            conn,
            """
            select p.id from tune_proposals p join rounds r on r.id = p.round_id
            where p.knob = %s and abs(p.value - %s) < 1e-9 and p.committed_by is null
              and p.proposed_by <> %s and r.number >= %s - 1 and r.number <= %s
            order by p.id limit 1
            """,
            (knob, value, identity.id, rnd["number"], rnd["number"]),
        )

    def report(self, identity: AgentIdentity, body: str, trigger: str | None = None) -> dict[str, Any]:
        if not identity.is_supervisor:
            return refuse("supervisor only")
        if not body or not body.strip():
            return refuse("report body is empty")
        if trigger is not None and trigger not in TRIGGERS:
            return refuse("trigger must be one of " + ", ".join(TRIGGERS))
        with self.db.transaction() as conn:
            blocked = pin_block(conn, identity)
            if blocked:
                return blocked
            rnd = self._open_round(conn)
            if rnd is None:
                return refuse(NO_ROUND)
            already = fetch_one(conn, "select 1 from messages where kind = 'report' and author_id = %s and created_at >= %s",
                                (identity.id, rnd["started_at"]))
            if already:
                return refuse("you already reported this round")
            stored = body.strip()
            if trigger is not None:
                problem = self._verify_trigger(conn, rnd, trigger, stored)
                if problem:
                    return refuse(problem)
                stored = f"{PING_PREFIX}{trigger}] {stored}"
            inserted = insert_message(conn, identity, "report", stored, thread_id=None, parent_id=None,
                                      refs=None, shared_dir=self.shared_dir, knobs=self.knobs, spill=False)
            log_event(conn, identity.id, "tool.civ_report", {"id": inserted["id"], "round_id": rnd["id"], "trigger": trigger})
            if trigger is not None:
                log_event(conn, identity.id, "report.ping", {"id": inserted["id"], "round_id": rnd["id"], "trigger": trigger})
            return {"ok": True, "id": inserted["id"]}

    def _verify_trigger(self, conn, rnd: dict[str, Any], trigger: str, body: str) -> str | None:
        """The refusal text when the data does not support the trigger, else None. Ping inflation is
        how an inbox dies, so a ping has to point at something the server can see."""
        if trigger == "ceiling_pinned":
            return "no ceilings until M5"
        if trigger == "succession_failing":
            kills_now = ((rnd.get("brief") or {}).get("succession") or {}).get("kills", 0)
            previous = fetch_one(conn, "select brief from rounds where id < %s order by id desc limit 1", (rnd["id"],))
            kills_before = ((previous["brief"] if previous else {}).get("succession") or {}).get("kills", 0)
            if kills_now >= 1 and kills_before >= 1:
                return None
            return f"trigger succession_failing is not in the data: kills this window = {kills_now}, previous window = {kills_before}"
        if trigger == "slo_degraded":
            match = re.search(r"slo=([a-z_]+)", body)
            if not match or match.group(1) not in SLOS:
                return "trigger slo_degraded is not in the data: name the SLO as slo=<name> in the body"
            name = match.group(1)
            rows = fetch_one(conn, "select brief from rounds where id = %s", (rnd["id"],))
            briefs = [rows["brief"]] if rows and rows["brief"] else []
            briefs += [r["brief"] for r in conn.execute(
                "select brief from rounds where id < %s and brief <> '{}'::jsonb order by id desc limit %s",
                (rnd["id"], DEGRADED_WINDOWS - 1)).fetchall()]
            values = [((b.get("slo") or {}).get("current") or {}).get(name, {}).get("value") for b in briefs]
            values.reverse()  # oldest first
            if len(values) < DEGRADED_WINDOWS or any(v is None for v in values):
                return (f"trigger slo_degraded is not in the data: {name} needs {DEGRADED_WINDOWS} windows of numbers, "
                        f"have {[v for v in values]}")
            worse = (lambda a, b: b < a) if HIGHER_IS_BETTER[name] else (lambda a, b: b > a)
            if all(worse(values[i], values[i + 1]) for i in range(len(values) - 1)):
                return None
            return f"trigger slo_degraded is not in the data: {name} over the last {DEGRADED_WINDOWS} windows was {values}"
        if trigger == "big_move_disputed":
            disputed = fetch_one(
                conn,
                """
                select p.knob, p.value from tune_proposals p join rounds r on r.id = p.round_id
                where p.committed_by is null and r.number <= %s - %s
                  and exists (select 1 from tune_proposals q where q.knob = p.knob and q.committed_by is null
                              and abs(q.value - p.value) > 1e-9)
                order by p.id limit 1
                """,
                (rnd["number"], DISPUTE_AGE_ROUNDS),
            )
            if disputed:
                return None
            return (f"trigger big_move_disputed is not in the data: no pending proposal at least {DISPUTE_AGE_ROUNDS} "
                    "rounds old with a conflicting proposal on the same knob")
        return f"trigger {trigger} is not in the data"
