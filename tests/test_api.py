"""HTTP-surface tests for the optional guest-operations API.

Ported from the overnight ``agent/hospes-core-api`` slice (PR #2). The HTTP
surface is an OPTIONAL extra (fastapi + uvicorn), so this whole module is
skipped cleanly when the extra is absent — ``done.sh`` stays green either way.
The non-HTTP domain logic is covered unconditionally in ``test_service.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# Skip the entire module (with a clear reason) unless the optional API extra is
# installed. This keeps the core test run stdlib-only.
pytest.importorskip("fastapi", reason="optional 'api' extra not installed (pip install -e '.[api]')")
from fastapi.testclient import TestClient  # noqa: E402

from hospes import partnerships, store  # noqa: E402
from hospes.api import create_app  # noqa: E402
from conftest import synthetic_bearer_authenticator  # noqa: E402

TOKEN = "synthetic-test-operator-token"
PRODUCER_TOKEN = f"{TOKEN}-producer"
HOST_TOKEN = f"{TOKEN}-host"
OWNER_TOKEN = f"{TOKEN}-relationship-owner"
OTHER_TENANT_TOKEN = f"{TOKEN}-other-tenant"
PRODUCER = {
    "Authorization": f"Bearer {PRODUCER_TOKEN}",
    "X-Hospes-Actor": "producer_fixture",
    "X-Hospes-Role": "producer",
    "X-Hospes-Tenant": "fixture_tenant",
}
HOST = {
    "Authorization": f"Bearer {HOST_TOKEN}",
    "X-Hospes-Actor": "host_fixture",
    "X-Hospes-Role": "host",
    "X-Hospes-Tenant": "fixture_tenant",
}
RELATIONSHIP_OWNER = {
    "Authorization": f"Bearer {OWNER_TOKEN}",
    "X-Hospes-Actor": "relationship_owner_fixture",
    "X-Hospes-Role": "relationship_owner",
    "X-Hospes-Tenant": "fixture_tenant",
}
OTHER_TENANT = {
    "Authorization": f"Bearer {OTHER_TENANT_TOKEN}",
    "X-Hospes-Actor": "spoofed-producer",
    "X-Hospes-Role": "host",
    "X-Hospes-Tenant": "fixture_tenant",
}


def api_authenticator():
    return synthetic_bearer_authenticator(
        {
            PRODUCER_TOKEN: ("producer_fixture", "producer", "fixture_tenant"),
            HOST_TOKEN: ("host_fixture", "host", "fixture_tenant"),
            OWNER_TOKEN: (
                "relationship_owner_fixture",
                "relationship_owner",
                "fixture_tenant",
            ),
            OTHER_TENANT_TOKEN: (
                "producer_other",
                "producer",
                "another_tenant",
            ),
        }
    )


def client_for(path: Path) -> TestClient:
    return TestClient(
        create_app(
            str(path),
            runtime_kind="synthetic_test",
            _test_bearer_authenticator=api_authenticator(),
            csrf_required=False,
        )
    )


def opportunity_payload(relationship_class: str = "C2") -> dict[str, Any]:
    return {
        "tenant_id": "fixture_tenant",
        "network_id": "fixture_network",
        "show_id": "fixture_show",
        "guest_name": "Guest Example",
        "why_guest": "The synthetic guest can stress-test a concrete operating claim.",
        "why_now": "The synthetic project has reached a useful public decision point.",
        "proposed_artifact": "A one-page decision framework",
        "relationship_class": relationship_class,
        "relationship_owner": "ari_owner",
        "social_cost_1_5": 2,
        "ari_effort": "review_only",
        "preferred_city": "Los Angeles",
        "next_action": "Review the synthetic candidate.",
        "source_provenance": "Synthetic API fixture provenance.",
    }


def create_and_prepare(client: TestClient, relationship_class: str = "C2") -> str:
    created = client.post("/v1/opportunities", json=opportunity_payload(relationship_class), headers=PRODUCER)
    assert created.status_code == 201, created.text
    opportunity_id = str(created.json()["id"])
    prepared = client.put(
        f"/v1/opportunities/{opportunity_id}/thesis-contact",
        json={
            "episode_thesis": ("Small teams should treat relationship permission as infrastructure, not etiquette."),
            "route_type": "publicist_form",
            "route_label": "Public booking form",
            "source_provenance": "Synthetic fixture from an official public booking page.",
            "verified_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        },
        headers=PRODUCER,
    )
    assert prepared.status_code == 200, prepared.text
    return opportunity_id


def preflight_assets() -> list[dict[str, str]]:
    kinds = {
        "environmental_master",
        "host_singles",
        "safety_microphone",
        "backup_recorder",
        "room_tone",
        "slate",
    }
    return [{"kind": kind, "custody_target": f"production://preflight/{kind}"} for kind in sorted(kinds)]


def test_complete_draft_only_vertical_slice_is_persistent(tmp_path: Path) -> None:
    database = tmp_path / "hospes.sqlite3"
    client = client_for(database)
    opportunity_id = create_and_prepare(client)

    approved = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "approve", "note": "Synthetic editorial approval."},
        headers=HOST,
    )
    assert approved.status_code == 200
    assert approved.json()["disposition"] == "APPROVED"

    note = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "note", "note": "Keep the artifact practical."},
        headers=PRODUCER,
    )
    assert note.status_code == 200
    assert len(note.json()["decisions"]) == 2

    draft = client.post(
        f"/v1/opportunities/{opportunity_id}/correspondence-drafts",
        json={"kind": "invitation"},
        headers=PRODUCER,
    )
    assert draft.status_code == 201
    assert draft.json()["status"] == "REVIEWED"
    assert draft.json()["persisted_body"] is False

    routed = client.post(
        f"/v1/opportunities/{opportunity_id}/studio-routing",
        json={"city": "Austin", "studio_reference": "studio://fixture/austin-room"},
        headers=PRODUCER,
    )
    assert routed.status_code == 201
    assert routed.json()["city"] == "Austin"

    brief = client.post(
        f"/v1/opportunities/{opportunity_id}/briefs",
        json={
            "research_claims": [
                {
                    "claim": "The fixture system records every approval decision.",
                    "evidence_source": "Synthetic acceptance-test record",
                    "verified": True,
                }
            ],
            "segments": [
                {
                    "title": "The Stress Test",
                    "objective": "Find the limit of the relationship-permission claim.",
                }
            ],
        },
        headers=PRODUCER,
    )
    assert brief.status_code == 201
    assert brief.json()["segments"][0]["title"] == "The Stress Test"

    assets = client.post(
        f"/v1/opportunities/{opportunity_id}/asset-packages",
        json={"assets": preflight_assets()},
        headers=PRODUCER,
    )
    assert assets.status_code == 201
    assert assets.json()["status"] == "DECLARED"

    due_at = datetime.now(UTC) + timedelta(days=1)
    commitment = client.post(
        f"/v1/opportunities/{opportunity_id}/commitments",
        json={
            "summary": "Confirm the synthetic segment brief.",
            "owner_role": "producer",
            "due_at": due_at.isoformat(),
        },
        headers=PRODUCER,
    )
    assert commitment.status_code == 201
    commitment_id = commitment.json()["id"]

    followups = client.get(
        "/v1/followups",
        params={"due_before": (due_at + timedelta(minutes=1)).isoformat()},
        headers=PRODUCER,
    )
    assert followups.status_code == 200
    assert [item["id"] for item in followups.json()] == [commitment_id]

    completed = client.post(f"/v1/commitments/{commitment_id}/complete", headers=PRODUCER)
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"

    restarted = client_for(database)
    detail = restarted.get(f"/v1/opportunities/{opportunity_id}", headers=PRODUCER)
    assert detail.status_code == 200
    assert detail.json()["asset_packages"][0]["status"] == "DECLARED"
    assert detail.json()["commitments"][0]["status"] == "COMPLETED"
    assert {event["event_type"] for event in detail.json()["audit_events"]} >= {
        "appearance.candidate_created",
        "appearance.approved",
        "outreach.draft_ready",
        "recording.preflight_assets_declared",
        "commitment.completed",
    }


def test_human_boundary_fails_closed_and_there_is_no_send_route(tmp_path: Path) -> None:
    app = create_app(
        str(tmp_path / "auth.sqlite3"),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=api_authenticator(),
        csrf_required=False,
    )
    client = TestClient(app)
    response = client.post("/v1/opportunities", json=opportunity_payload())
    assert response.status_code == 401

    paths = set(app.openapi()["paths"])
    assert "/v1/approval-queue" in paths
    assert "/v1/opportunities/{opportunity_id}/receipts" in paths
    assert "/v1/partnerships/{partnership_id}/pilot-runs" in paths
    assert "/v1/partnerships/{partnership_id}/pilot-runs/{run_id}/plan" in paths
    assert "/v1/partnerships/{partnership_id}/pilot-runs/{run_id}/decisions" in paths
    forbidden_actions = ("send", "deliver", "book", "sign", "publish", "distribute")
    assert not any(action in path for path in paths for action in forbidden_actions)
    nonexistent_send = client.post("/v1/send", json={}, headers=PRODUCER)
    assert nonexistent_send.status_code == 404

    unconfigured = TestClient(
        create_app(
            str(tmp_path / "unconfigured.sqlite3"),
            runtime_kind="synthetic_test",
            csrf_required=False,
        )
    )
    blocked = unconfigured.post("/v1/opportunities", json=opportunity_payload(), headers=PRODUCER)
    assert blocked.status_code == 503

    private_decision = client.post(
        "/v1/partnerships/missing/pilot-runs/missing/decisions",
        json={
            "decision_kind": "wait",
            "expected_revision": 1,
            "contact_email": "private@example.test",
        },
        headers=RELATIONSHIP_OWNER,
    )
    assert private_decision.status_code == 422

    wrong_tenant_headers = OTHER_TENANT
    forbidden = client.post("/v1/opportunities", json=opportunity_payload(), headers=wrong_tenant_headers)
    assert forbidden.status_code == 403

    created = client.post("/v1/opportunities", json=opportunity_payload(), headers=PRODUCER)
    assert created.status_code == 201
    cross_tenant_read = client.get(f"/v1/opportunities/{created.json()['id']}", headers=wrong_tenant_headers)
    assert cross_tenant_read.status_code == 403


def test_draft_preview_is_transient_and_does_not_advance_state(tmp_path: Path) -> None:
    database = tmp_path / "preview.sqlite3"
    app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=api_authenticator(),
        csrf_required=False,
    )
    client = TestClient(app)
    opportunity_id = create_and_prepare(client)
    approved = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "approve"},
        headers=HOST,
    )
    assert approved.status_code == 200
    preview = client.post(
        f"/v1/opportunities/{opportunity_id}/draft-preview",
        json={"kind": "invitation"},
        headers=PRODUCER,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["persisted"] is False
    assert preview.json()["persisted_body"] is False
    assert "body" in preview.json()
    conn = app.state.conn
    assert conn.execute("SELECT COUNT(*) FROM correspondence_drafts").fetchone()[0] == 0
    row = conn.execute("SELECT status FROM appearance_opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    assert row[0] == "APPROVED"

    blocked_receipt = client.post(
        f"/v1/opportunities/{opportunity_id}/receipts",
        json={
            "receipt_type": "outreach.sent",
            "external_reference": "mailbox://fixture/preview-not-reviewed",
            "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "details": {},
        },
        headers=PRODUCER,
    )
    assert blocked_receipt.status_code == 409

    reviewed = client.post(
        f"/v1/opportunities/{opportunity_id}/correspondence-drafts",
        json={"kind": "invitation"},
        headers=PRODUCER,
    )
    assert reviewed.status_code == 201
    assert reviewed.json()["status"] == "REVIEWED"
    stored = conn.execute(
        "SELECT subject, body, status FROM correspondence_drafts WHERE opportunity_id = ?",
        (opportunity_id,),
    ).fetchone()
    assert tuple(stored) == (
        "template:invitation",
        "[CORRESPONDENCE BODY NOT PERSISTED — EXTERNAL OWNER REQUIRED]",
        "REVIEWED",
    )

    accepted_receipt = client.post(
        f"/v1/opportunities/{opportunity_id}/receipts",
        json={
            "receipt_type": "outreach.sent",
            "external_reference": "mailbox://fixture/reviewed-send",
            "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "details": {},
        },
        headers=PRODUCER,
    )
    assert accepted_receipt.status_code == 201
    terminal_preview = client.post(
        f"/v1/opportunities/{opportunity_id}/draft-preview",
        json={"kind": "invitation"},
        headers=PRODUCER,
    )
    assert terminal_preview.status_code == 409


def test_filtered_approval_queue_is_live_and_tenant_scoped(tmp_path: Path) -> None:
    client = client_for(tmp_path / "queue.sqlite3")
    opportunity_id = create_and_prepare(client)

    filtered = client.get(
        "/v1/opportunities",
        params={"state": "EDITORIAL_REVIEW", "owner": "ari_owner"},
        headers=PRODUCER,
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()] == [opportunity_id]

    queue = client.get("/v1/approval-queue", params={"owner": "ari_owner"}, headers=PRODUCER)
    assert queue.status_code == 200, queue.text
    assert queue.json()[0]["thesis"].startswith("Small teams should")
    assert queue.json()[0]["route_provenance"].startswith("Synthetic fixture")
    assert "body" not in queue.json()[0]


def test_partnership_command_center_api_is_live_and_reusable(tmp_path: Path) -> None:
    database = tmp_path / "partnership.sqlite3"
    conn = store.connect(database)
    template = Path(__file__).resolve().parents[1] / "config" / "partnerships" / "example-partnership-private-pilot.yaml"
    imported = partnerships.import_template(
        conn,
        template,
        tenant_id="fixture_tenant",
        actor_id="producer_fixture",
        actor_role="producer",
        show_id="fixture_show",
    )
    conn.commit()
    conn.close()
    client = client_for(database)

    listed = client.get("/v1/partnerships", headers=PRODUCER)
    assert listed.status_code == 200
    assert listed.json()[0]["label"] == "Host + Producer"

    center = client.get(f"/v1/partnerships/{imported.partnership_id}/command-center", headers=PRODUCER)
    assert center.status_code == 200, center.text
    assert center.json()["summary"]["unknown"] == 4
    assert "agreement" in center.json()["categories"]
    assert "deal" in center.json()["categories"]

    item = client.post(
        f"/v1/partnerships/{imported.partnership_id}/items",
        json={
            "item_key": "decision.api_fixture",
            "category": "decision",
            "title": "API fixture decision",
            "summary": "A bounded decision summary with its external owner reference.",
            "owner": "Partners",
            "state": "agreed",
            "external_reference": "registry://fixture/decision",
        },
        headers=PRODUCER,
    )
    assert item.status_code == 201, item.text
    assert item.json()["state"] == "agreed"


def test_protected_relationships_require_the_relationship_owner(tmp_path: Path) -> None:
    client = client_for(tmp_path / "protected.sqlite3")
    opportunity_id = create_and_prepare(client, relationship_class="C5")

    host_approval = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "approve"},
        headers=HOST,
    )
    assert host_approval.status_code == 403

    producer_protect = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "protect"},
        headers=PRODUCER,
    )
    assert producer_protect.status_code == 403

    protected = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "protect", "note": "Synthetic owner protection."},
        headers=RELATIONSHIP_OWNER,
    )
    assert protected.status_code == 200
    assert protected.json()["disposition"] == "PROTECTED"
    assert protected.json()["contact_route"]["usable"] is False

    draft = client.post(
        f"/v1/opportunities/{opportunity_id}/correspondence-drafts",
        json={"kind": "invitation"},
        headers=PRODUCER,
    )
    assert draft.status_code == 403


def test_unverified_claims_and_invalid_decisions_are_rejected(tmp_path: Path) -> None:
    client = client_for(tmp_path / "validation.sqlite3")
    opportunity_id = create_and_prepare(client)

    note = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "note"},
        headers=PRODUCER,
    )
    assert note.status_code == 422

    approved = client.post(
        f"/v1/opportunities/{opportunity_id}/decisions",
        json={"action": "approve", "note": "Test the evidence gate first."},
        headers=HOST,
    )
    assert approved.status_code == 200

    brief = client.post(
        f"/v1/opportunities/{opportunity_id}/briefs",
        json={
            "research_claims": [
                {
                    "claim": "An intentionally unverified fixture claim.",
                    "evidence_source": "Synthetic source",
                    "verified": False,
                }
            ],
            "segments": [
                {
                    "title": "Receipts",
                    "objective": "Demonstrate that unverified claims cannot enter a brief.",
                }
            ],
        },
        headers=PRODUCER,
    )
    assert brief.status_code == 422

    rejected_id = create_and_prepare(client)
    rejected = client.post(
        f"/v1/opportunities/{rejected_id}/decisions",
        json={"action": "reject", "note": "Does not fit the synthetic pilot."},
        headers=HOST,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "DECLINED"
