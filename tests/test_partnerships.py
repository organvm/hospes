"""Reusable partnership command-center import and update contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from hospes import partnerships, platform, service, store
from hospes.__main__ import main

TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "partnerships" / "example-partnership-private-pilot.yaml"
SECOND_TEMPLATE = TEMPLATE.with_name("synthetic-studio-partnership.yaml")


def import_ari(conn, tenant: str = "private_pilot") -> partnerships.PartnershipImportResult:
    return partnerships.import_template(
        conn,
        TEMPLATE,
        tenant_id=tenant,
        actor_id="example_operator",
        actor_role="producer",
        show_id="private_pilot",
    )


def test_template_builds_complete_command_center_and_is_idempotent(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "partnership.sqlite3")
    first = import_ari(conn)
    conn.commit()
    center = partnerships.command_center(conn, first.partnership_id, "private_pilot")

    assert first.created == center["summary"]["total"] == 22
    assert center["partnership"]["label"] == "Host + Producer"
    assert center["summary"]["unknown"] == 4
    assert center["categories"]["engine"][0]["title"]
    assert center["categories"]["deal"][0]["external_reference"].startswith("registry://")
    assert center["categories"]["agreement"][0]["state"] == "agreed"
    assert all("contract_body" not in item for items in center["categories"].values() for item in items)

    second = import_ari(conn)
    conn.commit()
    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == first.created
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM partnership_items")["count"] == first.created
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM partnership_template_imports")["count"] == 1
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM partnership_item_revisions")["count"] == first.created
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM partnership_audit_events")["count"] == 23


def test_changed_template_conflicts_without_overwriting_live_state(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "template-conflict.sqlite3")
    imported = import_ari(conn)
    conn.commit()
    original = store.fetch_one(
        conn,
        "SELECT * FROM partnership_items WHERE partnership_id = ? AND item_key = ?",
        (imported.partnership_id, "plan.pilot_1"),
    )
    changed = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    changed["items"][4]["summary"] = "A changed template must never replace the live pilot decision."
    changed_path = tmp_path / "changed.yaml"
    changed_path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")

    with pytest.raises(partnerships.PartnershipError) as caught:
        partnerships.import_template(
            conn,
            changed_path,
            tenant_id="private_pilot",
            actor_id="example_operator",
            actor_role="producer",
            show_id="private_pilot",
        )
    assert caught.value.status_code == 409
    current = store.fetch_one(
        conn,
        "SELECT * FROM partnership_items WHERE partnership_id = ? AND item_key = ?",
        (imported.partnership_id, "plan.pilot_1"),
    )
    assert current["summary"] == original["summary"]
    assert current["revision"] == 1


def test_partnerships_are_tenant_isolated(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "tenants.sqlite3")
    first = import_ari(conn, "private_pilot")
    second = import_ari(conn, "another_partnership")
    conn.commit()

    assert first.partnership_id != second.partnership_id
    assert partnerships.list_partnerships(conn, "private_pilot")[0]["id"] == first.partnership_id
    with pytest.raises(partnerships.PartnershipError) as caught:
        partnerships.command_center(conn, first.partnership_id, "another_partnership")
    assert caught.value.status_code == 404


def test_live_item_upsert_records_audit_and_requires_external_deal_owner(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "updates.sqlite3")
    imported = import_ari(conn)
    conn.commit()

    payload = partnerships.PartnershipItemInput.from_dict(
        {
            "item_key": "decision.recording_window",
            "category": "decision",
            "title": "Recurring recording window",
            "summary": "The partners confirmed one recurring window in the external calendar owner.",
            "owner": "Host",
            "state": "agreed",
            "external_reference": "calendar://example-partnership/recurring-window",
        }
    )
    created = partnerships.upsert_item(
        conn,
        imported.partnership_id,
        payload,
        tenant_id="private_pilot",
        actor_id="example_operator",
        actor_role="producer",
    )
    assert created["state"] == "agreed"
    assert (
        partnerships.command_center(conn, imported.partnership_id, "private_pilot")["summary"]["total"]
        == imported.created + 1
    )
    assert (
        store.fetch_one(
            conn,
            "SELECT COUNT(*) AS count FROM partnership_audit_events "
            "WHERE partnership_id = ? AND event_type = 'partnership.item_created'",
            (imported.partnership_id,),
        )["count"]
        == imported.created + 1
    )

    with pytest.raises(partnerships.PartnershipError):
        partnerships.PartnershipItemInput.from_dict(
            {
                "item_key": "deal.no_owner",
                "category": "deal",
                "title": "Unowned terms",
                "summary": "A deal cannot float without a canonical owner reference.",
                "owner": "Partners",
                "state": "unknown",
            }
        )


def test_revision_checked_updates_and_supersession_keep_append_only_history(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "revisions.sqlite3")
    imported = import_ari(conn)
    conn.commit()
    first_input = partnerships.PartnershipItemInput.from_dict(
        {
            "item_key": "decision.fixture_revision",
            "category": "decision",
            "title": "Initial fixture decision",
            "summary": "The first bounded version remains in immutable history.",
            "owner": "Partners",
            "state": "planned",
        }
    )
    created = partnerships.create_item(
        conn,
        imported.partnership_id,
        first_input,
        tenant_id="private_pilot",
        actor_id="example_operator",
        actor_role="producer",
    )
    revised_input = partnerships.PartnershipItemInput.from_dict(
        {
            **first_input.__dict__,
            "title": "Revised fixture decision",
            "state": "agreed",
        }
    )
    revised = partnerships.update_item(
        conn,
        imported.partnership_id,
        created["id"],
        revised_input,
        expected_revision=1,
        tenant_id="private_pilot",
        actor_id="example_operator",
        actor_role="producer",
    )
    assert revised["revision"] == 2
    with pytest.raises(partnerships.PartnershipError) as stale:
        partnerships.update_item(
            conn,
            imported.partnership_id,
            created["id"],
            revised_input,
            expected_revision=1,
            tenant_id="private_pilot",
            actor_id="example_operator",
            actor_role="producer",
        )
    assert stale.value.status_code == 409

    successor = partnerships.create_item(
        conn,
        imported.partnership_id,
        partnerships.PartnershipItemInput.from_dict(
            {
                "item_key": "decision.fixture_successor",
                "category": "decision",
                "title": "Successor fixture decision",
                "summary": "The successor replaces the active meaning without deleting history.",
                "owner": "Partners",
                "state": "current",
            }
        ),
        tenant_id="private_pilot",
        actor_id="example_operator",
        actor_role="producer",
    )
    superseded = partnerships.supersede_item(
        conn,
        imported.partnership_id,
        created["id"],
        expected_revision=2,
        successor_item_id=successor["id"],
        tenant_id="private_pilot",
        actor_id="example_operator",
        actor_role="producer",
    )
    assert superseded["state"] == "superseded"
    assert superseded["revision"] == 3
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM partnership_item_revisions WHERE item_id = ?",
            (created["id"],),
        ).fetchone()[0]
        == 3
    )
    historical = partnerships.command_center(conn, imported.partnership_id, "private_pilot")["categories"]["decision"]
    assert any(item["id"] == created["id"] and item["state"] == "superseded" for item in historical)

    with pytest.raises(partnerships.PartnershipError) as direct_supersession:
        partnerships.PartnershipItemInput.from_dict(
            {
                **revised_input.__dict__,
                "state": "superseded",
            }
        )
    assert direct_supersession.value.status_code == 422


def test_resources_reviews_candidate_slate_and_second_template_are_reusable(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "reusable.sqlite3")
    imported = import_ari(conn)
    second = partnerships.import_template(
        conn,
        SECOND_TEMPLATE,
        tenant_id="synthetic_tenant",
        actor_id="fixture_operator",
        actor_role="producer",
        show_id="private_pilot",
    )
    conn.commit()
    assert partnerships.command_center(conn, second.partnership_id, "synthetic_tenant")["coverage"] == {
        "covered": 10,
        "total": 10,
        "covered_categories": list(partnerships.CATEGORIES),
        "missing_categories": [],
    }

    linked = partnerships.link_resource(
        conn,
        imported.partnership_id,
        {"resource_type": "issue", "resource_reference": "github://organvm/hospes/issues/9", "label": "Pilot owner"},
        tenant_id="private_pilot",
        actor_id="example_operator",
        actor_role="producer",
    )
    assert linked["resource_type"] == "issue"
    review = partnerships.record_review(
        conn,
        imported.partnership_id,
        {
            "review_kind": "ari_review",
            "decisions_count": 3,
            "coverage_met": 10,
            "coverage_total": 10,
            "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
        tenant_id="private_pilot",
        actor_id="ari_owner",
        actor_role="relationship_owner",
    )
    assert review["review_kind"] == "ari_review"

    now = datetime.now(UTC).isoformat()
    for slot in (1, 2, 3):
        opportunity_id = f"00000000-0000-4000-8000-00000000000{slot}"
        store.insert(
            conn,
            "appearance_opportunities",
            {
                "id": opportunity_id,
                "tenant_id": "private_pilot",
                "network_id": "example_network",
                "show_id": "private_pilot",
                "guest_name": f"Synthetic Guest {slot}",
                "why_guest": "A synthetic candidate proves the reusable slate contract.",
                "why_now": "The cockpit needs ordered pilot candidates.",
                "proposed_artifact": "A synthetic artifact",
                "relationship_class": "C2",
                "status": "APPROVED",
                "disposition": "APPROVED",
                "episode_thesis": "A bounded thesis.",
                "created_at": now,
                "updated_at": now,
            },
        )
        store.insert(
            conn,
            "contact_routes",
            {
                "id": f"route-{slot}",
                "tenant_id": "private_pilot",
                "opportunity_id": opportunity_id,
                "route_type": "public_form",
                "route_label": "Opaque route",
                "source_provenance": "Synthetic route provenance.",
                "verified_at": now,
                "usable": True,
                "created_at": now,
            },
        )
        partnerships.select_pilot_candidate(
            conn,
            imported.partnership_id,
            opportunity_id,
            slot,
            tenant_id="private_pilot",
            actor_id="ari_owner",
            actor_role="relationship_owner",
        )
    center = partnerships.command_center(conn, imported.partnership_id, "private_pilot")
    assert [candidate["slot_label"] for candidate in center["candidate_slate"]] == ["primary", "backup_1", "backup_2"]
    readiness = center["pilot_readiness"]
    assert [check["key"] for check in readiness["checks"]] == [
        "ari_review",
        "candidate_slate",
        "verified_routes",
        "recurring_window",
        "outreach",
        "booking",
        "consent",
        "brief",
        "assets",
        "preflight",
        "technical_rehearsal",
        "guest_recording",
        "media",
        "scorecard",
    ]
    assert readiness["total"] == 14
    assert all(readiness["checks"][index]["met"] for index in range(3))

    primary_id = "00000000-0000-4000-8000-000000000001"
    backup_id = "00000000-0000-4000-8000-000000000002"
    required_assets = sorted(service.REQUIRED_PREFLIGHT_ASSETS)
    for package_id, opportunity_id, kinds in (
        ("package-primary-a", primary_id, required_assets[:3]),
        ("package-primary-b", primary_id, required_assets[3:]),
        ("package-backup-complete", backup_id, required_assets),
    ):
        store.insert(
            conn,
            "asset_packages",
            {
                "id": package_id,
                "tenant_id": "private_pilot",
                "opportunity_id": opportunity_id,
                "assets": [{"kind": kind, "custody_target": f"production://fixture/{kind}"} for kind in kinds],
                "status": "DECLARED",
                "declared_by": "producer_fixture",
                "created_at": now,
            },
        )
    conn.commit()
    readiness = partnerships.command_center(conn, imported.partnership_id, "private_pilot")["pilot_readiness"]
    by_key = {check["key"]: check["met"] for check in readiness["checks"]}
    assert by_key["assets"] is False
    assert readiness["complete_primary_asset_package_id"] is None

    planned_window = partnerships.create_item(
        conn,
        imported.partnership_id,
        partnerships.PartnershipItemInput.from_dict(
            {
                "item_key": "decision.fixture_recording_window",
                "category": "decision",
                "title": "Fixture recurring recording window",
                "summary": "The external calendar owner holds the bounded recurring window.",
                "owner": "Partners",
                "state": "planned",
                "external_reference": "calendar://fixture/recurring-window",
            }
        ),
        tenant_id="private_pilot",
        actor_id="producer_fixture",
        actor_role="producer",
    )
    readiness = partnerships.command_center(conn, imported.partnership_id, "private_pilot")["pilot_readiness"]
    assert {check["key"]: check["met"] for check in readiness["checks"]}["recurring_window"] is False
    partnerships.update_item(
        conn,
        imported.partnership_id,
        planned_window["id"],
        partnerships.PartnershipItemInput.from_dict(
            {
                "item_key": "decision.fixture_recording_window",
                "category": "decision",
                "title": "Fixture recurring recording window",
                "summary": "The external calendar owner holds the bounded recurring window.",
                "owner": "Partners",
                "state": "agreed",
                "external_reference": "calendar://fixture/recurring-window",
            }
        ),
        expected_revision=1,
        tenant_id="private_pilot",
        actor_id="producer_fixture",
        actor_role="producer",
    )
    store.insert(
        conn,
        "asset_packages",
        {
            "id": "package-primary-complete",
            "tenant_id": "private_pilot",
            "opportunity_id": primary_id,
            "assets": [
                {"kind": kind, "custody_target": f"production://fixture/complete/{kind}"} for kind in required_assets
            ],
            "status": "DECLARED",
            "declared_by": "producer_fixture",
            "created_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        },
    )
    conn.commit()
    readiness = partnerships.command_center(conn, imported.partnership_id, "private_pilot")["pilot_readiness"]
    by_key = {check["key"]: check["met"] for check in readiness["checks"]}
    assert by_key["recurring_window"] is True
    assert by_key["assets"] is True
    assert readiness["complete_primary_asset_package_id"] == "package-primary-complete"
    assert readiness["ready_for_recording"] is False
    assert readiness["guest_recording_allowed"] is False
    assert readiness["pilot_complete"] is False

    store.insert(
        conn,
        "episode_briefs",
        {
            "id": "brief-primary-complete",
            "tenant_id": "private_pilot",
            "opportunity_id": primary_id,
            "thesis": "A complete synthetic brief proves the projection gate.",
            "research_claims": [{"claim": "Fixture evidence exists.", "verified": True}],
            "segments": [{"title": "Fixture segment"}],
            "created_by": "producer_fixture",
            "created_at": now,
        },
    )
    for receipt_id, receipt_type, details in (
        ("receipt-outreach", "outreach.sent", {}),
        ("receipt-booking", "booking.confirmed", {}),
        ("receipt-consent", "consent.signed", {}),
        (
            "receipt-ready",
            "recording.ready",
            {
                "preflight_ref": "preflight://fixture/pass",
                "asset_package_id": "package-primary-complete",
            },
        ),
    ):
        store.insert(
            conn,
            "operational_receipts",
            {
                "id": receipt_id,
                "tenant_id": "private_pilot",
                "opportunity_id": primary_id,
                "receipt_type": receipt_type,
                "external_reference": f"registry://fixture/{receipt_id}",
                "occurred_at": now,
                "actor_id": "producer_fixture",
                "actor_role": "producer",
                "details": details,
                "created_at": now,
            },
        )
    partnerships.record_review(
        conn,
        imported.partnership_id,
        {
            "review_kind": "technical_rehearsal",
            "decisions_count": 1,
            "coverage_met": 1,
            "coverage_total": 1,
            "external_reference": "production://fixture/technical-rehearsal",
            "occurred_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
        tenant_id="private_pilot",
        actor_id="producer_fixture",
        actor_role="producer",
    )
    readiness = partnerships.command_center(conn, imported.partnership_id, "private_pilot")["pilot_readiness"]
    assert readiness["ready_for_recording"] is True
    assert readiness["guest_recording_allowed"] is True
    assert readiness["pilot_complete"] is False

    for receipt_id, receipt_type, details in (
        (
            "receipt-guest-recording",
            "recording.completed",
            {"session_kind": "guest_pilot"},
        ),
        (
            "receipt-media",
            "media.ingested",
            {
                "master_ref": "media://fixture/master",
                "checksum_ref": "checksum://fixture/sha256",
            },
        ),
    ):
        store.insert(
            conn,
            "operational_receipts",
            {
                "id": receipt_id,
                "tenant_id": "private_pilot",
                "opportunity_id": primary_id,
                "receipt_type": receipt_type,
                "external_reference": f"registry://fixture/{receipt_id}",
                "occurred_at": now,
                "actor_id": "producer_fixture",
                "actor_role": "producer",
                "details": details,
                "created_at": now,
            },
        )
    partnerships.record_review(
        conn,
        imported.partnership_id,
        {
            "review_kind": "pilot_scorecard",
            "decisions_count": 1,
            "coverage_met": 14,
            "coverage_total": 14,
            "external_reference": "scorecard://fixture/pilot-1",
            "occurred_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
        tenant_id="private_pilot",
        actor_id="producer_fixture",
        actor_role="producer",
    )
    readiness = partnerships.command_center(conn, imported.partnership_id, "private_pilot")["pilot_readiness"]
    assert readiness["met"] == readiness["total"] == 14
    assert readiness["ready_for_recording"] is True
    assert readiness["guest_recording_allowed"] is True
    assert readiness["pilot_complete"] is True
    with pytest.raises(partnerships.PartnershipError):
        partnerships.PartnershipItemInput.from_dict(
            {
                "item_key": "agreement.private_text",
                "category": "agreement",
                "title": "Private contract text",
                "summary": "The summary remains bounded.",
                "owner": "Partners",
                "state": "agreed",
                "external_reference": "registry://fixture/agreement",
                "contract_body": "synthetic private terms",
            }
        )


def test_ari_review_requires_relationship_owner_without_writes(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "ari-review-authority.sqlite3")
    imported = import_ari(conn)
    before_reviews = conn.execute(
        "SELECT COUNT(*) FROM partnership_reviews WHERE partnership_id = ?",
        (imported.partnership_id,),
    ).fetchone()[0]
    before_audits = conn.execute(
        "SELECT COUNT(*) FROM partnership_audit_events WHERE partnership_id = ?",
        (imported.partnership_id,),
    ).fetchone()[0]

    with pytest.raises(partnerships.PartnershipError) as caught:
        partnerships.record_review(
            conn,
            imported.partnership_id,
            {
                "review_kind": "ari_review",
                "decisions_count": 3,
                "coverage_met": 10,
                "coverage_total": 10,
                "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            },
            tenant_id="private_pilot",
            actor_id="example_operator",
            actor_role="producer",
        )

    assert caught.value.status_code == 403
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM partnership_reviews WHERE partnership_id = ?",
            (imported.partnership_id,),
        ).fetchone()[0]
        == before_reviews
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM partnership_audit_events WHERE partnership_id = ?",
            (imported.partnership_id,),
        ).fetchone()[0]
        == before_audits
    )


@pytest.mark.parametrize("relationship_class", ["C4", "C5"])
def test_protected_classes_cannot_mutate_the_pilot_slate_or_audit(tmp_path: Path, relationship_class: str) -> None:
    conn = store.connect(tmp_path / f"protected-slate-{relationship_class}.sqlite3")
    imported = import_ari(conn)
    now = datetime.now(UTC).isoformat()
    for opportunity_id, candidate_class in (
        ("00000000-0000-4000-8000-000000000010", "C2"),
        ("00000000-0000-4000-8000-000000000011", relationship_class),
    ):
        store.insert(
            conn,
            "appearance_opportunities",
            {
                "id": opportunity_id,
                "tenant_id": "private_pilot",
                "network_id": "example_network",
                "show_id": "private_pilot",
                "guest_name": f"Synthetic {candidate_class} Guest",
                "why_guest": "The synthetic guest proves the pre-mutation relationship guard.",
                "why_now": "The slate needs an atomic protected-class boundary.",
                "proposed_artifact": "A guarded slate",
                "relationship_class": candidate_class,
                "status": "APPROVED",
                "disposition": "APPROVED",
                "episode_thesis": "Protected candidates never enter this pilot slate.",
                "created_at": now,
                "updated_at": now,
            },
        )
    partnerships.select_pilot_candidate(
        conn,
        imported.partnership_id,
        "00000000-0000-4000-8000-000000000010",
        1,
        tenant_id="private_pilot",
        actor_id="ari_owner",
        actor_role="relationship_owner",
    )
    before_audits = conn.execute(
        "SELECT COUNT(*) FROM partnership_audit_events WHERE partnership_id = ?",
        (imported.partnership_id,),
    ).fetchone()[0]

    with pytest.raises(partnerships.PartnershipError) as caught:
        partnerships.select_pilot_candidate(
            conn,
            imported.partnership_id,
            "00000000-0000-4000-8000-000000000011",
            1,
            tenant_id="private_pilot",
            actor_id="ari_owner",
            actor_role="relationship_owner",
        )

    assert caught.value.status_code == 409
    slot = store.fetch_one(
        conn,
        "SELECT * FROM pilot_candidate_slots WHERE partnership_id = ? AND slot = 1",
        (imported.partnership_id,),
    )
    assert slot["opportunity_id"] == "00000000-0000-4000-8000-000000000010"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM partnership_audit_events WHERE partnership_id = ?",
            (imported.partnership_id,),
        ).fetchone()[0]
        == before_audits
    )


@pytest.mark.parametrize("ineligible_status", sorted(partnerships.PILOT_SLOT_INELIGIBLE_STATES))
def test_ineligible_status_cannot_mutate_or_satisfy_the_pilot_slate(tmp_path: Path, ineligible_status: str) -> None:
    conn = store.connect(tmp_path / f"ineligible-slate-{ineligible_status}.sqlite3")
    imported = import_ari(conn)
    now = datetime.now(UTC).isoformat()
    opportunity_ids = [
        "00000000-0000-4000-8000-000000000021",
        "00000000-0000-4000-8000-000000000022",
        "00000000-0000-4000-8000-000000000023",
    ]
    for slot, opportunity_id in enumerate(opportunity_ids, start=1):
        store.insert(
            conn,
            "appearance_opportunities",
            {
                "id": opportunity_id,
                "tenant_id": "private_pilot",
                "network_id": "example_network",
                "show_id": "private_pilot",
                "guest_name": f"Synthetic Slate Guest {slot}",
                "why_guest": "The synthetic guest proves the lifecycle slate boundary.",
                "why_now": "Terminal evidence must invalidate active pilot readiness.",
                "proposed_artifact": "A lifecycle-guarded slate",
                "relationship_class": "C2",
                "status": "APPROVED",
                "disposition": "APPROVED",
                "episode_thesis": "Only eligible candidates satisfy the pilot slate.",
                "created_at": now,
                "updated_at": now,
            },
        )
        partnerships.select_pilot_candidate(
            conn,
            imported.partnership_id,
            opportunity_id,
            slot,
            tenant_id="private_pilot",
            actor_id="ari_owner",
            actor_role="relationship_owner",
        )

    store.update(
        conn,
        "appearance_opportunities",
        opportunity_ids[2],
        {"status": ineligible_status},
    )
    before_audits = conn.execute(
        "SELECT COUNT(*) FROM partnership_audit_events WHERE partnership_id = ?",
        (imported.partnership_id,),
    ).fetchone()[0]

    with pytest.raises(partnerships.PartnershipError) as caught:
        partnerships.select_pilot_candidate(
            conn,
            imported.partnership_id,
            opportunity_ids[2],
            1,
            tenant_id="private_pilot",
            actor_id="ari_owner",
            actor_role="relationship_owner",
        )

    assert caught.value.status_code == 409
    assert [
        row["opportunity_id"]
        for row in store.fetch_all(
            conn,
            "SELECT opportunity_id FROM pilot_candidate_slots WHERE partnership_id = ? ORDER BY slot",
            (imported.partnership_id,),
        )
    ] == opportunity_ids
    readiness = partnerships.command_center(conn, imported.partnership_id, "private_pilot")["pilot_readiness"]
    assert readiness["checks"][1]["key"] == "candidate_slate"
    assert readiness["checks"][1]["met"] is False
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM partnership_audit_events WHERE partnership_id = ?",
            (imported.partnership_id,),
        ).fetchone()[0]
        == before_audits
    )


@pytest.mark.parametrize(
    "private_summary",
    [
        "Contact the agreement owner at partner@example.org.",
        "Call the agreement owner at +1 (310) 555-0199.",
        "From: private owner\nTo: partner\nSubject: signed terms\nCopied correspondence.",
        "WHEREAS the party of the first part agrees to the following private terms.",
    ],
)
def test_partnership_items_reject_contact_or_copied_private_text(
    private_summary: str,
) -> None:
    with pytest.raises(partnerships.PartnershipError) as caught:
        partnerships.PartnershipItemInput.from_dict(
            {
                "item_key": "agreement.private_payload",
                "category": "agreement",
                "title": "External agreement owner",
                "summary": private_summary,
                "owner": "Partners",
                "state": "agreed",
                "external_reference": "registry://fixture/agreement",
            }
        )
    assert caught.value.status_code == 422
    assert "remain external" in caught.value.detail


def test_import_partnership_cli(tmp_path: Path, capsys) -> None:
    database = tmp_path / "cli.sqlite3"
    conn = store.connect(database)
    platform.register_show(
        conn,
        tenant_id="private_pilot",
        show_id="private_pilot",
        label="Private Pilot",
        config_ref="config/shows/private-pilot.yaml",
    )
    conn.commit()
    conn.close()
    argv = [
        "import-partnership",
        str(TEMPLATE),
        "--db",
        str(database),
        "--tenant",
        "private_pilot",
        "--show",
        "private_pilot",
        "--actor",
        "example_operator",
    ]
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created"] == 22
    assert main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["unchanged"] == 22


def test_import_partnership_cli_infers_only_one_active_show(tmp_path: Path, capsys) -> None:
    database = tmp_path / "single-show-cli.sqlite3"
    conn = store.connect(database)
    platform.register_show(
        conn,
        tenant_id="private_pilot",
        show_id="flagship",
        label="Flagship",
        config_ref="config/shows/flagship.yaml",
    )
    conn.commit()
    conn.close()
    argv = [
        "import-partnership", str(TEMPLATE), "--db", str(database),
        "--tenant", "private_pilot", "--actor", "example_operator",
    ]
    assert main(argv) == 0
    capsys.readouterr()
    conn = store.connect(database)
    row = store.fetch_one(conn, "SELECT show_id FROM partnerships")
    conn.close()
    assert row["show_id"] == "flagship"


def test_import_partnership_cli_requires_show_when_active_scope_is_ambiguous(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "multi-show-cli.sqlite3"
    conn = store.connect(database)
    for show_id in ("flagship", "field"):
        platform.register_show(
            conn,
            tenant_id="private_pilot",
            show_id=show_id,
            label=show_id.title(),
            config_ref=f"config/shows/{show_id}.yaml",
        )
    conn.commit()
    conn.close()
    assert main([
        "import-partnership", str(TEMPLATE), "--db", str(database),
        "--tenant", "private_pilot", "--actor", "example_operator",
    ]) == 2
    assert "--show is required" in capsys.readouterr().err


def test_import_partnership_cli_rejects_an_inactive_explicit_show(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "retired-show-cli.sqlite3"
    conn = store.connect(database)
    platform.register_show(
        conn,
        tenant_id="private_pilot",
        show_id="field",
        label="Field",
        config_ref="config/shows/field.yaml",
    )
    conn.execute(
        "UPDATE show_registry SET status = 'retired' WHERE tenant_id = ? AND show_id = ?",
        ("private_pilot", "field"),
    )
    conn.commit()
    conn.close()
    assert main([
        "import-partnership", str(TEMPLATE), "--db", str(database),
        "--tenant", "private_pilot", "--show", "field",
        "--actor", "example_operator",
    ]) == 2
    assert "active show" in capsys.readouterr().err


def test_assign_legacy_show_updates_partnership_dependents_atomically(tmp_path: Path) -> None:
    database = tmp_path / "assign-legacy.sqlite3"
    conn = store.connect(database)
    platform.register_show(
        conn,
        tenant_id="private_pilot",
        show_id="field",
        label="Field",
        config_ref="config/shows/field.yaml",
    )
    imported = import_ari(conn)
    for table in (
        "partnership_items", "partnership_audit_events",
        "partnership_item_revisions", "partnership_template_imports",
        "partnership_resource_links", "partnership_reviews",
        "pilot_candidate_slots", "pilot_policies", "pilot_runs",
    ):
        conn.execute(
            f"UPDATE {table} SET show_id = 'legacy' WHERE partnership_id = ?",
            (imported.partnership_id,),
        )
    conn.execute(
        "UPDATE partnerships SET show_id = 'legacy' WHERE id = ?",
        (imported.partnership_id,),
    )
    result = partnerships.assign_legacy_show(
        conn,
        imported.partnership_id,
        tenant_id="private_pilot",
        show_id="field",
        actor_id="example_operator",
        actor_role="producer",
    )
    conn.commit()
    assert result["changed"] is True
    for table in (
        "partnerships", "partnership_items", "partnership_audit_events",
        "partnership_item_revisions", "partnership_template_imports",
    ):
        rows = store.fetch_all(
            conn,
            f"SELECT DISTINCT show_id FROM {table} WHERE partnership_id = ?" if table != "partnerships"
            else "SELECT DISTINCT show_id FROM partnerships WHERE id = ?",
            (imported.partnership_id,),
        )
        assert {row["show_id"] for row in rows} == {"field"}
    audit = store.fetch_one(
        conn,
        "SELECT event_type FROM partnership_audit_events "
        "WHERE partnership_id = ? ORDER BY created_at DESC LIMIT 1",
        (imported.partnership_id,),
    )
    assert audit["event_type"] == "legacy_show_assigned"
    conn.close()


def test_assign_legacy_show_rejects_foreign_candidate_slots_atomically(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "assign-legacy-foreign-slot.sqlite3")
    platform.register_show(
        conn,
        tenant_id="private_pilot",
        show_id="field",
        label="Field",
        config_ref="config/shows/field.yaml",
    )
    imported = import_ari(conn)
    now = datetime.now(UTC).isoformat()
    opportunity_id = "00000000-0000-4000-8000-000000000099"
    store.insert(
        conn,
        "appearance_opportunities",
        {
            "id": opportunity_id,
            "tenant_id": "private_pilot",
            "network_id": "example_network",
            "show_id": "private_pilot",
            "guest_name": "Foreign slot guest",
            "why_guest": "This fixture proves slot custody cannot be rewritten.",
            "why_now": "The migration needs an atomic guard.",
            "proposed_artifact": "A custody receipt",
            "relationship_class": "C2",
            "status": "APPROVED",
            "disposition": "APPROVED",
            "episode_thesis": "Legacy assignment preserves opportunity ownership.",
            "created_at": now,
            "updated_at": now,
        },
    )
    partnerships.select_pilot_candidate(
        conn,
        imported.partnership_id,
        opportunity_id,
        1,
        tenant_id="private_pilot",
        actor_id="ari_owner",
        actor_role="relationship_owner",
    )
    conn.execute(
        "UPDATE pilot_candidate_slots SET show_id = 'legacy' WHERE partnership_id = ?",
        (imported.partnership_id,),
    )
    conn.execute(
        "UPDATE partnerships SET show_id = 'legacy' WHERE id = ?",
        (imported.partnership_id,),
    )
    with pytest.raises(partnerships.PartnershipError, match="candidate slots") as caught:
        partnerships.assign_legacy_show(
            conn,
            imported.partnership_id,
            tenant_id="private_pilot",
            show_id="field",
            actor_id="example_operator",
            actor_role="producer",
        )
    assert caught.value.status_code == 409
    assert store.fetch_one(
        conn, "SELECT show_id FROM partnerships WHERE id = ?", (imported.partnership_id,)
    )["show_id"] == "legacy"
    assert store.fetch_one(
        conn, "SELECT show_id FROM pilot_candidate_slots WHERE partnership_id = ?",
        (imported.partnership_id,),
    )["show_id"] == "legacy"
