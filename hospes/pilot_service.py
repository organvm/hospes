"""Revisioned persistence and human decisions for context-derived Pilot plans."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
import yaml

from . import generation, partnerships, store
from .pilot_models import PilotDecisionInput, PilotError, PilotPolicyInput, aware_datetime
from .pilot_planner import (
    ACTIVATION_APPROVED,
    AVAILABLE,
    AWAITING_REPLY,
    BOOKED,
    FOLLOW_UP_APPROVED,
    FOLLOW_UP_DUE,
    INFEASIBLE,
    OPTED_OUT,
    PAUSED,
    PROMOTION_DUE,
    REPLIED,
    REVISIT_LATER,
    build_projection,
)


ACTIVE_ASSIGNMENT_STATES = frozenset({
    ACTIVATION_APPROVED,
    AVAILABLE,
    AWAITING_REPLY,
    FOLLOW_UP_APPROVED,
    FOLLOW_UP_DUE,
    PROMOTION_DUE,
    REPLIED,
})
DECISION_ROLES = frozenset({"producer", "editorial_owner", "relationship_owner"})


def _now() -> datetime:
    return generation.now()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _id(purpose: str) -> str:
    return generation.new_id(purpose)


def _require_role(actor_role: str, allowed: Iterable[str], action: str) -> None:
    role_set = set(allowed)
    if actor_role not in role_set:
        raise PilotError(403, f"{action} requires one of these roles: {sorted(role_set)}")


def _partnership(conn: sqlite3.Connection, partnership_id: str, tenant_id: str) -> dict[str, Any]:
    record = store.fetch_one(
        conn,
        "SELECT * FROM partnerships WHERE id = ? AND tenant_id = ?",
        (partnership_id, tenant_id),
    )
    if record is None:
        raise PilotError(404, "partnership not found in this tenant")
    return record


def _policy(
    conn: sqlite3.Connection, policy_id: str, partnership_id: str, tenant_id: str
) -> dict[str, Any]:
    record = store.fetch_one(
        conn,
        "SELECT * FROM pilot_policies WHERE id = ? AND partnership_id = ? AND tenant_id = ?",
        (policy_id, partnership_id, tenant_id),
    )
    if record is None:
        raise PilotError(404, "pilot policy not found in this partnership")
    return record


def _run(
    conn: sqlite3.Connection, run_id: str, partnership_id: str, tenant_id: str
) -> dict[str, Any]:
    record = store.fetch_one(
        conn,
        "SELECT * FROM pilot_runs WHERE id = ? AND partnership_id = ? AND tenant_id = ?",
        (run_id, partnership_id, tenant_id),
    )
    if record is None:
        raise PilotError(404, "pilot run not found in this partnership")
    return record


def _assignments(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return store.fetch_all(
        conn,
        "SELECT * FROM pilot_candidate_assignments WHERE pilot_run_id = ? ORDER BY slot_order",
        (run_id,),
    )


def _audit(
    conn: sqlite3.Connection,
    *,
    partnership_id: str,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    event_type: str,
    details: Mapping[str, Any],
    timestamp: str,
) -> None:
    partnership = _partnership(conn, partnership_id, tenant_id)
    store.insert(conn, "partnership_audit_events", {
        "id": _id("partnership_audit_event"),
        "tenant_id": tenant_id,
        "show_id": partnership["show_id"],
        "partnership_id": partnership_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "details": dict(details),
        "created_at": timestamp,
    })


def import_policy(
    conn: sqlite3.Connection,
    partnership_id: str,
    path: str | Path,
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Import exactly one checksummed policy version, idempotently."""
    partnership = _partnership(conn, partnership_id, tenant_id)
    _require_role(actor_role, DECISION_ROLES, "pilot policy import")
    try:
        source = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PilotError(422, f"pilot policy is unreadable: {exc}") from exc
    if not isinstance(source, Mapping):
        raise PilotError(422, "pilot policy must be an object")
    declared_show = source.get("show_id")
    if declared_show is not None and declared_show != partnership["show_id"]:
        raise PilotError(403, "pilot policy belongs to another show")
    parsed = PilotPolicyInput.from_mapping(source)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM pilot_policies WHERE partnership_id = ? AND policy_digest = ?",
        (partnership_id, parsed.digest),
    )
    if existing is not None:
        return {**existing, "outcome": "unchanged"}
    conflicting = store.fetch_one(
        conn,
        "SELECT id FROM pilot_policies WHERE partnership_id = ? AND policy_key = ? "
        "AND policy_version = ?",
        (partnership_id, parsed.policy_key, parsed.policy_version),
    )
    if conflicting is not None:
        raise PilotError(409, "that policy key and version already has a different digest")
    timestamp = _iso(now or _now())
    record = {
        "id": _id("pilot_policy"),
        "tenant_id": tenant_id,
        "show_id": partnership["show_id"],
        "partnership_id": partnership_id,
        **parsed.canonical(),
        "timezone": parsed.timezone_name,
        "policy_digest": parsed.digest,
        "created_by": actor_id,
        "created_role": actor_role,
        "created_at": timestamp,
    }
    record.pop("timezone_name", None)
    store.insert(conn, "pilot_policies", record)
    _audit(
        conn,
        partnership_id=partnership_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
        event_type="pilot.policy_imported",
        details={
            "policy_id": record["id"],
            "policy_key": parsed.policy_key,
            "policy_version": parsed.policy_version,
            "policy_digest": parsed.digest,
        },
        timestamp=timestamp,
    )
    conn.commit()
    return {**store.fetch_one(conn, "SELECT * FROM pilot_policies WHERE id = ?", (record["id"],)), "outcome": "created"}


