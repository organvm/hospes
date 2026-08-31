"""Persistence, authority, revision, and evidence tests for Pilot runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from hospes import partnerships, pilot_service, service, store
from hospes.pilot_models import PilotDecisionInput, PilotError
from conftest import synthetic_bearer_authenticator


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "partnerships" / "example-private-pilot.yaml"
POLICY = ROOT / "config" / "pilot_policies" / "example-pilot-1.yaml"
TENANT = "private_pilot"
OWNER = service.HumanActor("ari_owner", service.HumanRole.RELATIONSHIP_OWNER, TENANT)
PRODUCER = service.HumanActor("producer_fixture", service.HumanRole.PRODUCER, TENANT)
# Deterministic simulated clock anchored well before the policy deadline.
# Every test in this module uses this instead of datetime.now(UTC) so that
# the suite is hermetically stable regardless of when it runs.
SIMULATED_NOW = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)


def prepared_run(conn, *, now: datetime | None = None) -> tuple[str, dict]:
    current = now or SIMULATED_NOW
    imported = partnerships.import_template(
        conn,
        TEMPLATE,
        tenant_id=TENANT,
        actor_id="producer_fixture",
        actor_role="producer",
        show_id="private_pilot",
    )
    partnership_id = imported.partnership_id
    partnerships.create_item(
        conn,
        partnership_id,
        partnerships.PartnershipItemInput.from_dict(
            {
                "item_key": "decision.recording_window",
                "category": "decision",
                "title": "Recurring recording window",
                "summary": "The recurring private-pilot window is agreed in its external owner.",
                "owner": "ari_owner",
                "state": "agreed",
                "external_reference": "calendar://fixture/recurring-window",
            }
        ),
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
    )
    partnerships.record_review(
        conn,
        partnership_id,
        {
            "review_kind": "ari_review",
            "decisions_count": 3,
            "coverage_met": 3,
            "coverage_total": 3,
            "external_reference": "registry://fixture/ari-review",
            "occurred_at": (current - timedelta(minutes=2)).isoformat(),
        },
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
    )
    for slot in range(1, 4):
        opportunity_id = f"10000000-0000-4000-8000-00000000000{slot}"
        timestamp = current.isoformat()
        store.insert(
            conn,
            "appearance_opportunities",
            {
                "id": opportunity_id,
                "tenant_id": TENANT,
                "network_id": "ari_network",
                "show_id": "private_pilot",
                "guest_name": f"Synthetic Pilot Candidate {slot}",
                "why_guest": "This synthetic candidate proves a context-derived Pilot schedule.",
                "why_now": "The synthetic deadline requires executable planner evidence.",
                "proposed_artifact": "A bounded synthetic decision map",
                "relationship_class": "C2" if slot < 3 else "C3",
                "relationship_owner": f"ari_owner_{slot}",
                "social_cost_1_5": 1 if slot < 3 else 2,
                "ari_effort": "review_only",
                "preferred_city": "Los Angeles",
                "next_action": "Use the human-approved Pilot plan.",
                "source_provenance": "Synthetic Pilot execution fixture.",
                "status": "APPROVED",
                "disposition": "APPROVED",
                "episode_thesis": "Context should determine safe outreach timing.",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        store.insert(
            conn,
            "contact_routes",
            {
                "id": f"route-{slot}",
                "tenant_id": TENANT,
                "opportunity_id": opportunity_id,
                "route_type": "producer_system",
                "route_label": f"Opaque synthetic route {slot}",
                "source_provenance": "Synthetic verified route provenance.",
                "verified_at": timestamp,
                "usable": True,
                "created_at": timestamp,
            },
        )
        partnerships.select_pilot_candidate(
            conn,
            partnership_id,
            opportunity_id,
            slot,
            tenant_id=TENANT,
            actor_id=OWNER.actor_id,
            actor_role=OWNER.role.value,
        )
    policy = pilot_service.import_policy(
        conn,
        partnership_id,
        POLICY,
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
        now=current,
    )
    started = pilot_service.start_run(
        conn,
        partnership_id,
        {"policy_id": policy["id"]},
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
        now=current,
    )
    return partnership_id, started


def decision_from_plan(plan: dict) -> PilotDecisionInput:
    action = plan["ranked_action"]
    return PilotDecisionInput.from_mapping(
        {
            "decision_kind": "activate_set",
            "expected_revision": plan["revision"],
            "assignments": action["assignments"],
        }
    )


def test_policy_import_is_checksummed_idempotent_and_attributable(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "policy.sqlite3")
    partnership_id, started = prepared_run(conn)
    again = pilot_service.import_policy(
        conn,
        partnership_id,
        POLICY,
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
    )

    assert again["outcome"] == "unchanged"
    assert again["policy_digest"] == started["policy_digest"]
    assert conn.execute("SELECT COUNT(*) FROM pilot_policies").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM partnership_audit_events WHERE event_type = 'pilot.policy_imported'"
        ).fetchone()[0]
        == 1
    )


def test_policy_import_rejects_cross_show_ownership(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "foreign-policy.sqlite3")
    imported = partnerships.import_template(
        conn,
        TEMPLATE,
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
        show_id="private_pilot",
    )
    source = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    source["show_id"] = "foreign_show"
    foreign_policy = tmp_path / "foreign-policy.yaml"
    foreign_policy.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(PilotError, match="another show") as caught:
        pilot_service.import_policy(
            conn,
            imported.partnership_id,
            foreign_policy,
            tenant_id=TENANT,
            actor_id=PRODUCER.actor_id,
            actor_role=PRODUCER.role.value,
        )
    assert caught.value.status_code == 403


def test_run_requires_relationship_owner_and_all_four_start_gates(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "authority.sqlite3")
    imported = partnerships.import_template(
        conn,
        TEMPLATE,
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
        show_id="private_pilot",
    )
    policy = pilot_service.import_policy(
        conn,
        imported.partnership_id,
        POLICY,
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
    )
    with pytest.raises(PilotError) as incomplete:
        pilot_service.start_run(
            conn,
            imported.partnership_id,
            {"policy_id": policy["id"]},
            tenant_id=TENANT,
            actor_id=OWNER.actor_id,
            actor_role=OWNER.role.value,
        )
    assert incomplete.value.status_code == 409

    complete_conn = store.connect(tmp_path / "complete.sqlite3")
    partnership_id, started = prepared_run(complete_conn)
    assert started["action_kind"] == "activate_set"
    with pytest.raises(PilotError) as duplicate:
        pilot_service.start_run(
            complete_conn,
            partnership_id,
            {"policy_id": started["policy_id"]},
            tenant_id=TENANT,
            actor_id=PRODUCER.actor_id,
            actor_role=PRODUCER.role.value,
        )
    assert duplicate.value.status_code == 403


def test_decisions_are_revision_checked_append_only_and_human_only(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "decisions.sqlite3")
    partnership_id, plan = prepared_run(conn)
    choice = decision_from_plan(plan)

    if len(choice.payload["assignments"]) > 1:
        with pytest.raises(PilotError) as producer_blocked:
            pilot_service.record_decision(
                conn,
                partnership_id,
                plan["run_id"],
                choice,
                tenant_id=TENANT,
                actor_id=PRODUCER.actor_id,
                actor_role=PRODUCER.role.value,
            )
        assert producer_blocked.value.status_code == 403

    revised = pilot_service.record_decision(
        conn,
        partnership_id,
        plan["run_id"],
        choice,
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
        now=datetime(2026, 7, 22, 18, 0, tzinfo=UTC),
    )
    assert revised["revision"] == plan["revision"] + 1
    assert all(
        assignment["active_state"] == "ACTIVATION_APPROVED"
        for assignment in revised["assignments"]
        if assignment["id"] in {item["assignment_id"] for item in choice.payload["assignments"]}
    )
    remaining = next(assignment for assignment in revised["assignments"] if assignment["active_state"] == "AVAILABLE")
    with pytest.raises(PilotError) as active_ask_blocked:
        pilot_service.record_decision(
            conn,
            partnership_id,
            plan["run_id"],
            PilotDecisionInput.from_mapping(
                {
                    "decision_kind": "activate_set",
                    "expected_revision": revised["revision"],
                    "assignments": [
                        {
                            "assignment_id": remaining["id"],
                            "not_before": (SIMULATED_NOW + timedelta(minutes=1)).isoformat(),
                        }
                    ],
                }
            ),
            tenant_id=TENANT,
            actor_id=PRODUCER.actor_id,
            actor_role=PRODUCER.role.value,
        )
    assert active_ask_blocked.value.status_code == 403
    with pytest.raises(PilotError) as stale:
        pilot_service.record_decision(
            conn,
            partnership_id,
            plan["run_id"],
            choice,
            tenant_id=TENANT,
            actor_id=OWNER.actor_id,
            actor_role=OWNER.role.value,
        )
    assert stale.value.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM pilot_decisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pilot_plan_projections").fetchone()[0] == 2


def test_no_response_follow_up_and_atomic_promotion_preserve_history(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "promotion.sqlite3")
    start_time = SIMULATED_NOW - timedelta(days=2)
    partnership_id, plan = prepared_run(conn, now=start_time)
    activated = pilot_service.record_decision(
        conn,
        partnership_id,
        plan["run_id"],
        decision_from_plan(plan),
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
        now=start_time + timedelta(minutes=1),
    )
    active = next(
        assignment for assignment in activated["assignments"] if assignment["active_state"] == "ACTIVATION_APPROVED"
    )
    service.create_draft(
        conn,
        active["opportunity_id"],
        service.DraftCreate.from_dict({"kind": "initial"}),
        PRODUCER,
    )
    sent_at = start_time + timedelta(hours=10)
    service.record_receipt(
        conn,
        active["opportunity_id"],
        service.ReceiptCreate.from_dict(
            {
                "receipt_type": "outreach.sent",
                "external_reference": "mailbox://fixture/initial-send",
                "occurred_at": sent_at.isoformat(),
                "details": {},
            }
        ),
        PRODUCER,
    )
    followup_due = pilot_service.get_plan(
        conn,
        partnership_id,
        plan["run_id"],
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
        now=sent_at + timedelta(hours=50),
    )
    assert followup_due["action_kind"] == "follow_up"
    assert followup_due["assignments"][0]["active_state"] == "FOLLOW_UP_DUE"

    approved_followup = pilot_service.record_decision(
        conn,
        partnership_id,
        plan["run_id"],
        PilotDecisionInput.from_mapping(
            {
                "decision_kind": "follow_up",
                "expected_revision": followup_due["revision"],
                "assignment_id": active["id"],
            }
        ),
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
        now=sent_at + timedelta(hours=50),
    )
    assert approved_followup["assignments"][0]["active_state"] == "FOLLOW_UP_APPROVED"
    service.create_draft(
        conn,
        active["opportunity_id"],
        service.DraftCreate.from_dict({"kind": "follow_up"}),
        PRODUCER,
    )
    followup_sent_at = sent_at + timedelta(hours=51)
    service.record_receipt(
        conn,
        active["opportunity_id"],
        service.ReceiptCreate.from_dict(
            {
                "receipt_type": "outreach.sent",
                "external_reference": "mailbox://fixture/follow-up-send",
                "occurred_at": followup_sent_at.isoformat(),
                "details": {},
            }
        ),
        PRODUCER,
    )
    promotion_due = pilot_service.get_plan(
        conn,
        partnership_id,
        plan["run_id"],
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
        now=followup_sent_at + timedelta(hours=25),
    )
    assert promotion_due["action_kind"] == "promote"
    promotion = promotion_due["ranked_action"]
    promoted = pilot_service.record_decision(
        conn,
        partnership_id,
        plan["run_id"],
        PilotDecisionInput.from_mapping(
            {
                "decision_kind": "promote",
                "expected_revision": promotion_due["revision"],
                "exhausted_assignment_id": promotion["exhausted_assignment_id"],
                "promoted_assignment_id": promotion["promoted_assignment_id"],
                "not_before": promotion["not_before"],
            }
        ),
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
        now=followup_sent_at + timedelta(hours=25),
    )
    states = {assignment["id"]: assignment["active_state"] for assignment in promoted["assignments"]}
    assert states[promotion["exhausted_assignment_id"]] == "REVISIT_LATER"
    assert states[promotion["promoted_assignment_id"]] == "ACTIVATION_APPROVED"
    events = store.fetch_all(
        conn,
        "SELECT * FROM pilot_assignment_events WHERE assignment_id = ? ORDER BY run_revision, created_at",
        (active["id"],),
    )
    assert [event["resulting_state"] for event in events] == [
        "AVAILABLE",
        "ACTIVATION_APPROVED",
        "AWAITING_REPLY",
        "FOLLOW_UP_DUE",
        "FOLLOW_UP_APPROVED",
        "AWAITING_REPLY",
        "PROMOTION_DUE",
        "REVISIT_LATER",
    ]


def test_reply_and_opt_out_receipts_invalidate_pending_actions(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "reply.sqlite3")
    partnership_id, plan = prepared_run(conn)
    activated = pilot_service.record_decision(
        conn,
        partnership_id,
        plan["run_id"],
        decision_from_plan(plan),
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
        now=SIMULATED_NOW,
    )
    active = next(item for item in activated["assignments"] if item["active_state"] == "ACTIVATION_APPROVED")
    service.create_draft(
        conn,
        active["opportunity_id"],
        service.DraftCreate.from_dict({"kind": "initial"}),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        active["opportunity_id"],
        service.ReceiptCreate.from_dict(
            {
                "receipt_type": "outreach.sent",
                "external_reference": "mailbox://fixture/reply-initial",
                "occurred_at": (SIMULATED_NOW - timedelta(minutes=2)).isoformat(),
                "details": {},
            }
        ),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        active["opportunity_id"],
        service.ReceiptCreate.from_dict(
            {
                "receipt_type": "reply.classified",
                "external_reference": "mailbox://fixture/opt-out",
                "occurred_at": (SIMULATED_NOW - timedelta(minutes=1)).isoformat(),
                "details": {"classification": "UNSUBSCRIBE"},
            }
        ),
        PRODUCER,
    )
    current = pilot_service.get_plan(
        conn,
        partnership_id,
        plan["run_id"],
        tenant_id=TENANT,
        actor_id=OWNER.actor_id,
        actor_role=OWNER.role.value,
    )
    assert current["action_kind"] == "fallback_rehearsal"
    assert next(item for item in current["assignments"] if item["id"] == active["id"])["active_state"] == "OPTED_OUT"
    assert not any(
        item.get("assignment_id") == active["id"] for item in current["ranked_action"].get("assignments", [])
    )


def test_versioned_plan_and_decision_api_return_current_revision(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hospes.api import create_app

    database = tmp_path / "pilot-api.sqlite3"
    setup_conn = store.connect(database)
    partnership_id, started = prepared_run(setup_conn)
    setup_conn.close()
    client = TestClient(
        create_app(
            str(database),
            runtime_kind="synthetic_test",
            _test_bearer_authenticator=synthetic_bearer_authenticator(
                {
                    "synthetic-pilot-token": (
                        PRODUCER.actor_id,
                        PRODUCER.role.value,
                        TENANT,
                    )
                }
            ),
            csrf_required=False,
        )
    )
    headers = {
        "Authorization": "Bearer synthetic-pilot-token",
        "X-Hospes-Actor": PRODUCER.actor_id,
        "X-Hospes-Role": PRODUCER.role.value,
        "X-Hospes-Tenant": TENANT,
    }

    plan = client.get(
        f"/v1/partnerships/{partnership_id}/pilot-runs/{started['run_id']}/plan",
        headers=headers,
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["revision"] == started["revision"]
    waited = client.post(
        f"/v1/partnerships/{partnership_id}/pilot-runs/{started['run_id']}/decisions",
        json={"decision_kind": "wait", "expected_revision": started["revision"]},
        headers=headers,
    )
    assert waited.status_code == 200, waited.text
    assert waited.json()["revision"] == started["revision"] + 1
    stale = client.post(
        f"/v1/partnerships/{partnership_id}/pilot-runs/{started['run_id']}/decisions",
        json={"decision_kind": "wait", "expected_revision": started["revision"]},
        headers=headers,
    )
    assert stale.status_code == 409


def test_run_completes_only_when_the_fourteen_gate_projection_is_true(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "completion.sqlite3")
    partnership_id, started = prepared_run(conn)
    primary = started["assignments"][0]["opportunity_id"]
    timestamp = SIMULATED_NOW.isoformat()
    store.insert(
        conn,
        "episode_briefs",
        {
            "id": "brief-completion",
            "tenant_id": TENANT,
            "opportunity_id": primary,
            "thesis": "A verified synthetic completion thesis.",
            "research_claims": [{"claim": "Synthetic", "evidence_source": "fixture", "verified": True}],
            "segments": [{"title": "Gate", "objective": "Prove completion."}],
            "created_by": PRODUCER.actor_id,
            "created_at": timestamp,
        },
    )
    store.insert(
        conn,
        "asset_packages",
        {
            "id": "package-completion",
            "tenant_id": TENANT,
            "opportunity_id": primary,
            "assets": [
                {"kind": kind, "custody_target": f"production://fixture/{kind}"}
                for kind in sorted(service.REQUIRED_PREFLIGHT_ASSETS)
            ],
            "status": "DECLARED",
            "declared_by": PRODUCER.actor_id,
            "created_at": timestamp,
        },
    )
    receipt_details = {
        "outreach.sent": {},
        "booking.confirmed": {
            "studio_ref": "studio://fixture/completion",
            "producer_ref": "producer://fixture/completion",
            "recording_time": timestamp,
        },
        "consent.signed": {"private_pilot": True, "clip_scope": "none"},
        "recording.ready": {
            "preflight_ref": "preflight://fixture/completion",
            "asset_package_id": "package-completion",
        },
        "recording.completed": {"session_kind": "guest_pilot"},
        "media.ingested": {
            "master_ref": "media://fixture/completion-master",
            "checksum_ref": "checksum://fixture/completion-sha256",
        },
    }
    for index, (receipt_type, details) in enumerate(receipt_details.items(), start=1):
        store.insert(
            conn,
            "operational_receipts",
            {
                "id": f"completion-receipt-{index}",
                "tenant_id": TENANT,
                "opportunity_id": primary,
                "receipt_type": receipt_type,
                "external_reference": f"registry://fixture/completion-{index}",
                "occurred_at": timestamp,
                "actor_id": PRODUCER.actor_id,
                "actor_role": PRODUCER.role.value,
                "details": details,
                "created_at": timestamp,
            },
        )
    partnerships.record_review(
        conn,
        partnership_id,
        {
            "review_kind": "technical_rehearsal",
            "decisions_count": 1,
            "coverage_met": 1,
            "coverage_total": 1,
            "external_reference": "rehearsal://fixture/completion",
            "occurred_at": timestamp,
        },
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
    )
    assert (
        store.fetch_one(conn, "SELECT lifecycle_state FROM pilot_runs WHERE id = ?", (started["run_id"],))[
            "lifecycle_state"
        ]
        == "ACTIVE"
    )
    partnerships.record_review(
        conn,
        partnership_id,
        {
            "review_kind": "pilot_scorecard",
            "decisions_count": 1,
            "coverage_met": 14,
            "coverage_total": 14,
            "external_reference": "scorecard://fixture/completion",
            "occurred_at": timestamp,
        },
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
    )
    completed = pilot_service.get_plan(
        conn,
        partnership_id,
        started["run_id"],
        tenant_id=TENANT,
        actor_id=PRODUCER.actor_id,
        actor_role=PRODUCER.role.value,
    )
    assert completed["lifecycle_state"] == "COMPLETED"
    assert completed["risk_level"] == "complete"
    assert completed["evidence"]["pilot_complete"] is True
