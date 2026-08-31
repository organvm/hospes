"""Service-layer tests — run WITHOUT the optional FastAPI extra.

These exercise the guest-operations domain logic (ported from PR #2's
``services.py``) directly against the stdlib sqlite store, so the human-gated
rules, tenant boundaries, and DRAFTS-never-SENDS discipline are covered even
when the HTTP surface is not installed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hospes import service, store
from hospes.drafts import DRAFT_BANNER

UTC = timezone.utc

PRODUCER = service.HumanActor("producer_fixture", service.HumanRole.PRODUCER, "fixture_tenant")
HOST = service.HumanActor("host_fixture", service.HumanRole.HOST, "fixture_tenant")
OWNER = service.HumanActor(
    "relationship_owner_fixture", service.HumanRole.RELATIONSHIP_OWNER, "fixture_tenant"
)
EDITOR = service.HumanActor(
    "editorial_owner_fixture", service.HumanRole.EDITORIAL_OWNER, "fixture_tenant"
)
OTHER_TENANT = service.HumanActor("intruder", service.HumanRole.PRODUCER, "another_tenant")


def opportunity_payload(relationship_class: str = "C2") -> dict:
    return {
        "tenant_id": "fixture_tenant",
        "network_id": "fixture_network",
        "show_id": "fixture_show",
        "guest_name": "Guest Example",
        "why_guest": "The synthetic guest can stress-test a concrete operating claim.",
        "why_now": "The synthetic project has reached a useful public decision point.",
        "proposed_artifact": "A one-page decision framework",
        "relationship_class": relationship_class,
    }


def create_and_prepare(conn, relationship_class: str = "C2") -> str:
    created = service.create_opportunity(
        conn, service.OpportunityCreate.from_dict(opportunity_payload(relationship_class)), PRODUCER
    )
    opportunity_id = created["id"]
    service.attach_thesis_contact(
        conn,
        opportunity_id,
        service.ThesisContactUpdate.from_dict({
            "episode_thesis": (
                "Small teams should treat relationship permission as infrastructure, not etiquette."
            ),
            "route_type": "publicist_form",
            "route_label": "Public booking form",
            "source_provenance": "Synthetic fixture from an official public booking page.",
            "verified_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        }),
        PRODUCER,
    )
    return opportunity_id


def preflight_assets() -> list[dict[str, str]]:
    return [
        {"kind": kind, "custody_target": f"production://preflight/{kind}"}
        for kind in sorted(service.REQUIRED_PREFLIGHT_ASSETS)
    ]


def test_store_roundtrip_json_and_bool(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "roundtrip.sqlite3"))
    now = datetime.now(UTC).isoformat()
    store.insert(conn, "appearance_opportunities", {
        "id": "opp1", "tenant_id": "t", "network_id": "n", "show_id": "s",
        "guest_name": "G", "why_guest": "x", "why_now": "y", "proposed_artifact": "z",
        "relationship_class": "C1", "status": "DISCOVERED", "disposition": None,
        "episode_thesis": None, "created_at": now, "updated_at": now,
    })
    store.insert(conn, "contact_routes", {
        "id": "r1", "tenant_id": "t", "opportunity_id": "opp1", "route_type": "form",
        "route_label": "Form", "source_provenance": "prov", "verified_at": now,
        "usable": True, "created_at": now,
    })
    conn.commit()
    route = store.fetch_one(conn, "SELECT * FROM contact_routes WHERE id = 'r1'")
    assert route["usable"] is True
    store.insert(conn, "episode_briefs", {
        "id": "b1", "tenant_id": "t", "opportunity_id": "opp1", "thesis": "th",
        "research_claims": [{"claim": "c", "verified": True}], "segments": [{"title": "s"}],
        "created_by": "u", "created_at": now,
    })
    conn.commit()
    brief = store.fetch_one(conn, "SELECT * FROM episode_briefs WHERE id = 'b1'")
    assert brief["research_claims"] == [{"claim": "c", "verified": True}]
    assert brief["segments"] == [{"title": "s"}]


def test_db_path_override(tmp_path, monkeypatch) -> None:
    target = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("HOSPES_DB", str(target))
    assert service.store.resolve_db_path() == target
    conn = store.connect()
    assert target.exists()
    conn.close()


def test_complete_draft_only_vertical_slice_is_persistent(tmp_path: Path) -> None:
    database = str(tmp_path / "hospes.sqlite3")
    conn = store.connect(database)
    opportunity_id = create_and_prepare(conn)

    approved = service.record_decision(
        conn, opportunity_id, service.DecisionCreate.from_dict({"action": "approve"}), HOST
    )
    assert approved["disposition"] == "APPROVED"
    assert approved["status"] == "APPROVED"

    service.record_decision(
        conn, opportunity_id,
        service.DecisionCreate.from_dict({"action": "note", "note": "Keep it practical."}), PRODUCER
    )
    detail = service.opportunity_detail(conn, opportunity_id, PRODUCER)
    assert len(detail["decisions"]) == 2

    draft = service.create_draft(
        conn, opportunity_id, service.DraftCreate.from_dict({"kind": "invitation"}), PRODUCER
    )
    assert draft["status"] == "REVIEWED"
    assert draft["persisted_body"] is False
    assert DRAFT_BANNER in draft["body"]
    persisted_draft = store.fetch_one(
        conn, "SELECT * FROM correspondence_drafts WHERE id = ?", (draft["id"],)
    )
    assert persisted_draft["subject"] == "template:invitation"
    assert persisted_draft["body"] == (
        "[CORRESPONDENCE BODY NOT PERSISTED — EXTERNAL OWNER REQUIRED]"
    )
    assert draft["body"] != persisted_draft["body"]

    routed = service.route_to_studio(
        conn, opportunity_id,
        service.StudioRoutingCreate.from_dict({
            "city": "Austin", "studio_reference": "studio://fixture/austin-room"
        }),
        PRODUCER,
    )
    assert routed["city"] == "Austin"

    brief = service.create_brief(
        conn, opportunity_id,
        service.BriefCreate.from_dict({
            "research_claims": [
                {"claim": "Every approval is recorded.", "evidence_source": "record", "verified": True}
            ],
            "segments": [{"title": "The Stress Test", "objective": "Find the limit of the claim."}],
        }),
        PRODUCER,
    )
    assert brief["segments"][0]["title"] == "The Stress Test"

    assets = service.declare_assets(
        conn, opportunity_id,
        service.AssetPackageCreate.from_dict({
            "assets": preflight_assets()
        }),
        PRODUCER,
    )
    assert assets["status"] == "DECLARED"

    due_at = datetime.now(UTC) + timedelta(days=1)
    commitment = service.create_commitment(
        conn, opportunity_id,
        service.CommitmentCreate.from_dict({
            "summary": "Confirm the segment brief.", "owner_role": "producer",
            "due_at": due_at.isoformat(),
        }),
        PRODUCER,
    )
    commitment_id = commitment["id"]

    followups = service.list_followups(conn, "fixture_tenant", due_at + timedelta(minutes=1))
    assert [c["id"] for c in followups] == [commitment_id]

    completed = service.complete_commitment(conn, commitment_id, PRODUCER)
    assert completed["status"] == "COMPLETED"
    conn.close()

    # Persistence: reopen the same file, records survive.
    reopened = store.connect(database)
    detail = service.opportunity_detail(reopened, opportunity_id, PRODUCER)
    assert detail["asset_packages"][0]["status"] == "DECLARED"
    assert detail["commitments"][0]["status"] == "COMPLETED"
    assert {e["event_type"] for e in detail["audit_events"]} >= {
        "appearance.candidate_created", "appearance.approved",
        "outreach.draft_ready", "recording.preflight_assets_declared", "commitment.completed",
    }
    assert {event["show_id"] for event in detail["audit_events"]} == {"fixture_show"}
    reopened.close()


def test_human_boundary_and_no_send_path(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "boundary.sqlite3"))
    # The service module exposes no send/deliver operation.
    assert not any("send" in name or "deliver" in name for name in dir(service))

    with pytest.raises(service.DomainError) as exc:
        service.create_opportunity(
            conn, service.OpportunityCreate.from_dict(opportunity_payload()), OTHER_TENANT
        )
    assert exc.value.status_code == 403

    created = service.create_opportunity(
        conn, service.OpportunityCreate.from_dict(opportunity_payload()), PRODUCER
    )
    with pytest.raises(service.DomainError) as read_exc:
        service.get_opportunity(conn, created["id"], OTHER_TENANT)
    assert read_exc.value.status_code == 403


def test_protected_relationships_require_the_relationship_owner(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "protected.sqlite3"))
    opportunity_id = create_and_prepare(conn, relationship_class="C5")

    with pytest.raises(service.DomainError) as host_exc:
        service.record_decision(
            conn, opportunity_id, service.DecisionCreate.from_dict({"action": "approve"}), HOST
        )
    assert host_exc.value.status_code == 403

    with pytest.raises(service.DomainError) as prod_exc:
        service.record_decision(
            conn, opportunity_id, service.DecisionCreate.from_dict({"action": "protect"}), PRODUCER
        )
    assert prod_exc.value.status_code == 403

    protected = service.record_decision(
        conn, opportunity_id,
        service.DecisionCreate.from_dict({"action": "protect", "note": "Owner protection."}), OWNER
    )
    assert protected["disposition"] == "PROTECTED"
    assert protected["contact_route"]["usable"] is False

    with pytest.raises(service.DomainError) as draft_exc:
        service.create_draft(
            conn, opportunity_id, service.DraftCreate.from_dict({"kind": "invitation"}), PRODUCER
        )
    assert draft_exc.value.status_code == 403


@pytest.mark.parametrize("relationship_class", ["C4", "C5"])
def test_every_protected_class_draft_attempt_is_403_and_write_free(
    tmp_path: Path, relationship_class: str
) -> None:
    conn = store.connect(tmp_path / f"draft-{relationship_class}.sqlite3")
    opportunity_id = create_and_prepare(conn, relationship_class=relationship_class)
    service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "approve"}),
        OWNER,
    )

    for actor in (PRODUCER, EDITOR, OWNER, HOST):
        with pytest.raises(service.DomainError) as caught:
            service.create_draft(
                conn,
                opportunity_id,
                service.DraftCreate.from_dict({"kind": "invitation"}),
                actor,
            )
        assert caught.value.status_code == 403

    assert store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM correspondence_drafts WHERE opportunity_id = ?",
        (opportunity_id,),
    )["count"] == 0


@pytest.mark.parametrize(
    "status",
    [
        "OUTREACH_SENT",
        "FOLLOW_UP_DUE",
        "DECLINED",
        "REVISIT_LATER",
        "DUPLICATE",
        "DO_NOT_CONTACT",
        "CANCELLED",
    ],
)
def test_draft_preview_rejects_non_pre_outreach_states_without_write(
    tmp_path: Path, status: str
) -> None:
    conn = store.connect(tmp_path / f"preview-{status.lower()}.sqlite3")
    opportunity_id = create_and_prepare(conn)
    service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "approve"}),
        HOST,
    )
    conn.execute(
        "UPDATE appearance_opportunities SET status = ? WHERE id = ?",
        (status, opportunity_id),
    )
    conn.commit()
    before_events = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE opportunity_id = ?", (opportunity_id,)
    ).fetchone()[0]

    with pytest.raises(service.DomainError) as caught:
        service.preview_draft(
            conn,
            opportunity_id,
            service.DraftCreate.from_dict({"kind": "invitation"}),
            PRODUCER,
        )

    assert caught.value.status_code == 409
    assert conn.execute(
        "SELECT COUNT(*) FROM correspondence_drafts WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE opportunity_id = ?", (opportunity_id,)
    ).fetchone()[0] == before_events


def test_reviewed_draft_can_be_revised_from_outreach_approved(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "approved-redraft.sqlite3")
    opportunity_id = create_and_prepare(conn)
    service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "approve"}),
        HOST,
    )
    conn.execute(
        "UPDATE appearance_opportunities SET status = 'OUTREACH_APPROVED' WHERE id = ?",
        (opportunity_id,),
    )
    conn.commit()

    reviewed = service.create_draft(
        conn,
        opportunity_id,
        service.DraftCreate.from_dict({"kind": "invitation"}),
        PRODUCER,
    )

    assert reviewed["status"] == "REVIEWED"
    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["status"] == (
        "OUTREACH_DRAFTED"
    )


def test_invalid_decision_transition_writes_no_decision_or_route_change(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "atomic-decision.sqlite3")
    opportunity_id = create_and_prepare(conn)
    service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "approve"}),
        HOST,
    )

    with pytest.raises(service.DomainError) as caught:
        service.record_decision(
            conn,
            opportunity_id,
            service.DecisionCreate.from_dict({"action": "protect"}),
            OWNER,
        )

    assert caught.value.status_code == 409
    assert store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM decisions WHERE opportunity_id = ?",
        (opportunity_id,),
    )["count"] == 1
    assert service.opportunity_detail(conn, opportunity_id, OWNER)["contact_route"]["usable"] is True


def test_unprepared_candidates_are_not_actionable_in_the_approval_queue(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "unprepared-queue.sqlite3")
    created = service.create_opportunity(
        conn, service.OpportunityCreate.from_dict(opportunity_payload()), PRODUCER
    )

    assert service.approval_queue(conn, "fixture_tenant") == []
    with pytest.raises(service.DomainError) as caught:
        service.record_decision(
            conn,
            created["id"],
            service.DecisionCreate.from_dict({"action": "approve"}),
            HOST,
        )
    assert caught.value.status_code == 409
    assert store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM decisions WHERE opportunity_id = ?",
        (created["id"],),
    )["count"] == 0


@pytest.mark.parametrize(
    ("field_name", "private_value"),
    [
        ("why_guest", "Reach the synthetic guest at person@example.org for details."),
        ("next_action", "Call 310-555-0199 after approval."),
    ],
)
def test_live_candidate_admission_rejects_contact_content(
    field_name: str, private_value: str
) -> None:
    with pytest.raises(service.ValidationError) as caught:
        service.OpportunityCreate.from_dict({
            **opportunity_payload(),
            field_name: private_value,
        })
    assert "contact data" in caught.value.detail


def test_validation_gates(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "validation.sqlite3"))
    opportunity_id = create_and_prepare(conn)

    # A note action with no text is rejected at the domain layer.
    with pytest.raises(service.DomainError) as note_exc:
        service.record_decision(
            conn, opportunity_id, service.DecisionCreate.from_dict({"action": "note"}), PRODUCER
        )
    assert note_exc.value.status_code == 422

    service.record_decision(
        conn, opportunity_id, service.DecisionCreate.from_dict({"action": "approve"}), HOST
    )

    # Unverified research claims cannot enter a brief.
    with pytest.raises(service.DomainError) as brief_exc:
        service.create_brief(
            conn, opportunity_id,
            service.BriefCreate.from_dict({
                "research_claims": [
                    {"claim": "Unverified fixture claim.", "evidence_source": "source-x", "verified": False}
                ],
                "segments": [{"title": "Receipts", "objective": "Unverified claims stay out."}],
            }),
            PRODUCER,
        )
    assert brief_exc.value.status_code == 422

    # A future verified_at is rejected on thesis-contact.
    with pytest.raises(service.DomainError) as future_exc:
        service.attach_thesis_contact(
            conn, opportunity_id,
            service.ThesisContactUpdate.from_dict({
                "episode_thesis": "A future-dated verification must be rejected outright.",
                "route_type": "publicist_form", "route_label": "Form",
                "source_provenance": "Synthetic provenance string.",
                "verified_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            }),
            PRODUCER,
        )
    assert future_exc.value.status_code == 422

    # A bad payload (too-short guest name) fails validation before any write.
    with pytest.raises(service.ValidationError):
        service.OpportunityCreate.from_dict({**opportunity_payload(), "guest_name": "x"})


def test_asset_package_requires_brief_and_full_recording_set(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "assets.sqlite3"))
    opportunity_id = create_and_prepare(conn)
    service.record_decision(
        conn, opportunity_id, service.DecisionCreate.from_dict({"action": "approve"}), HOST
    )
    # No brief yet -> assets refused.
    with pytest.raises(service.DomainError) as no_brief:
        service.declare_assets(
            conn, opportunity_id,
            service.AssetPackageCreate.from_dict({
                "assets": [{"kind": "room_tone", "custody_target": "media://a"}]
            }),
            PRODUCER,
        )
    assert no_brief.value.status_code == 409

    service.create_brief(
        conn, opportunity_id,
        service.BriefCreate.from_dict({
            "research_claims": [{"claim": "Verified.", "evidence_source": "source-x", "verified": True}],
            "segments": [{"title": "Seg", "objective": "A concrete objective here."}],
        }),
        PRODUCER,
    )
    # Incomplete recording set -> 422 naming the missing kinds.
    with pytest.raises(service.DomainError) as missing:
        service.declare_assets(
            conn, opportunity_id,
            service.AssetPackageCreate.from_dict({
                "assets": [{"kind": "room_tone", "custody_target": "media://a"}]
            }),
            PRODUCER,
        )
    assert missing.value.status_code == 422
    assert "backup_recorder" in missing.value.detail