def _event(
    conn: sqlite3.Connection,
    *,
    run: Mapping[str, Any],
    assignment: Mapping[str, Any],
    revision: int,
    event_kind: str,
    resulting_state: str,
    actor_id: str,
    actor_role: str,
    timestamp: str,
    evidence_reference: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    store.insert(conn, "pilot_assignment_events", {
        "id": _id("pilot_assignment_event"),
        "tenant_id": run["tenant_id"],
        "show_id": run.get("show_id", "legacy"),
        "pilot_run_id": run["id"],
        "assignment_id": assignment["id"],
        "run_revision": revision,
        "event_kind": event_kind,
        "previous_state": assignment.get("active_state"),
        "resulting_state": resulting_state,
        "evidence_reference": evidence_reference,
        "details": dict(details or {}),
        "actor_id": actor_id,
        "actor_role": actor_role,
        "occurred_at": timestamp,
        "created_at": timestamp,
    })


def _write_projection(
    conn: sqlite3.Connection,
    run: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    projection = build_projection(policy, run, _assignments(conn, str(run["id"])), now=now)
    record = {
        "id": _id("pilot_plan_projection"),
        "tenant_id": run["tenant_id"],
        "show_id": run.get("show_id", "legacy"),
        "pilot_run_id": run["id"],
        "run_revision": run["revision"],
        **projection,
        "created_at": _iso(now),
    }
    store.insert(conn, "pilot_plan_projections", record)
    return record


def _plan_response(
    conn: sqlite3.Connection, run: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    projection = store.fetch_one(
        conn,
        "SELECT * FROM pilot_plan_projections WHERE pilot_run_id = ? AND run_revision = ?",
        (run["id"], run["revision"]),
    )
    if projection is None:
        projection = _write_projection(conn, run, policy, now=_now())
    return {
        **projection,
        "run_id": run["id"],
        "partnership_id": run["partnership_id"],
        "revision": run["revision"],
        "lifecycle_state": run["lifecycle_state"],
        "deadline_at": run["deadline_at"],
        "policy_id": policy["id"],
        "policy_digest": policy["policy_digest"],
        "assignments": _assignments(conn, str(run["id"])),
    }


def start_run(
    conn: sqlite3.Connection,
    partnership_id: str,
    payload: Mapping[str, Any],
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Start a run only from a reviewed, routable, policy-valid slate."""
    partnership = _partnership(conn, partnership_id, tenant_id)
    _require_role(actor_role, {"relationship_owner"}, "pilot run start")
    if not isinstance(payload, Mapping) or set(payload) != {"policy_id"}:
        raise PilotError(422, "pilot run start requires exactly policy_id")
    policy_id = payload.get("policy_id")
    if not isinstance(policy_id, str):
        raise PilotError(422, "policy_id must be an opaque internal id")
    policy = _policy(conn, policy_id, partnership_id, tenant_id)
    current = now or _now()
    if aware_datetime(policy["deadline_at"], "policy.deadline_at") <= current:
        raise PilotError(409, "pilot policy deadline has elapsed")
    open_run = store.fetch_one(
        conn,
        "SELECT id FROM pilot_runs WHERE partnership_id = ? "
        "AND lifecycle_state IN ('ACTIVE', 'PAUSED', 'REHEARSAL_FALLBACK') LIMIT 1",
        (partnership_id,),
    )
    if open_run is not None:
        raise PilotError(409, "this partnership already has an open pilot run")
    readiness = partnerships.command_center(conn, partnership_id, tenant_id)["pilot_readiness"]
    required = {check["key"]: bool(check["met"]) for check in readiness["checks"][:4]}
    if not all(required.values()):
        missing = sorted(key for key, met in required.items() if not met)
        raise PilotError(409, f"pilot run prerequisites are incomplete: {missing}")
    slots = store.fetch_all(
        conn,
        "SELECT s.slot, o.*, r.id AS route_id, r.usable AS route_usable, "
        "r.source_provenance AS route_provenance, r.verified_at AS route_verified_at "
        "FROM pilot_candidate_slots s "
        "JOIN appearance_opportunities o ON o.id = s.opportunity_id "
        "JOIN contact_routes r ON r.opportunity_id = o.id "
        "WHERE s.partnership_id = ? AND s.tenant_id = ? "
        "AND s.show_id = ? AND o.show_id = ? ORDER BY s.slot",
        (
            partnership_id,
            tenant_id,
            partnership["show_id"],
            partnership["show_id"],
        ),
    )
    if len(slots) != int(policy["candidate_count"]):
        raise PilotError(409, "candidate slate does not match the policy count")
    for slot in slots:
        if (
            slot["network_id"] != policy["candidate_network_id"]
            or slot["preferred_city"] != policy["candidate_city"]
            or slot["relationship_class"] not in set(policy["allowed_relationship_classes"])
            or slot["social_cost_1_5"] is None
            or int(slot["social_cost_1_5"]) > int(policy["max_social_cost"])
            or int(slot["social_cost_1_5"]) > int(policy["relationship_exposure_budget"])
            or not slot["relationship_owner"]
            or not slot["route_usable"]
            or not slot["route_provenance"]
            or not slot["route_verified_at"]
        ):
            raise PilotError(409, f"pilot slot {slot['slot']} violates the selected policy")
    timestamp = _iso(current)
    run = {
        "id": _id("pilot_run"),
        "tenant_id": tenant_id,
        "show_id": partnership["show_id"],
        "partnership_id": partnership_id,
        "policy_id": policy_id,
        "lifecycle_state": "ACTIVE",
        "revision": 1,
        "deadline_at": policy["deadline_at"],
        "created_by": actor_id,
        "created_role": actor_role,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    conn.execute("SAVEPOINT pilot_run_start")
    try:
        store.insert(conn, "pilot_runs", run)
        for slot in slots:
            assignment = {
                "id": _id("pilot_candidate_assignment"),
                "tenant_id": tenant_id,
                "show_id": partnership["show_id"],
                "pilot_run_id": run["id"],
                "opportunity_id": slot["id"],
                "slot_order": slot["slot"],
                "relationship_class": slot["relationship_class"],
                "social_cost": slot["social_cost_1_5"],
                "route_id": slot["route_id"],
                "owner_id": slot["relationship_owner"],
                "active_state": AVAILABLE,
                "not_before": None,
                "response_due_at": None,
                "follow_ups_sent": 0,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            store.insert(conn, "pilot_candidate_assignments", assignment)
            _event(
                conn,
                run=run,
                assignment={**assignment, "active_state": None},
                revision=1,
                event_kind="assignment.created",
                resulting_state=AVAILABLE,
                actor_id=actor_id,
                actor_role=actor_role,
                timestamp=timestamp,
            )
        _write_projection(conn, run, policy, now=current)
        _audit(
            conn,
            partnership_id=partnership_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_role=actor_role,
            event_type="pilot.run_started",
            details={"run_id": run["id"], "policy_id": policy_id, "revision": 1},
            timestamp=timestamp,
        )
        conn.execute("RELEASE SAVEPOINT pilot_run_start")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT pilot_run_start")
        conn.execute("RELEASE SAVEPOINT pilot_run_start")
        raise
    return _plan_response(conn, run, policy)


def _advance_expired_windows(
    conn: sqlite3.Connection,
    run: dict[str, Any],
    policy: Mapping[str, Any],
    *,
    actor_id: str,
    actor_role: str,
    now: datetime,
) -> dict[str, Any]:
    if run["lifecycle_state"] != "ACTIVE":
        return run
    expired = [
        assignment
        for assignment in _assignments(conn, str(run["id"]))
        if assignment["active_state"] == AWAITING_REPLY
        and assignment.get("response_due_at")
        and aware_datetime(assignment["response_due_at"], "response_due_at") <= now
    ]
    if not expired:
        return run
    revision = int(run["revision"]) + 1
    timestamp = _iso(now)
    for assignment in expired:
        state = (
            FOLLOW_UP_DUE
            if int(assignment["follow_ups_sent"]) < int(policy["follow_up_limit"])
            else PROMOTION_DUE
        )
        store.update(conn, "pilot_candidate_assignments", assignment["id"], {
            "active_state": state,
            "updated_at": timestamp,
        })
        conn.execute(
            "UPDATE appearance_opportunities SET status = 'FOLLOW_UP_DUE', updated_at = ? "
            "WHERE id = ? AND status = 'OUTREACH_SENT'",
            (timestamp, assignment["opportunity_id"]),
        )
        _event(
            conn,
            run=run,
            assignment=assignment,
            revision=revision,
            event_kind="response_window.elapsed",
            resulting_state=state,
            actor_id=actor_id,
            actor_role=actor_role,
            timestamp=timestamp,
            details={"response_due_at": assignment["response_due_at"]},
        )
    store.update(conn, "pilot_runs", run["id"], {
        "revision": revision,
        "updated_at": timestamp,
    })
    run = {**run, "revision": revision, "updated_at": timestamp}
    _write_projection(conn, run, policy, now=now)
    _audit(
        conn,
        partnership_id=run["partnership_id"],
        tenant_id=run["tenant_id"],
        actor_id=actor_id,
        actor_role=actor_role,
        event_type="pilot.response_window_elapsed",
        details={"run_id": run["id"], "revision": revision, "assignments": len(expired)},
        timestamp=timestamp,
    )
    conn.commit()
    return run


def get_plan(
    conn: sqlite3.Connection,
    partnership_id: str,
    run_id: str,
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_role(actor_role, DECISION_ROLES, "pilot plan access")
    run = _run(conn, run_id, partnership_id, tenant_id)
    policy = _policy(conn, run["policy_id"], partnership_id, tenant_id)
    run = _advance_expired_windows(
        conn,
        run,
        policy,
        actor_id=actor_id,
        actor_role=actor_role,
        now=now or _now(),
    )
    return _plan_response(conn, run, policy)


def _validate_activation(
    policy: Mapping[str, Any],
    assignments: list[dict[str, Any]],
    requested: list[dict[str, Any]],
    *,
    now: datetime,
) -> None:
    by_id = {assignment["id"]: assignment for assignment in assignments}
    chosen: list[tuple[dict[str, Any], datetime]] = []
    for item in requested:
        assignment = by_id.get(item["assignment_id"])
        if assignment is None or assignment["active_state"] != AVAILABLE:
            raise PilotError(409, "activate_set contains a non-available assignment")
        if (
            assignment["relationship_class"] not in set(policy["allowed_relationship_classes"])
            or int(assignment["social_cost"]) > int(policy["max_social_cost"])
            or int(assignment["social_cost"]) > int(policy["relationship_exposure_budget"])
        ):
            raise PilotError(409, "activate_set violates candidate eligibility or exposure")
        start = aware_datetime(item["not_before"], "not_before")
        if start < now - timedelta(minutes=1):
            raise PilotError(409, "activate_set not_before cannot be in the past")
        chosen.append((assignment, start))
    already_active = [
        assignment
        for assignment in assignments
        if assignment["active_state"] in {
            ACTIVATION_APPROVED,
            AWAITING_REPLY,
            FOLLOW_UP_DUE,
            FOLLOW_UP_APPROVED,
            PROMOTION_DUE,
        }
    ]
    for assignment, _start in chosen:
        if any(
            assignment["route_id"] == active["route_id"]
            or assignment["owner_id"] == active["owner_id"]
            for active in already_active
        ):
            raise PilotError(409, "multiple active asks require independent routes and owners")
    cycle = timedelta(
        hours=int(policy["initial_response_hours"])
        + (int(policy["follow_up_response_hours"]) if int(policy["follow_up_limit"]) else 0)
    )
    production = timedelta(hours=sum(int(value) for value in policy["production_gate_durations"].values()))
    latest = aware_datetime(policy["deadline_at"], "deadline_at") - production
    for assignment, start in chosen:
        if start + cycle > latest:
            raise PilotError(409, "activate_set cannot complete before production dependencies")
    for left_index, (left, left_start) in enumerate(chosen):
        for right, right_start in chosen[left_index + 1:]:
            overlap = max(left_start, right_start) < min(left_start + cycle, right_start + cycle)
            if overlap and (left["route_id"] == right["route_id"] or left["owner_id"] == right["owner_id"]):
                raise PilotError(409, "overlapping activations require independent routes and owners")


def record_decision(
    conn: sqlite3.Connection,
    partnership_id: str,
    run_id: str,
    decision: PilotDecisionInput,
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_role(actor_role, DECISION_ROLES, "pilot decision")
    current = now or _now()
    run = _run(conn, run_id, partnership_id, tenant_id)
    policy = _policy(conn, run["policy_id"], partnership_id, tenant_id)
    _require_role(
        actor_role,
        policy["human_authority_rules"]["decision_roles"],
        "pilot policy decision",
    )
    run = _advance_expired_windows(
        conn, run, policy, actor_id=actor_id, actor_role=actor_role, now=current
    )
    if int(run["revision"]) != decision.expected_revision:
        raise PilotError(409, "stale pilot run revision")
    if run["lifecycle_state"] not in {"ACTIVE", "PAUSED"}:
        raise PilotError(409, "pilot run no longer accepts this decision")
    assignments = _assignments(conn, run_id)
    by_id = {item["id"]: item for item in assignments}
    kind = decision.decision_kind
    assignment_states = {item["active_state"] for item in assignments}
    if (
        kind in {"activate_set", "follow_up", "promote"}
        and assignment_states & {REPLIED, BOOKED, OPTED_OUT, INFEASIBLE}
    ):
        raise PilotError(409, "reply, booking, opt-out, or infeasibility freezes this action")
    if kind in {"promote", "fallback_rehearsal", "pause"}:
        _require_role(actor_role, {"relationship_owner"}, kind)
    has_active_ask = any(
        item["active_state"] in {
            ACTIVATION_APPROVED,
            AWAITING_REPLY,
            FOLLOW_UP_DUE,
            FOLLOW_UP_APPROVED,
            PROMOTION_DUE,
        }
        for item in assignments
    )
    if kind == "activate_set" and (
        len(decision.payload["assignments"]) > 1 or has_active_ask
    ):
        _require_role(actor_role, {"relationship_owner"}, "multi-candidate activation")
    changes: list[dict[str, Any]] = []
    lifecycle = run["lifecycle_state"]
    timestamp = _iso(current)

    if kind == "activate_set":
        if lifecycle != "ACTIVE":
            raise PilotError(409, "a paused run cannot activate candidates")
        _validate_activation(policy, assignments, decision.payload["assignments"], now=current)
        for request in decision.payload["assignments"]:
            assignment = by_id[request["assignment_id"]]
            store.update(conn, "pilot_candidate_assignments", assignment["id"], {
                "active_state": ACTIVATION_APPROVED,
                "not_before": request["not_before"],
                "updated_at": timestamp,
            })
            changes.append({
                "assignment": assignment,
                "state": ACTIVATION_APPROVED,
                "event": "activation.approved",
                "details": {"not_before": request["not_before"]},
            })
    elif kind == "follow_up":
        assignment = by_id.get(decision.payload["assignment_id"])
        if assignment is None or assignment["active_state"] != FOLLOW_UP_DUE:
            raise PilotError(409, "follow_up requires an assignment in FOLLOW_UP_DUE")
        if int(assignment["follow_ups_sent"]) >= int(policy["follow_up_limit"]):
            raise PilotError(409, "follow-up ceiling is exhausted")
        store.update(conn, "pilot_candidate_assignments", assignment["id"], {
            "active_state": FOLLOW_UP_APPROVED,
            "updated_at": timestamp,
        })
        changes.append({"assignment": assignment, "state": FOLLOW_UP_APPROVED, "event": "follow_up.approved"})
    elif kind == "promote":
        exhausted = by_id.get(decision.payload["exhausted_assignment_id"])
        promoted = by_id.get(decision.payload["promoted_assignment_id"])
        if exhausted is None or exhausted["active_state"] != PROMOTION_DUE:
            raise PilotError(409, "promotion requires an exhausted PROMOTION_DUE assignment")
        if promoted is None or promoted["active_state"] != AVAILABLE:
            raise PilotError(409, "promotion target must be an unused AVAILABLE assignment")
        _validate_activation(
            policy,
            assignments,
            [{"assignment_id": promoted["id"], "not_before": decision.payload["not_before"]}],
            now=current,
        )
        store.update(conn, "pilot_candidate_assignments", exhausted["id"], {
            "active_state": REVISIT_LATER,
            "updated_at": timestamp,
        })
        conn.execute(
            "UPDATE appearance_opportunities SET status = 'REVISIT_LATER', updated_at = ? "
            "WHERE id = ? AND status = 'FOLLOW_UP_DUE'",
            (timestamp, exhausted["opportunity_id"]),
        )
        store.update(conn, "pilot_candidate_assignments", promoted["id"], {
            "active_state": ACTIVATION_APPROVED,
            "not_before": decision.payload["not_before"],
            "updated_at": timestamp,
        })
        changes.extend([
            {"assignment": exhausted, "state": REVISIT_LATER, "event": "promotion.exhausted"},
            {
                "assignment": promoted,
                "state": ACTIVATION_APPROVED,
                "event": "promotion.approved",
                "details": {"not_before": decision.payload["not_before"]},
            },
        ])
    elif kind == "fallback_rehearsal":
        lifecycle = "REHEARSAL_FALLBACK"
        for assignment in assignments:
            if assignment["active_state"] in ACTIVE_ASSIGNMENT_STATES:
                store.update(conn, "pilot_candidate_assignments", assignment["id"], {
                    "active_state": PAUSED,
                    "updated_at": timestamp,
                })
                changes.append({"assignment": assignment, "state": PAUSED, "event": "fallback.activated"})
    elif kind == "pause":
        lifecycle = "PAUSED"
        for assignment in assignments:
            if assignment["active_state"] in ACTIVE_ASSIGNMENT_STATES:
                store.update(conn, "pilot_candidate_assignments", assignment["id"], {
                    "active_state": PAUSED,
                    "updated_at": timestamp,
                })
                changes.append({"assignment": assignment, "state": PAUSED, "event": "run.paused"})
    elif kind != "wait":
        raise PilotError(422, "unsupported pilot decision")

    revision = int(run["revision"]) + 1
    state_changes = [
        {
            "assignment_id": item["assignment"]["id"],
            "from": item["assignment"]["active_state"],
            "to": item["state"],
        }
        for item in changes
    ]
    try:
        for item in changes:
            _event(
                conn,
                run=run,
                assignment=item["assignment"],
                revision=revision,
                event_kind=item["event"],
                resulting_state=item["state"],
                actor_id=actor_id,
                actor_role=actor_role,
                timestamp=timestamp,
                details=item.get("details"),
            )
        store.update(conn, "pilot_runs", run_id, {
            "revision": revision,
            "lifecycle_state": lifecycle,
            "updated_at": timestamp,
        })
        revised_run = {**run, "revision": revision, "lifecycle_state": lifecycle, "updated_at": timestamp}
        store.insert(conn, "pilot_decisions", {
            "id": _id("pilot_decision"),
            "tenant_id": tenant_id,
            "show_id": run.get("show_id", "legacy"),
            "pilot_run_id": run_id,
            "expected_revision": decision.expected_revision,
            "resulting_revision": revision,
            "decision_kind": kind,
            "decision_payload": decision.payload,
            "resulting_state_changes": state_changes,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "created_at": timestamp,
        })
        _write_projection(conn, revised_run, policy, now=current)
        _audit(
            conn,
            partnership_id=partnership_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_role=actor_role,
            event_type="pilot.decision_recorded",
            details={"run_id": run_id, "decision_kind": kind, "revision": revision},
            timestamp=timestamp,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _plan_response(conn, revised_run, policy)


def apply_opportunity_evidence(
    conn: sqlite3.Connection,
    opportunity_id: str,
    *,
    evidence_kind: str,
    evidence_reference: str | None,
    actor_id: str,
    actor_role: str,
    occurred_at: datetime,
    classification: str | None = None,
) -> bool:
    """Project typed opportunity evidence into any active Pilot run atomically.

    The caller owns the surrounding transaction. No raw contact or message body
    is accepted here: only typed classifications and opaque receipt references.
    """
    assignment = store.fetch_one(
        conn,
        "SELECT a.*, r.partnership_id, r.policy_id, r.revision, r.deadline_at, "
        "r.lifecycle_state, r.tenant_id AS run_tenant_id, r.created_by, r.created_role, "
        "r.created_at AS run_created_at, r.updated_at AS run_updated_at "
        "FROM pilot_candidate_assignments a JOIN pilot_runs r ON r.id = a.pilot_run_id "
        "WHERE a.opportunity_id = ? "
        "AND r.lifecycle_state IN ('ACTIVE', 'PAUSED', 'REHEARSAL_FALLBACK') "
        "ORDER BY r.created_at DESC LIMIT 1",
        (opportunity_id,),
    )
    if assignment is None:
        return False
    run = {
        "id": assignment["pilot_run_id"],
        "tenant_id": assignment["run_tenant_id"],
        "show_id": assignment.get("show_id", "legacy"),
        "partnership_id": assignment["partnership_id"],
        "policy_id": assignment["policy_id"],
        "revision": assignment["revision"],
        "deadline_at": assignment["deadline_at"],
        "lifecycle_state": assignment["lifecycle_state"],
        "created_by": assignment["created_by"],
        "created_role": assignment["created_role"],
        "created_at": assignment["run_created_at"],
        "updated_at": assignment["run_updated_at"],
    }
    policy = _policy(
        conn, str(run["policy_id"]), str(run["partnership_id"]), str(run["tenant_id"])
    )
    current_state = assignment["active_state"]
    resulting_state = current_state
    changes: dict[str, Any] = {}
    details: dict[str, Any] = {}
    production_evidence = {
        "consent.signed",
        "recording.ready",
        "recording.completed",
        "media.ingested",
    }
    if run["lifecycle_state"] != "ACTIVE" and evidence_kind not in production_evidence:
        raise PilotError(409, "this evidence is incompatible with the current pilot lifecycle")
    if evidence_kind == "outreach.sent":
        if current_state == ACTIVATION_APPROVED:
            resulting_state = AWAITING_REPLY
            changes["response_due_at"] = _iso(
                occurred_at + timedelta(hours=int(policy["initial_response_hours"]))
            )
        elif current_state == FOLLOW_UP_APPROVED:
            resulting_state = AWAITING_REPLY
            changes["response_due_at"] = _iso(
                occurred_at + timedelta(hours=int(policy["follow_up_response_hours"]))
            )
            changes["follow_ups_sent"] = int(assignment["follow_ups_sent"]) + 1
        else:
            raise PilotError(409, "outreach receipt is incompatible with the current pilot plan")
    elif evidence_kind == "reply.classified":
        if current_state not in {AWAITING_REPLY, FOLLOW_UP_DUE, FOLLOW_UP_APPROVED, PROMOTION_DUE}:
            raise PilotError(409, "reply evidence is incompatible with the current pilot assignment")
        details["classification"] = classification
        if classification == "UNSUBSCRIBE":
            resulting_state = OPTED_OUT
        elif classification in {"SOFT_DECLINE", "HARD_DECLINE"}:
            resulting_state = INFEASIBLE
        elif classification == "FOLLOW_UP_LATER":
            resulting_state = REVISIT_LATER
        else:
            resulting_state = REPLIED
        changes["response_due_at"] = None
    elif evidence_kind == "booking.confirmed":
        resulting_state = BOOKED
        changes["response_due_at"] = None
    elif evidence_kind == "contact_route.changed":
        route = store.fetch_one(
            conn, "SELECT id, usable FROM contact_routes WHERE opportunity_id = ?", (opportunity_id,)
        )
        if route is None or not route["usable"]:
            resulting_state = INFEASIBLE
        else:
            changes["route_id"] = route["id"]
    elif evidence_kind == "candidate.rejected":
        resulting_state = INFEASIBLE
    elif evidence_kind in production_evidence:
        details["production_evidence"] = evidence_kind
    else:
        return False
    changes.update({"active_state": resulting_state, "updated_at": _iso(occurred_at)})
    revision = int(run["revision"]) + 1
    store.update(conn, "pilot_candidate_assignments", assignment["id"], changes)
    _event(
        conn,
        run=run,
        assignment=assignment,
        revision=revision,
        event_kind=evidence_kind,
        resulting_state=resulting_state,
        actor_id=actor_id,
        actor_role=actor_role,
        timestamp=_iso(occurred_at),
        evidence_reference=evidence_reference,
        details=details,
    )
    store.update(conn, "pilot_runs", str(run["id"]), {
        "revision": revision,
        "updated_at": _iso(occurred_at),
    })
    lifecycle = run["lifecycle_state"]
    readiness = partnerships.command_center(
        conn, str(run["partnership_id"]), str(run["tenant_id"])
    )["pilot_readiness"]
    if readiness["pilot_complete"]:
        lifecycle = "COMPLETED"
        store.update(conn, "pilot_runs", str(run["id"]), {"lifecycle_state": lifecycle})
    revised_run = {
        **run,
        "revision": revision,
        "lifecycle_state": lifecycle,
        "updated_at": _iso(occurred_at),
    }
    _write_projection(conn, revised_run, policy, now=occurred_at)
    return True


def apply_partnership_review_evidence(
    conn: sqlite3.Connection,
    partnership_id: str,
    *,
    tenant_id: str,
    review_kind: str,
    actor_id: str,
    actor_role: str,
    occurred_at: datetime,
) -> bool:
    """Reproject an open run after a rehearsal or scorecard review receipt."""
    run = store.fetch_one(
        conn,
        "SELECT * FROM pilot_runs WHERE partnership_id = ? AND tenant_id = ? "
        "AND lifecycle_state IN ('ACTIVE', 'PAUSED', 'REHEARSAL_FALLBACK') "
        "ORDER BY created_at DESC LIMIT 1",
        (partnership_id, tenant_id),
    )
    if run is None or review_kind not in {"technical_rehearsal", "pilot_scorecard"}:
        return False
    policy = _policy(conn, run["policy_id"], partnership_id, tenant_id)
    revision = int(run["revision"]) + 1
    lifecycle = run["lifecycle_state"]
    readiness = partnerships.command_center(conn, partnership_id, tenant_id)["pilot_readiness"]
    if readiness["pilot_complete"]:
        lifecycle = "COMPLETED"
    timestamp = _iso(occurred_at)
    store.update(conn, "pilot_runs", run["id"], {
        "revision": revision,
        "lifecycle_state": lifecycle,
        "updated_at": timestamp,
    })
    revised_run = {
        **run,
        "revision": revision,
        "lifecycle_state": lifecycle,
        "updated_at": timestamp,
    }
    projection = _write_projection(conn, revised_run, policy, now=occurred_at)
    evidence = dict(projection["evidence"])
    evidence["partnership_review_kind"] = review_kind
    store.update(conn, "pilot_plan_projections", projection["id"], {"evidence": evidence})
    return True


__all__ = [
    "apply_opportunity_evidence",
    "apply_partnership_review_evidence",
    "get_plan",
    "import_policy",
    "record_decision",
    "start_run",
]
