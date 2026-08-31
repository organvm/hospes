"""Deterministic scenario proofs for the context-derived Pilot planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hospes.pilot_planner import build_projection


NOW = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)


def policy(deadline_after_hours: int) -> dict:
    return {
        "deadline_at": (NOW + timedelta(hours=deadline_after_hours)).isoformat(),
        "candidate_count": 3,
        "allowed_relationship_classes": ["C2", "C3"],
        "max_social_cost": 2,
        "follow_up_limit": 1,
        "initial_response_hours": 48,
        "follow_up_response_hours": 24,
        "production_gate_durations": {
            "booking": 12,
            "consent": 6,
            "brief": 8,
            "asset_preflight": 8,
            "rehearsal": 12,
        },
        "safety_reserve_hours": 24,
        "relationship_exposure_budget": 2,
        "rehearsal_lead_hours": 48,
    }


def run(deadline_after_hours: int, state: str = "ACTIVE") -> dict:
    return {
        "deadline_at": (NOW + timedelta(hours=deadline_after_hours)).isoformat(),
        "lifecycle_state": state,
    }


def slate(*, shared_owner: bool = False, third_cost: int = 2) -> list[dict]:
    return [
        {
            "id": f"assignment_{index}",
            "slot_order": index,
            "relationship_class": "C2" if index != 2 else "C3",
            "social_cost": third_cost if index == 3 else 1,
            "route_id": f"route_{index}",
            "owner_id": "shared_owner" if shared_owner else f"owner_{index}",
            "active_state": "AVAILABLE",
            "follow_ups_sent": 0,
        }
        for index in range(1, 4)
    ]


def test_high_slack_prefers_low_exposure_single_activation() -> None:
    projection = build_projection(policy(230), run(230), slate(), now=NOW)

    assert projection["ranked_action"]["schedule_kind"] == "single"
    assert len(projection["ranked_action"]["assignments"]) == 1
    assert projection["ranked_action"]["assignments"][0]["assignment_id"] == "assignment_1"
    assert projection["risk_level"] == "normal"


def test_moderate_slack_favors_independent_staggered_activation() -> None:
    projection = build_projection(policy(200), run(200), slate(), now=NOW)

    assert projection["ranked_action"]["schedule_kind"] == "staggered"
    assert len(projection["ranked_action"]["assignments"]) == 2
    assert projection["required_role"] == "relationship_owner"
    assert "concurrent_routes_and_owners_are_independent" in projection["constraints"]


def test_shared_owner_and_excess_social_cost_prevent_unsafe_overlap() -> None:
    shared = build_projection(policy(120), run(120), slate(shared_owner=True), now=NOW)
    excessive = build_projection(policy(230), run(230), slate(third_cost=3), now=NOW)

    assert len(shared["ranked_action"]["assignments"]) == 1
    assert shared["required_role"] == "producer"
    assert excessive["action_kind"] == "fallback_rehearsal"


def test_low_slack_uses_broader_activation_only_within_policy() -> None:
    projection = build_projection(policy(120), run(120), slate(), now=NOW)

    assert projection["ranked_action"]["schedule_kind"] == "parallel"
    assert len(projection["ranked_action"]["assignments"]) == 3
    assert projection["required_role"] == "relationship_owner"
    assert projection["risk_level"] == "elevated"


def test_reply_and_opt_out_freeze_incompatible_actions() -> None:
    replied = slate()
    replied[0]["active_state"] = "REPLIED"
    replied[1].update({
        "active_state": "AWAITING_REPLY",
        "response_due_at": (NOW + timedelta(hours=24)).isoformat(),
    })
    opted_out = slate()
    opted_out[0]["active_state"] = "OPTED_OUT"
    opted_out[1].update({
        "active_state": "AWAITING_REPLY",
        "response_due_at": (NOW + timedelta(hours=24)).isoformat(),
    })

    reply_plan = build_projection(policy(230), run(230), replied, now=NOW)
    opt_out_plan = build_projection(policy(230), run(230), opted_out, now=NOW)

    assert reply_plan["action_kind"] == "wait"
    assert "reply_requires_human_booking_or_resolution" in reply_plan["constraints"]
    assert opt_out_plan["action_kind"] == "fallback_rehearsal"


def test_infeasible_guest_path_selects_rehearsal_and_output_is_deterministic() -> None:
    impossible = slate()
    impossible[2]["active_state"] = "INFEASIBLE"

    first = build_projection(policy(120), run(120), impossible, now=NOW)
    second = build_projection(policy(120), run(120), impossible, now=NOW)

    assert first == second
    assert first["action_kind"] == "fallback_rehearsal"
    assert first["ranked_action"]["decision_kind"] == "fallback_rehearsal"
