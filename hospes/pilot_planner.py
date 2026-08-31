"""Deterministic, context-derived schedule enumeration for Pilot runs.

The planner never stores a global sequential/parallel doctrine. It enumerates
bounded activation schedules from the current policy, evidence, and assignment
state, excludes unsafe schedules, and explains the resulting ranking. It only
returns decisions for a human to approve; it has no delivery capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Iterable, Mapping

from .pilot_models import aware_datetime


AVAILABLE = "AVAILABLE"
ACTIVATION_APPROVED = "ACTIVATION_APPROVED"
AWAITING_REPLY = "AWAITING_REPLY"
FOLLOW_UP_DUE = "FOLLOW_UP_DUE"
FOLLOW_UP_APPROVED = "FOLLOW_UP_APPROVED"
PROMOTION_DUE = "PROMOTION_DUE"
REPLIED = "REPLIED"
BOOKED = "BOOKED"
REVISIT_LATER = "REVISIT_LATER"
OPTED_OUT = "OPTED_OUT"
INFEASIBLE = "INFEASIBLE"
PAUSED = "PAUSED"

INCOMPATIBLE_STATES = frozenset({REVISIT_LATER, OPTED_OUT, INFEASIBLE, PAUSED})


@dataclass(frozen=True)
class Candidate:
    assignment_id: str
    slot_order: int
    relationship_class: str
    social_cost: int
    route_id: str
    owner_id: str
    active_state: str
    not_before: datetime | None = None
    response_due_at: datetime | None = None
    follow_ups_sent: int = 0

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "Candidate":
        return cls(
            assignment_id=str(row["id"]),
            slot_order=int(row["slot_order"]),
            relationship_class=str(row["relationship_class"]),
            social_cost=int(row["social_cost"]),
            route_id=str(row["route_id"]),
            owner_id=str(row["owner_id"]),
            active_state=str(row["active_state"]),
            not_before=(
                aware_datetime(row["not_before"], "not_before")
                if row.get("not_before")
                else None
            ),
            response_due_at=(
                aware_datetime(row["response_due_at"], "response_due_at")
                if row.get("response_due_at")
                else None
            ),
            follow_ups_sent=int(row.get("follow_ups_sent") or 0),
        )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _base_constraints(policy: Mapping[str, Any]) -> list[str]:
    return [
        "human_send_only",
        f"follow_up_ceiling={int(policy['follow_up_limit'])}",
        f"relationship_exposure_budget={int(policy['relationship_exposure_budget'])}",
        "raw_contact_and_correspondence_forbidden",
    ]


def _fallback(
    policy: Mapping[str, Any],
    run: Mapping[str, Any],
    now: datetime,
    rationale: list[str],
    *,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    deadline = aware_datetime(run["deadline_at"], "run.deadline_at")
    rehearsal_by = deadline - timedelta(hours=int(policy["rehearsal_lead_hours"]))
    remaining = int((rehearsal_by - now).total_seconds() // 60)
    return {
        "action_kind": "fallback_rehearsal",
        "required_role": "producer",
        "risk_level": "fallback",
        "remaining_slack_minutes": remaining,
        "ranked_action": {
            "decision_kind": "fallback_rehearsal",
            "not_after": _iso(rehearsal_by),
            "required_role": "producer",
        },
        "alternatives": [],
        "candidate_timing": [],
        "constraints": _base_constraints(policy) + ["guest_path_not_deadline_feasible"],
        "rationale": rationale,
        "evidence": dict(evidence or {}),
    }


def _state_projection(
    policy: Mapping[str, Any],
    run: Mapping[str, Any],
    candidates: list[Candidate],
    now: datetime,
) -> dict[str, Any] | None:
    constraints = _base_constraints(policy)
    deadline = aware_datetime(run["deadline_at"], "run.deadline_at")
    production_hours = sum(int(value) for value in policy["production_gate_durations"].values())
    remaining = int(
        ((deadline - timedelta(hours=production_hours)) - now).total_seconds() // 60
    )
    by_state: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_state.setdefault(candidate.active_state, []).append(candidate)

    if run["lifecycle_state"] == "COMPLETED":
        return {
            "action_kind": "wait",
            "required_role": "producer",
            "risk_level": "complete",
            "remaining_slack_minutes": remaining,
            "ranked_action": {"decision_kind": "wait", "required_role": "producer"},
            "alternatives": [],
            "candidate_timing": [],
            "constraints": constraints + ["all_fourteen_pilot_gates_are_complete"],
            "rationale": ["The truthful fourteen-gate readiness projection marks this run complete."],
            "evidence": {"pilot_complete": True},
        }

    if run["lifecycle_state"] == "PAUSED":
        return {
            "action_kind": "pause",
            "required_role": "relationship_owner",
            "risk_level": "paused",
            "remaining_slack_minutes": remaining,
            "ranked_action": {"decision_kind": "wait", "required_role": "relationship_owner"},
            "alternatives": [],
            "candidate_timing": [],
            "constraints": constraints + ["pilot_run_paused"],
            "rationale": ["The run is explicitly paused; no activation is compatible."],
            "evidence": {"assignment_states": {key: len(value) for key, value in by_state.items()}},
        }
    if run["lifecycle_state"] == "REHEARSAL_FALLBACK":
        return _fallback(
            policy,
            run,
            now,
            ["The human-approved run state selects the technical rehearsal fallback."],
            evidence={"assignment_states": {key: len(value) for key, value in by_state.items()}},
        )
    if by_state.get(BOOKED):
        candidate = sorted(by_state[BOOKED], key=lambda item: item.slot_order)[0]
        return {
            "action_kind": "wait",
            "required_role": "producer",
            "risk_level": "normal" if remaining >= 0 else "elevated",
            "remaining_slack_minutes": remaining,
            "ranked_action": {
                "decision_kind": "wait",
                "assignment_id": candidate.assignment_id,
                "required_role": "producer",
            },
            "alternatives": [],
            "candidate_timing": [],
            "constraints": constraints + ["production_dependencies_now_control_deadline"],
            "rationale": ["A candidate is booked; outreach activation is no longer the critical path."],
            "evidence": {"booked_assignment_id": candidate.assignment_id},
        }
    incompatible = [item for item in candidates if item.active_state in INCOMPATIBLE_STATES]
    if incompatible:
        return _fallback(
            policy,
            run,
            now,
            [
                "At least one required slate assignment is no longer guest-path feasible.",
                "Pending outreach actions are invalidated and the rehearsal fallback is recommended.",
            ],
            evidence={
                "incompatible_assignment_ids": [item.assignment_id for item in incompatible]
            },
        )
    if by_state.get(REPLIED):
        return {
            "action_kind": "wait",
            "required_role": "producer",
            "risk_level": "normal" if remaining >= 0 else "elevated",
            "remaining_slack_minutes": remaining,
            "ranked_action": {"decision_kind": "wait", "required_role": "producer"},
            "alternatives": [],
            "candidate_timing": [],
            "constraints": constraints + ["reply_requires_human_booking_or_resolution"],
            "rationale": ["Reply evidence freezes incompatible outreach actions immediately."],
            "evidence": {"reply_count": len(by_state[REPLIED])},
        }
    if by_state.get(PROMOTION_DUE):
        exhausted = sorted(by_state[PROMOTION_DUE], key=lambda item: item.slot_order)[0]
        available = sorted(by_state.get(AVAILABLE, []), key=lambda item: item.slot_order)
        if not available:
            return _fallback(
                policy,
                run,
                now,
                ["The follow-up window expired and no unused candidate remains for promotion."],
                evidence={"exhausted_assignment_id": exhausted.assignment_id},
            )
        return {
            "action_kind": "promote",
            "required_role": "relationship_owner",
            "risk_level": "normal" if remaining >= int(policy["safety_reserve_hours"]) * 60 else "elevated",
            "remaining_slack_minutes": remaining,
            "ranked_action": {
                "decision_kind": "promote",
                "exhausted_assignment_id": exhausted.assignment_id,
                "promoted_assignment_id": available[0].assignment_id,
                "not_before": _iso(now),
                "required_role": "relationship_owner",
            },
            "alternatives": [],
            "candidate_timing": [],
            "constraints": constraints + ["promotion_must_be_atomic_and_human_approved"],
            "rationale": [
                "The single follow-up window expired without reply.",
                "Promotion preserves silence as REVISIT_LATER rather than fabricating a decline.",
            ],
            "evidence": {"follow_up_exhausted": exhausted.assignment_id},
        }
    if by_state.get(FOLLOW_UP_DUE):
        candidate = sorted(by_state[FOLLOW_UP_DUE], key=lambda item: item.slot_order)[0]
        if candidate.follow_ups_sent >= int(policy["follow_up_limit"]):
            return _fallback(
                policy,
                run,
                now,
                ["The configured follow-up ceiling is exhausted and promotion is not available."],
                evidence={"assignment_id": candidate.assignment_id},
            )
        return {
            "action_kind": "follow_up",
            "required_role": "producer",
            "risk_level": "normal" if remaining >= int(policy["safety_reserve_hours"]) * 60 else "elevated",
            "remaining_slack_minutes": remaining,
            "ranked_action": {
                "decision_kind": "follow_up",
                "assignment_id": candidate.assignment_id,
                "required_role": "producer",
            },
            "alternatives": [],
            "candidate_timing": [{
                "assignment_id": candidate.assignment_id,
                "response_due_at": _iso(candidate.response_due_at or now),
            }],
            "constraints": constraints + ["one_human_sent_follow_up_maximum"],
            "rationale": ["The planned initial response window elapsed without reply evidence."],
            "evidence": {"assignment_state": FOLLOW_UP_DUE},
        }
    awaiting = by_state.get(AWAITING_REPLY, [])
    if awaiting:
        due = min(
            (candidate.response_due_at for candidate in awaiting if candidate.response_due_at),
            default=now,
        )
        return {
            "action_kind": "wait",
            "required_role": "producer",
            "risk_level": "normal" if remaining >= int(policy["safety_reserve_hours"]) * 60 else "elevated",
            "remaining_slack_minutes": remaining,
            "ranked_action": {
                "decision_kind": "wait",
                "until": _iso(due),
                "required_role": "producer",
            },
            "alternatives": [],
            "candidate_timing": [
                {
                    "assignment_id": candidate.assignment_id,
                    "response_due_at": _iso(candidate.response_due_at or due),
                }
                for candidate in sorted(awaiting, key=lambda item: item.slot_order)
            ],
            "constraints": constraints + ["reply_or_opt_out_invalidates_pending_actions"],
            "rationale": ["One or more human-sent asks remain inside their response window."],
            "evidence": {"awaiting_reply_count": len(awaiting)},
        }
    approved = by_state.get(ACTIVATION_APPROVED, []) + by_state.get(FOLLOW_UP_APPROVED, [])
    if approved:
        next_time = min((candidate.not_before or now for candidate in approved), default=now)
        return {
            "action_kind": "wait",
            "required_role": "producer",
            "risk_level": "normal" if remaining >= 0 else "elevated",
            "remaining_slack_minutes": remaining,
            "ranked_action": {
                "decision_kind": "wait",
                "until": _iso(next_time),
                "required_role": "producer",
            },
            "alternatives": [],
            "candidate_timing": [
                {"assignment_id": item.assignment_id, "not_before": _iso(item.not_before or now)}
                for item in sorted(approved, key=lambda item: item.slot_order)
            ],
            "constraints": constraints + ["human_must_preview_review_copy_and_send_externally"],
            "rationale": ["Activation is approved, but no send receipt exists; HOSPES cannot send."],
            "evidence": {"activation_approved_count": len(approved)},
        }
    return None


def _patterns(count: int, initial_hours: int, cycle_hours: int) -> Iterable[tuple[str, tuple[int, ...]]]:
    if count == 1:
        yield "single", (0,)
        return
    yield "parallel", tuple(0 for _ in range(count))
    yield "staggered", tuple(index * initial_hours for index in range(count))
    if count == 3:
        yield "mixed", (0, 0, initial_hours)
    yield "sequential", tuple(index * cycle_hours for index in range(count))


def _overlap(left: tuple[datetime, datetime], right: tuple[datetime, datetime]) -> float:
    start = max(left[0], right[0])
    end = min(left[1], right[1])
    return max(0.0, (end - start).total_seconds() / 3600)


def build_projection(
    policy: Mapping[str, Any],
    run: Mapping[str, Any],
    assignments: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the ranked plan and alternatives for the current evidence."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = sorted(
        (Candidate.from_mapping(item) for item in assignments),
        key=lambda item: item.slot_order,
    )
    state_plan = _state_projection(policy, run, candidates, current)
    if state_plan is not None:
        return state_plan

    constraints = _base_constraints(policy)
    allowed_classes = set(policy["allowed_relationship_classes"])
    eligible = [
        item
        for item in candidates
        if item.active_state == AVAILABLE
        and item.relationship_class in allowed_classes
        and item.social_cost <= int(policy["max_social_cost"])
        and item.social_cost <= int(policy["relationship_exposure_budget"])
        and item.route_id
        and item.owner_id
    ]
    if len(eligible) != int(policy["candidate_count"]):
        return _fallback(
            policy,
            run,
            current,
            ["The complete policy-required candidate set is not eligible and independently routable."],
            evidence={"eligible_count": len(eligible), "required_count": int(policy["candidate_count"])},
        )

    deadline = aware_datetime(run["deadline_at"], "run.deadline_at")
    production_hours = sum(int(value) for value in policy["production_gate_durations"].values())
    guest_latest = deadline - timedelta(hours=production_hours)
    reserve_latest = guest_latest - timedelta(hours=int(policy["safety_reserve_hours"]))
    cycle_hours = int(policy["initial_response_hours"]) + (
        int(policy["follow_up_response_hours"]) if int(policy["follow_up_limit"]) else 0
    )
    reserve_window_hours = (reserve_latest - current).total_seconds() / 3600
    if reserve_window_hours >= 2 * cycle_hours:
        target_count = 1
    elif reserve_window_hours >= cycle_hours:
        target_count = min(2, len(eligible))
    else:
        target_count = len(eligible)

    schedules: list[dict[str, Any]] = []
    ordered = sorted(eligible, key=lambda item: (item.social_cost, item.slot_order))
    for count in range(1, len(ordered) + 1):
        for subset in combinations(ordered, count):
            for schedule_kind, offsets in _patterns(
                count, int(policy["initial_response_hours"]), cycle_hours
            ):
                intervals = [
                    (
                        current + timedelta(hours=offset),
                        current + timedelta(hours=offset + cycle_hours),
                    )
                    for offset in offsets
                ]
                conflict = False
                overlap_hours = 0.0
                for left_index, left in enumerate(subset):
                    for right_index in range(left_index + 1, len(subset)):
                        overlap = _overlap(intervals[left_index], intervals[right_index])
                        if overlap and (
                            left.route_id == subset[right_index].route_id
                            or left.owner_id == subset[right_index].owner_id
                        ):
                            conflict = True
                            break
                        overlap_hours += overlap * (left.social_cost + subset[right_index].social_cost)
                    if conflict:
                        break
                if conflict:
                    continue
                completion = max(end for _start, end in intervals)
                if completion > guest_latest:
                    continue
                reserve_retained = completion <= reserve_latest
                slack_minutes = int((guest_latest - completion).total_seconds() // 60)
                concurrent = any(
                    _overlap(intervals[left], intervals[right]) > 0
                    for left in range(len(intervals))
                    for right in range(left + 1, len(intervals))
                )
                required_role = "relationship_owner" if concurrent else "producer"
                timing = [
                    {
                        "assignment_id": item.assignment_id,
                        "slot_order": item.slot_order,
                        "not_before": _iso(intervals[index][0]),
                        "response_window_ends_at": _iso(intervals[index][1]),
                        "latest_safe_guest_at": _iso(guest_latest),
                    }
                    for index, item in enumerate(subset)
                ]
                schedule_constraints = list(constraints)
                if concurrent:
                    schedule_constraints.extend([
                        "concurrent_routes_and_owners_are_independent",
                        "concurrent_activation_requires_relationship_owner",
                    ])
                if not reserve_retained:
                    schedule_constraints.append(
                        f"reserve_deficit_minutes={int(policy['safety_reserve_hours']) * 60 - slack_minutes}"
                    )
                relationship_exposure = sum(item.social_cost for item in subset) + overlap_hours
                schedules.append({
                    "schedule_kind": schedule_kind,
                    "assignments": timing,
                    "required_role": required_role,
                    "risk_level": "normal" if reserve_retained else "elevated",
                    "remaining_slack_minutes": slack_minutes,
                    "constraints": schedule_constraints,
                    "relationship_exposure": relationship_exposure,
                    "operator_load": count * (1 + int(policy["follow_up_limit"])),
                    "score": (
                        0 if reserve_retained else 1,
                        abs(count - target_count),
                        relationship_exposure,
                        count * (1 + int(policy["follow_up_limit"])),
                        completion.timestamp(),
                        tuple(item.slot_order for item in subset),
                        schedule_kind,
                    ),
                })

    if not schedules:
        return _fallback(
            policy,
            run,
            current,
            ["No eligible guest activation schedule can leave enough time for production."],
            evidence={"guest_latest_at": _iso(guest_latest)},
        )

    if any(item["risk_level"] == "normal" for item in schedules):
        schedules.sort(key=lambda item: item["score"])
    else:
        # With no reserve-retaining option, the reserve deficit is the first
        # ordering fact, followed by contextual coverage and human/social load.
        reserve_minutes = int(policy["safety_reserve_hours"]) * 60
        schedules.sort(key=lambda item: (
            reserve_minutes - item["remaining_slack_minutes"],
            abs(len(item["assignments"]) - target_count),
            item["relationship_exposure"],
            item["operator_load"],
            item["score"],
        ))
    winner = schedules[0]
    alternatives = []
    for candidate in schedules[1:6]:
        alternatives.append({
            key: value
            for key, value in candidate.items()
            if key not in {"score", "relationship_exposure", "operator_load"}
        })
    ranked_action = {
        "decision_kind": "activate_set",
        "schedule_kind": winner["schedule_kind"],
        "assignments": [
            {
                "assignment_id": item["assignment_id"],
                "not_before": item["not_before"],
            }
            for item in winner["assignments"]
        ],
        "required_role": winner["required_role"],
    }
    rationale = [
        f"Deadline slack supports {target_count} planned activation(s) in the current evidence window.",
        f"The {winner['schedule_kind']} schedule retains {winner['remaining_slack_minutes']} minutes after production gates.",
        "It outranks alternatives by reserve retention, fallback coverage, relationship exposure, and operator load.",
    ]
    if winner["risk_level"] == "elevated":
        rationale.append("No schedule retained the configured reserve; this is deadline-feasible elevated risk.")
    return {
        "action_kind": "activate_set",
        "required_role": winner["required_role"],
        "risk_level": winner["risk_level"],
        "remaining_slack_minutes": winner["remaining_slack_minutes"],
        "ranked_action": ranked_action,
        "alternatives": alternatives,
        "candidate_timing": winner["assignments"],
        "constraints": winner["constraints"],
        "rationale": rationale,
        "evidence": {
            "deadline_at": _iso(deadline),
            "guest_latest_at": _iso(guest_latest),
            "reserve_latest_at": _iso(reserve_latest),
            "target_activation_count": target_count,
            "enumerated_feasible_schedules": len(schedules),
        },
    }


__all__ = [
    "ACTIVATION_APPROVED",
    "AVAILABLE",
    "AWAITING_REPLY",
    "BOOKED",
    "FOLLOW_UP_APPROVED",
    "FOLLOW_UP_DUE",
    "INFEASIBLE",
    "OPTED_OUT",
    "PAUSED",
    "PROMOTION_DUE",
    "REPLIED",
    "REVISIT_LATER",
    "build_projection",
]
