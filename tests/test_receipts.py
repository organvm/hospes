"""Receipt-driven private-pilot lifecycle and readiness gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hospes import service, store

PRODUCER = service.HumanActor(
    "producer_fixture", service.HumanRole.PRODUCER, "pilot_tenant"
)
HOST = service.HumanActor("host_fixture", service.HumanRole.HOST, "pilot_tenant")
OTHER_TENANT = service.HumanActor(
    "producer_other", service.HumanRole.PRODUCER, "other_tenant"
)


def receipt(
    receipt_type: str,
    external_reference: str,
    *,
    details: dict | None = None,
    minutes_ago: int = 1,
) -> service.ReceiptCreate:
    return service.ReceiptCreate.from_dict({
        "receipt_type": receipt_type,
        "external_reference": external_reference,
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
        "details": details or {},
    })


def create_editorial_candidate(conn) -> str:
    created = service.create_opportunity(
        conn,
        service.OpportunityCreate.from_dict({
            "tenant_id": "pilot_tenant",
            "network_id": "example_network",
            "show_id": "private_pilot",
            "guest_name": "Synthetic Pilot Guest",
            "why_guest": "The synthetic guest can test a concrete private-pilot claim.",
            "why_now": "The synthetic format needs an executable readiness gate this week.",
            "proposed_artifact": "A one-page operating map",
            "relationship_class": "C2",
            "relationship_owner": "ari_owner",
            "social_cost_1_5": 2,
            "ari_effort": "review_only",
            "preferred_city": "Los Angeles",
            "next_action": "Approve, reject, or protect the candidate.",
            "source_provenance": "Synthetic private-pilot test fixture.",
        }),
        PRODUCER,
    )
    opportunity_id = created["id"]
    service.attach_thesis_contact(
        conn,
        opportunity_id,
        service.ThesisContactUpdate.from_dict({
            "episode_thesis": "Human authority should be represented as executable operational receipts.",
            "route_type": "producer_system",
            "route_label": "Opaque producer-owned route",
            "source_provenance": "Synthetic verified route fixture with no contact data.",
            "verified_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        }),
        PRODUCER,
    )
    service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "approve"}),
        HOST,
    )
    service.create_draft(
        conn,
        opportunity_id,
        service.DraftCreate.from_dict({"kind": "invitation"}),
        PRODUCER,
    )
    return opportunity_id


def preflight_assets() -> list[dict[str, str]]:
    return [
        {"kind": kind, "custody_target": f"production://fixture/{kind}"}
        for kind in sorted(service.REQUIRED_PREFLIGHT_ASSETS)
    ]


def advance_to_prep(conn) -> tuple[str, str]:
    opportunity_id = create_editorial_candidate(conn)
    service.record_receipt(
        conn,
        opportunity_id,
        receipt("outreach.sent", "mailbox://fixture/sent-001", minutes_ago=30),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "reply.classified",
            "mailbox://fixture/reply-classification-001",
            details={"classification": "POSITIVE_INTEREST"},
            minutes_ago=25,
        ),
        PRODUCER,
    )
    service.route_to_studio(
        conn,
        opportunity_id,
        service.StudioRoutingCreate.from_dict({
            "city": "Los Angeles",
            "studio_reference": "studio://fixture/la-room",
        }),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "booking.confirmed",
            "calendar://fixture/booking-001",
            details={
                "studio_ref": "studio://fixture/la-room",
                "producer_ref": "producer://fixture/existing-producer",
                "recording_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
            minutes_ago=20,
        ),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "consent.signed",
            "release://fixture/consent-001",
            details={"private_pilot": True, "clip_scope": "approved_clips"},
            minutes_ago=15,
        ),
        PRODUCER,
    )
    service.create_brief(
        conn,
        opportunity_id,
        service.BriefCreate.from_dict({
            "research_claims": [{
                "claim": "The synthetic receipt is present.",
                "evidence_source": "Synthetic source fixture",
                "verified": True,
            }],
            "segments": [{
                "title": "The Stress Test",
                "objective": "Test the limit of the receipt-driven lifecycle.",
            }],
        }),
        PRODUCER,
    )
    package = service.declare_assets(
        conn,
        opportunity_id,
        service.AssetPackageCreate.from_dict({"assets": preflight_assets()}),
        PRODUCER,
    )
    return opportunity_id, package["id"]


def test_complete_receipt_lifecycle_is_idempotent_and_never_publishes(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "pilot.sqlite3")
    opportunity_id, package_id = advance_to_prep(conn)

    sent = receipt("outreach.sent", "mailbox://fixture/sent-001", minutes_ago=30)
    duplicate = service.record_receipt(conn, opportunity_id, sent, PRODUCER)
    assert duplicate["external_reference"] == "mailbox://fixture/sent-001"
    assert store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM operational_receipts "
        "WHERE opportunity_id = ? AND receipt_type = 'outreach.sent'",
        (opportunity_id,),
    )["count"] == 1

    ready = service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "recording.ready",
            "production://fixture/ready-001",
            details={
                "preflight_ref": "preflight://fixture/playback-pass",
                "asset_package_id": package_id,
            },
            minutes_ago=10,
        ),
        PRODUCER,
    )
    assert ready["receipt_type"] == "recording.ready"
    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == "RECORDING_READY"

    rehearsal = service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "recording.completed",
            "production://fixture/rehearsal-001",
            details={"session_kind": "technical_rehearsal"},
            minutes_ago=8,
        ),
        PRODUCER,
    )
    assert rehearsal["details"]["session_kind"] == "technical_rehearsal"
    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == "RECORDING_READY"

    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "recording.completed",
            "production://fixture/guest-pilot-001",
            details={"session_kind": "guest_pilot"},
            minutes_ago=5,
        ),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "media.ingested",
            "media://fixture/ingest-001",
            details={
                "master_ref": "media://fixture/master-001",
                "checksum_ref": "checksum://fixture/sha256-001",
            },
            minutes_ago=1,
        ),
        PRODUCER,
    )
    detail = service.opportunity_detail(conn, opportunity_id, PRODUCER)
    assert detail["status"] == "RECORDED"
    assert {item["receipt_type"] for item in detail["receipts"]} == {
        item.value for item in service.ReceiptType
    }
    assert "PUBLISHED" not in {event["event_type"] for event in detail["audit_events"]}


@pytest.mark.parametrize("missing", ["route", "booking", "consent", "brief", "preflight"])
def test_recording_ready_fails_closed_when_evidence_is_missing(
    tmp_path: Path, missing: str
) -> None:
    conn = store.connect(tmp_path / f"missing-{missing}.sqlite3")
    opportunity_id, package_id = advance_to_prep(conn)
    details = {
        "preflight_ref": "preflight://fixture/playback-pass",
        "asset_package_id": package_id,
    }
    if missing == "route":
        conn.execute(
            "UPDATE contact_routes SET source_provenance = '' WHERE opportunity_id = ?",
            (opportunity_id,),
        )
    elif missing == "booking":
        conn.execute(
            "DELETE FROM operational_receipts WHERE opportunity_id = ? AND receipt_type = ?",
            (opportunity_id, "booking.confirmed"),
        )
    elif missing == "consent":
        conn.execute(
            "DELETE FROM operational_receipts WHERE opportunity_id = ? AND receipt_type = ?",
            (opportunity_id, "consent.signed"),
        )
    elif missing == "brief":
        conn.execute("DELETE FROM episode_briefs WHERE opportunity_id = ?", (opportunity_id,))
    else:
        details.pop("preflight_ref")
    conn.commit()

    with pytest.raises(service.DomainError) as caught:
        service.record_receipt(
            conn,
            opportunity_id,
            receipt(
                "recording.ready",
                f"production://fixture/missing-{missing}",
                details=details,
            ),
            PRODUCER,
        )
    assert caught.value.status_code in {409, 422}
    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == "PREP_IN_PROGRESS"


def test_receipts_enforce_tenant_role_type_and_opaque_reference(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "boundaries.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    payload = receipt("outreach.sent", "mailbox://fixture/sent-002")

    with pytest.raises(service.DomainError) as tenant_error:
        service.record_receipt(conn, opportunity_id, payload, OTHER_TENANT)
    assert tenant_error.value.status_code == 403

    with pytest.raises(service.DomainError) as role_error:
        service.record_receipt(conn, opportunity_id, payload, HOST)
    assert role_error.value.status_code == 403

    with pytest.raises(service.ValidationError):
        service.ReceiptCreate.from_dict({
            "receipt_type": "outreach.sent",
            "external_reference": "person@example.com",
            "occurred_at": datetime.now(UTC).isoformat(),
        })
    with pytest.raises(service.ValidationError):
        service.ReceiptCreate.from_dict({
            "receipt_type": "distribution.delivered",
            "external_reference": "distribution://fixture/nope",
            "occurred_at": datetime.now(UTC).isoformat(),
        })


def test_outreach_receipt_requires_human_reviewed_safe_metadata(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "reviewed-draft.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    conn.execute(
        "UPDATE correspondence_drafts SET status = 'DRAFT' WHERE opportunity_id = ?",
        (opportunity_id,),
    )
    conn.commit()
    payload = receipt("outreach.sent", "mailbox://fixture/reviewed-metadata")

    with pytest.raises(service.DomainError) as caught:
        service.record_receipt(conn, opportunity_id, payload, PRODUCER)

    assert caught.value.status_code == 409
    assert store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM operational_receipts WHERE opportunity_id = ?",
        (opportunity_id,),
    )["count"] == 0

    conn.execute(
        "UPDATE correspondence_drafts SET status = 'REVIEWED' WHERE opportunity_id = ?",
        (opportunity_id,),
    )
    conn.commit()
    recorded = service.record_receipt(conn, opportunity_id, payload, PRODUCER)
    assert recorded["receipt_type"] == "outreach.sent"


@pytest.mark.parametrize(
    ("receipt_type", "details"),
    [
        ("outreach.sent", {"correspondence_body": "Copied message"}),
        ("reply.classified", {"classification": "POSITIVE_INTEREST", "body": "Copied"}),
        ("reply.classified", {"classification": "NOT_CANONICAL"}),
        (
            "booking.confirmed",
            {
                "studio_ref": "studio://person@example.org/room",
                "producer_ref": "producer://fixture/owner",
                "recording_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        ),
        ("consent.signed", {"private_pilot": True, "clip_scope": "approved_clips", "signed_document": True}),
        (
            "recording.ready",
            {"preflight_ref": "preflight://fixture/pass", "asset_package_id": "not-a-uuid"},
        ),
        ("recording.completed", {"session_kind": "guest_pilot", "guest_name": "Private"}),
        ("media.ingested", {"master_ref": "media://fixture/master"}),
    ],
)
def test_receipt_details_use_exact_private_safe_schemas(
    receipt_type: str, details: dict
) -> None:
    with pytest.raises(service.ValidationError):
        service.ReceiptCreate.from_dict({
            "receipt_type": receipt_type,
            "external_reference": "owner://fixture/receipt",
            "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "details": details,
        })


def test_future_receipt_time_is_rejected_without_persistence(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "future-receipt.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    future = service.ReceiptCreate.from_dict({
        "receipt_type": "outreach.sent",
        "external_reference": "mailbox://fixture/future-sent",
        "occurred_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "details": {},
    })

    with pytest.raises(service.DomainError) as caught:
        service.record_receipt(conn, opportunity_id, future, PRODUCER)

    assert caught.value.status_code == 422
    assert store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM operational_receipts WHERE opportunity_id = ?",
        (opportunity_id,),
    )["count"] == 0


@pytest.mark.parametrize("classification", ["AMBIGUOUS", "TRAVEL_REQUEST"])
def test_sensitive_reply_classifications_pause_for_human_review(
    tmp_path: Path, classification: str
) -> None:
    conn = store.connect(tmp_path / f"reply-{classification}.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    service.record_receipt(
        conn,
        opportunity_id,
        receipt("outreach.sent", "mailbox://fixture/sent-human-review"),
        PRODUCER,
    )

    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "reply.classified",
            f"mailbox://fixture/reply-{classification.lower()}",
            details={"classification": classification},
        ),
        PRODUCER,
    )

    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == "NEEDS_HUMAN"


@pytest.mark.parametrize(
    ("classification", "expected_status"),
    [
        ("FOLLOW_UP_LATER", "REVISIT_LATER"),
        ("NEEDS_MORE_INFORMATION", "NEEDS_HUMAN"),
        ("CONTACT_PUBLICIST", "NEEDS_HUMAN"),
        ("CONTACT_ASSISTANT", "NEEDS_HUMAN"),
    ],
)
def test_operational_reply_classifications_leave_active_outreach_flow(
    tmp_path: Path, classification: str, expected_status: str
) -> None:
    conn = store.connect(tmp_path / f"reply-transition-{classification}.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    service.record_receipt(
        conn,
        opportunity_id,
        receipt("outreach.sent", f"mailbox://fixture/sent-{classification.lower()}"),
        PRODUCER,
    )

    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "reply.classified",
            f"mailbox://fixture/reply-{classification.lower()}",
            details={"classification": classification},
        ),
        PRODUCER,
    )

    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == expected_status


def test_early_studio_route_is_honored_when_interest_arrives(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "early-studio.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    service.route_to_studio(
        conn,
        opportunity_id,
        service.StudioRoutingCreate.from_dict({
            "city": "Los Angeles",
            "studio_reference": "studio://fixture/early-la-room",
        }),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt("outreach.sent", "mailbox://fixture/early-route-sent"),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "reply.classified",
            "mailbox://fixture/early-route-reply",
            details={"classification": "POSITIVE_INTEREST"},
        ),
        PRODUCER,
    )

    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == "SCHEDULING"
    booked = service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "booking.confirmed",
            "calendar://fixture/early-route-booking",
            details={
                "studio_ref": "studio://fixture/early-la-room",
                "producer_ref": "producer://fixture/existing-producer",
                "recording_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        ),
        PRODUCER,
    )
    assert booked["receipt_type"] == "booking.confirmed"


def test_brief_then_consent_enters_prep_without_duplicate_brief(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "brief-first.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    service.record_receipt(
        conn,
        opportunity_id,
        receipt("outreach.sent", "mailbox://fixture/brief-first-sent", minutes_ago=30),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "reply.classified",
            "mailbox://fixture/brief-first-reply",
            details={"classification": "POSITIVE_INTEREST"},
            minutes_ago=25,
        ),
        PRODUCER,
    )
    service.route_to_studio(
        conn,
        opportunity_id,
        service.StudioRoutingCreate.from_dict({
            "city": "Los Angeles",
            "studio_reference": "studio://fixture/brief-first-room",
        }),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "booking.confirmed",
            "calendar://fixture/brief-first-booking",
            details={
                "studio_ref": "studio://fixture/brief-first-room",
                "producer_ref": "producer://fixture/existing-producer",
                "recording_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
            minutes_ago=20,
        ),
        PRODUCER,
    )
    service.create_brief(
        conn,
        opportunity_id,
        service.BriefCreate.from_dict({
            "research_claims": [{
                "claim": "The synthetic receipt is present.",
                "evidence_source": "Synthetic source fixture",
                "verified": True,
            }],
            "segments": [{
                "title": "The Stress Test",
                "objective": "Test the limit of the receipt-driven lifecycle.",
            }],
        }),
        PRODUCER,
    )
    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == "BOOKED"

    service.record_receipt(
        conn,
        opportunity_id,
        receipt(
            "consent.signed",
            "release://fixture/brief-first-consent",
            details={"private_pilot": True, "clip_scope": "approved_clips"},
            minutes_ago=15,
        ),
        PRODUCER,
    )

    detail = service.opportunity_detail(conn, opportunity_id, PRODUCER)
    assert detail["status"] == "PREP_IN_PROGRESS"
    assert len(detail["briefs"]) == 1


def test_approval_queue_and_filters_expose_only_operational_fields(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "queue.sqlite3")
    opportunity_id = create_editorial_candidate(conn)
    # Return it to the decision queue without changing its fixture metadata.
    store.update(
        conn,
        "appearance_opportunities",
        opportunity_id,
        {"disposition": None, "status": "EDITORIAL_REVIEW"},
    )
    conn.commit()

    queue = service.approval_queue(conn, "pilot_tenant", owner="ari_owner")
    assert len(queue) == 1
    expected = {
        "thesis": "Human authority should be represented as executable operational receipts.",
        "relationship_owner": "ari_owner",
        "social_cost": 2,
        "ari_effort": "review_only",
        "city": "Los Angeles",
        "route_usable": True,
    }
    assert expected.items() <= queue[0].items()
    assert "body" not in queue[0]
    assert service.list_opportunities(
        conn, "pilot_tenant", state="EDITORIAL_REVIEW", owner="ari_owner"
    )[0]["id"] == opportunity_id
    assert service.list_opportunities(conn, "other_tenant") == []
