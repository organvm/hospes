"""Executable completion predicate for HOSPES issue #27.

Close condition: assignments, transition-driven notifications, My Queue, read
controls, and authorized idempotent email or webhook previews with delivery
receipts pass.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from conftest import synthetic_bearer_authenticator
from hospes import migrations, notifications, platform, service, store
from hospes.api import create_app

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - exercised only without the api extra
    TestClient = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")

ROOT = Path(__file__).resolve().parents[2]
TENANT = "hospes"
SHOW = "field"
OTHER_SHOW = "flagship"
PRODUCER_TOKEN = "issue27-producer-token-0123456789abcdef"  # allow-secret: fixture
HOST_TOKEN = "issue27-host-token-0123456789abcdef"  # allow-secret: fixture
EDITOR_TOKEN = "issue27-editor-token-0123456789abcdef"  # allow-secret: fixture

PRODUCER = service.HumanActor("producer_fixture", service.HumanRole.PRODUCER, TENANT)
HOST = service.HumanActor("host_fixture", service.HumanRole.HOST, TENANT)
EDITOR = service.HumanActor("editor_fixture", service.HumanRole.EDITOR, TENANT)


class _SessionShowASGI:
    """Project the operator's active show into request scope, as the shell does."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-session-show":
                    scope["hospes.operator_show"] = value.decode("utf-8")
        await self.app(scope, receive, send)


class _ShowBoundAuthenticator:
    def __init__(self, inner, show_id: str) -> None:
        self.inner = inner
        self.show_id = show_id

    def authenticate(self, presented_bearer):
        return dataclasses.replace(self.inner.authenticate(presented_bearer), show_id=self.show_id)


def _authenticator():
    return synthetic_bearer_authenticator(
        {
            PRODUCER_TOKEN: ("producer_fixture", "producer", TENANT),
            HOST_TOKEN: ("host_fixture", "host", TENANT),
            EDITOR_TOKEN: ("editor_fixture", "editor", TENANT),
        }
    )


def _client(path: Path, *, bound_show: str | None = None) -> TestClient:
    authenticator = _authenticator()
    if bound_show is not None:
        authenticator = _ShowBoundAuthenticator(authenticator, bound_show)
    app = create_app(
        str(path),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=authenticator,
        csrf_required=False,
    )
    return TestClient(_SessionShowASGI(app))


def _headers(token: str, session_show: str | None = SHOW) -> dict[str, str]:  # allow-secret: synthetic bearer fixture
    headers = {"Authorization": f"Bearer {token}"}
    if session_show is not None:
        headers["X-Session-Show"] = session_show
    return headers


def _register_shows(conn) -> None:
    for show_id, label in ((SHOW, "Field Show"), (OTHER_SHOW, "Flagship Show")):
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
        )
    conn.commit()


def _receipt(
    receipt_type: str,
    external_reference: str,
    *,
    details: dict | None = None,
    minutes_ago: int = 1,
) -> service.ReceiptCreate:
    return service.ReceiptCreate.from_dict(
        {
            "receipt_type": receipt_type,
            "external_reference": external_reference,
            "occurred_at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
            "details": details or {},
        }
    )


def _approve_candidate(conn, *, show_id: str = SHOW, guest: str = "Synthetic Queue Guest") -> str:
    created = service.create_opportunity(
        conn,
        service.OpportunityCreate.from_dict(
            {
                "tenant_id": TENANT,
                "network_id": "issue27_network",
                "show_id": show_id,
                "guest_name": guest,
                "why_guest": "The synthetic guest exercises the team-notification contract.",
                "why_now": "The completion predicate needs a live transition this week.",
                "proposed_artifact": "A one-page operating map",
                "relationship_class": "C2",
                "relationship_owner": "ari_owner",
                "social_cost_1_5": 2,
                "ari_effort": "review_only",
                "preferred_city": "Los Angeles",
                "next_action": "Approve, reject, or protect the candidate.",
                "source_provenance": "Issue 27 predicate fixture provenance.",
            }
        ),
        PRODUCER,
    )
    opportunity_id = created["id"]
    service.attach_thesis_contact(
        conn,
        opportunity_id,
        service.ThesisContactUpdate.from_dict(
            {
                "episode_thesis": "Team coordination should be a record, not a memory.",
                "route_type": "producer_system",
                "route_label": "Opaque producer-owned route",
                "source_provenance": "Synthetic verified route fixture with no contact data.",
                "verified_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            }
        ),
        PRODUCER,
    )
    service.record_decision(
        conn,
        opportunity_id,
        service.DecisionCreate.from_dict({"action": "approve"}),
        HOST,
    )
    return opportunity_id


def _advance_to_recorded(conn, opportunity_id: str) -> None:
    service.create_draft(
        conn,
        opportunity_id,
        service.DraftCreate.from_dict({"kind": "invitation"}),
        PRODUCER,
    )
    service.record_receipt(
        conn, opportunity_id, _receipt("outreach.sent", "mailbox://fixture/sent-27", minutes_ago=40), PRODUCER
    )
    service.record_receipt(
        conn,
        opportunity_id,
        _receipt(
            "reply.classified",
            "mailbox://fixture/reply-27",
            details={"classification": "POSITIVE_INTEREST"},
            minutes_ago=35,
        ),
        PRODUCER,
    )
    service.route_to_studio(
        conn,
        opportunity_id,
        service.StudioRoutingCreate.from_dict({"city": "Los Angeles", "studio_reference": "studio://fixture/la-room"}),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        _receipt(
            "booking.confirmed",
            "calendar://fixture/booking-27",
            details={
                "studio_ref": "studio://fixture/la-room",
                "producer_ref": "producer://fixture/existing-producer",
                "recording_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
            minutes_ago=30,
        ),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        _receipt(
            "consent.signed",
            "release://fixture/consent-27",
            details={"private_pilot": True, "clip_scope": "approved_clips"},
            minutes_ago=25,
        ),
        PRODUCER,
    )
    service.create_brief(
        conn,
        opportunity_id,
        service.BriefCreate.from_dict(
            {
                "research_claims": [
                    {
                        "claim": "The synthetic receipt is present.",
                        "evidence_source": "Synthetic source fixture",
                        "verified": True,
                    }
                ],
                "segments": [{"title": "The Stress Test", "objective": "Test the coordination boundary."}],
            }
        ),
        PRODUCER,
    )
    package = service.declare_assets(
        conn,
        opportunity_id,
        service.AssetPackageCreate.from_dict(
            {
                "assets": [
                    {"kind": kind, "custody_target": f"production://fixture/{kind}"}
                    for kind in sorted(service.REQUIRED_PREFLIGHT_ASSETS)
                ]
            }
        ),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        _receipt(
            "recording.ready",
            "production://fixture/ready-27",
            details={
                "preflight_ref": "preflight://fixture/playback-pass",
                "asset_package_id": package["id"],
            },
            minutes_ago=15,
        ),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        _receipt(
            "recording.completed",
            "production://fixture/rehearsal-27",
            details={"session_kind": "technical_rehearsal"},
            minutes_ago=10,
        ),
        PRODUCER,
    )
    service.record_receipt(
        conn,
        opportunity_id,
        _receipt(
            "recording.completed",
            "production://fixture/guest-pilot-27",
            details={"session_kind": "guest_pilot"},
            minutes_ago=5,
        ),
        PRODUCER,
    )


def _by_type(rows: list[dict]) -> dict[str, dict]:
    return {str(row["notification_type"]): row for row in rows}


def test_lifecycle_transitions_emit_role_notifications_and_queue_tasks(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue27-transitions.sqlite3")
    _register_shows(conn)
    opportunity_id = _approve_candidate(conn)
    entity_ref = f"opportunity://{opportunity_id}"

    producer = _by_type(
        notifications.list_notifications(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer")
    )
    assert producer["draft_ready"]["title"].startswith("Draft ready for review — ")
    assert producer["draft_ready"]["entity_ref"] == entity_ref
    assert producer["draft_ready"]["body_ref"] == entity_ref
    assert producer["draft_ready"]["severity"] == "normal"
    assert producer["draft_ready"]["read_at"] is None

    # Replay is idempotent: an identical approval emits no second bell item.
    service.record_decision(conn, opportunity_id, service.DecisionCreate.from_dict({"action": "approve"}), HOST)
    assert [
        row["id"]
        for row in notifications.list_notifications(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer")
    ] == [producer["draft_ready"]["id"]]
    assert len(notifications.list_assignments(conn, tenant_id=TENANT, show_id=SHOW)) == 1

    _advance_to_recorded(conn, opportunity_id)

    producer = _by_type(
        notifications.list_notifications(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer")
    )
    host = _by_type(notifications.list_notifications(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="host"))
    editor = _by_type(notifications.list_notifications(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="editor"))
    assert set(producer) == {"draft_ready", "booking_confirmed"}
    assert set(host) == {"brief_ready"}
    assert set(editor) == {"clips_needed"}
    assert editor["clips_needed"]["severity"] == "critical"
    assert producer["booking_confirmed"]["due_at"] is not None

    # Every notification carries the assignment it produced, and every rule role
    # owns exactly one open queue task for this entity.
    for row in (*producer.values(), *host.values(), *editor.values()):
        assert row["assignment_id"]
    queue_tasks = notifications.list_assignments(conn, tenant_id=TENANT, show_id=SHOW)
    assert {(row["assignee_role"], row["assignment_type"]) for row in queue_tasks} == {
        ("producer", "draft_review"),
        ("producer", "schedule_prep"),
        ("host", "brief_review"),
        ("editor", "clip_production"),
    }
    assert {row["entity_ref"] for row in queue_tasks} == {entity_ref}
    assert {row["status"] for row in queue_tasks} == {"open"}

    # A technical rehearsal never raised clip work; only the guest pilot did.
    assert len(editor) == 1
    conn.close()


def test_an_unregistered_show_emits_no_team_work(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue27-unregistered.sqlite3")
    opportunity_id = _approve_candidate(conn)
    assert notifications.show_is_registered(conn, tenant_id=TENANT, show_id=SHOW) is False
    assert notifications.list_notifications(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer") == []
    assert notifications.list_assignments(conn, tenant_id=TENANT, show_id=SHOW) == []
    assert service.get_opportunity(conn, opportunity_id, PRODUCER)["disposition"] == "APPROVED"
    conn.close()


def test_my_queue_is_due_sorted_role_scoped_and_completion_is_authorized(tmp_path: Path) -> None:
    database = tmp_path / "issue27-queue.sqlite3"
    conn = store.connect(database)
    _register_shows(conn)
    opportunity_id = _approve_candidate(conn)
    entity_ref = f"opportunity://{opportunity_id}"
    overdue_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    later_at = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    notifications.assign(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        entity_ref=entity_ref,
        assignment_type="clip_production",
        assignee_role="producer",
        title="Overdue clip pull",
        created_by="producer_fixture",
        actor_role="producer",
        due_at=overdue_at,
    )
    notifications.assign(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        entity_ref=entity_ref,
        assignment_type="schedule_prep",
        assignee_role="producer",
        title="Later prep",
        created_by="producer_fixture",
        actor_role="producer",
        due_at=later_at,
    )
    conn.close()

    with _client(database) as client:
        queue = client.get("/v1/shows/field/my-queue", headers=_headers(PRODUCER_TOKEN)).json()
        assert queue["role"] == "producer"
        titles = [row["title"] for row in queue["assignments"]]
        # Due-sorted with the undated transition task last.
        assert titles[0] == "Overdue clip pull"
        assert titles[1] == "Later prep"
        assert titles[-1].startswith("Draft ready for review — ")
        assert queue["assignments"][0]["overdue"] is True
        assert queue["assignments"][-1]["overdue"] is False
        assert queue["summary"]["open_assignments"] == 3
        assert queue["summary"]["overdue_assignments"] == 1
        assert queue["summary"]["unread"] == 3

        task_id = queue["assignments"][0]["id"]
        # The host's queue is a different queue, and the host cannot close producer work.
        host_queue = client.get("/v1/shows/field/my-queue", headers=_headers(HOST_TOKEN)).json()
        assert host_queue["assignments"] == []
        refused = client.post(f"/v1/shows/field/queue-tasks/{task_id}/complete", headers=_headers(HOST_TOKEN))
        assert refused.status_code == 403

        done = client.post(f"/v1/shows/field/queue-tasks/{task_id}/complete", headers=_headers(PRODUCER_TOKEN))
        assert done.status_code == 200
        assert done.json()["status"] == "done"
        assert done.json()["completed_by"] == "producer_fixture"
        # Completion is idempotent.
        again = client.post(f"/v1/shows/field/queue-tasks/{task_id}/complete", headers=_headers(PRODUCER_TOKEN))
        assert again.json()["completed_at"] == done.json()["completed_at"]

        remaining = client.get("/v1/shows/field/my-queue", headers=_headers(PRODUCER_TOKEN)).json()
        assert task_id not in {row["id"] for row in remaining["assignments"]}
        with_done = client.get("/v1/shows/field/my-queue?include_done=true", headers=_headers(PRODUCER_TOKEN)).json()
        assert task_id in {row["id"] for row in with_done["assignments"]}

        # A queue task can also be raised explicitly, and an unassignable role is refused.
        created = client.post(
            "/v1/shows/field/queue-tasks",
            json={
                "entity_ref": entity_ref,
                "assignment_type": "brief_review",
                "assignee_role": "editor",
                "title": "Pull the cold open",
                "due_at": later_at,
            },
            headers=_headers(PRODUCER_TOKEN),
        )
        assert created.status_code == 201
        editor_notifications = client.get("/v1/shows/field/notifications", headers=_headers(EDITOR_TOKEN)).json()
        assert [row["notification_type"] for row in editor_notifications] == ["assignment_assigned"]
        blocked = client.post(
            "/v1/shows/field/queue-tasks",
            json={
                "entity_ref": entity_ref,
                "assignment_type": "brief_review",
                "assignee_role": "producer",
                "title": "Editor cannot assign",
            },
            headers=_headers(EDITOR_TOKEN),
        )
        assert blocked.status_code == 403


def test_bell_summary_and_read_controls_are_role_bound(tmp_path: Path) -> None:
    database = tmp_path / "issue27-read.sqlite3"
    conn = store.connect(database)
    _register_shows(conn)
    opportunity_id = _approve_candidate(conn)
    _advance_to_recorded(conn, opportunity_id)
    conn.close()

    with _client(database) as client:
        summary = client.get("/v1/shows/field/notifications/summary", headers=_headers(PRODUCER_TOKEN)).json()
        assert summary["recipient_role"] == "producer"
        assert summary["unread"] == 2
        assert summary["critical_unread"] == 0
        assert summary["by_type"]["draft_ready"] == 1
        assert summary["by_type"]["clips_needed"] == 0

        editor_summary = client.get("/v1/shows/field/notifications/summary", headers=_headers(EDITOR_TOKEN)).json()
        assert editor_summary["unread"] == 1
        assert editor_summary["critical_unread"] == 1

        # Filtering by type and unread state is bounded and role scoped.
        filtered = client.get(
            "/v1/shows/field/notifications?notification_type=draft_ready&unread_only=true",
            headers=_headers(PRODUCER_TOKEN),
        ).json()
        assert [row["notification_type"] for row in filtered] == ["draft_ready"]
        assert (
            client.get(
                "/v1/shows/field/notifications?notification_type=clips_needed",
                headers=_headers(PRODUCER_TOKEN),
            ).json()
            == []
        )
        assert (
            client.get(
                "/v1/shows/field/notifications?notification_type=not_a_type",
                headers=_headers(PRODUCER_TOKEN),
            ).status_code
            == 422
        )
        assert client.get("/v1/shows/field/notifications?limit=0", headers=_headers(PRODUCER_TOKEN)).status_code == 422

        # Only the addressed role may mark one read.
        target = filtered[0]["id"]
        foreign = client.post(f"/v1/shows/field/notifications/{target}/read", headers=_headers(HOST_TOKEN))
        assert foreign.status_code == 403
        read = client.post(f"/v1/shows/field/notifications/{target}/read", headers=_headers(PRODUCER_TOKEN))
        assert read.status_code == 200
        assert read.json()["read_by"] == "producer_fixture"
        repeat = client.post(f"/v1/shows/field/notifications/{target}/read", headers=_headers(PRODUCER_TOKEN))
        assert repeat.json()["read_at"] == read.json()["read_at"]
        missing = client.post(
            "/v1/shows/field/notifications/00000000-0000-4000-8000-00000000dead/read",
            headers=_headers(PRODUCER_TOKEN),
        )
        assert missing.status_code == 404

        # Mark-all clears the acting role only.
        cleared = client.post(
            "/v1/shows/field/notifications/read-all", json={}, headers=_headers(PRODUCER_TOKEN)
        ).json()
        assert cleared == {
            "recipient_role": "producer",
            "marked": cleared["marked"],
            "read_at": cleared["read_at"],
        }
        assert cleared["marked"] == 1
        assert (
            client.get("/v1/shows/field/notifications/summary", headers=_headers(PRODUCER_TOKEN)).json()["unread"] == 0
        )
        assert client.get("/v1/shows/field/notifications/summary", headers=_headers(EDITOR_TOKEN)).json()["unread"] == 1
        empty = client.post("/v1/shows/field/notifications/read-all", json={}, headers=_headers(PRODUCER_TOKEN)).json()
        assert empty["marked"] == 0
        assert empty["read_at"] is None

    # A show-bound identity cannot read another show's bell or queue.
    with _client(database, bound_show=SHOW) as bound:
        assert (
            bound.get(f"/v1/shows/{OTHER_SHOW}/notifications", headers=_headers(PRODUCER_TOKEN, None)).status_code
            == 403
        )
        assert bound.get(f"/v1/shows/{OTHER_SHOW}/my-queue", headers=_headers(PRODUCER_TOKEN, None)).status_code == 403


def test_email_and_webhook_previews_are_authorized_idempotent_and_receipted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "issue27-previews.sqlite3"
    conn = store.connect(database)
    _register_shows(conn)
    opportunity_id = _approve_candidate(conn)
    _advance_to_recorded(conn, opportunity_id)
    conn.close()

    with _client(database) as client:
        editor_rows = client.get("/v1/shows/field/notifications", headers=_headers(EDITOR_TOKEN)).json()
        critical_id = editor_rows[0]["id"]
        normal_id = client.get("/v1/shows/field/notifications", headers=_headers(PRODUCER_TOKEN)).json()[0]["id"]

        # Only critical notifications may be prepared for external delivery.
        refused = client.post(
            f"/v1/shows/field/notifications/{normal_id}/previews",
            json={
                "channel": "email",
                "target_ref": "contact-route://fixture/producer-inbox",
                "idempotency_key": "issue27-normal-email",
            },
            headers=_headers(PRODUCER_TOKEN),
        )
        assert refused.status_code == 409

        created = client.post(
            f"/v1/shows/field/notifications/{critical_id}/previews",
            json={
                "channel": "email",
                "target_ref": "contact-route://fixture/editor-inbox",
                "idempotency_key": "issue27-critical-email",
            },
            headers=_headers(PRODUCER_TOKEN),
        )
        assert created.status_code == 201
        preview = created.json()
        assert preview["status"] == "preview"
        assert preview["channel"] == "email"
        assert created.headers["cache-control"] == "no-store, private"
        assert notifications.DELIVERY_BANNER in preview["preview"]
        assert "<h1>" in preview["preview"]
        assert hashlib.sha256(preview["preview"].encode("utf-8")).hexdigest() == preview["preview_checksum"]

        # Exact retries are idempotent; a retry with different evidence fails closed.
        retry = client.post(
            f"/v1/shows/field/notifications/{critical_id}/previews",
            json={
                "channel": "email",
                "target_ref": "contact-route://fixture/editor-inbox",
                "idempotency_key": "issue27-critical-email",
            },
            headers=_headers(PRODUCER_TOKEN),
        )
        assert retry.json()["id"] == preview["id"]
        conflict = client.post(
            f"/v1/shows/field/notifications/{critical_id}/previews",
            json={
                "channel": "webhook",
                "target_ref": "contact-route://fixture/editor-inbox",
                "idempotency_key": "issue27-critical-email",
            },
            headers=_headers(PRODUCER_TOKEN),
        )
        assert conflict.status_code == 409

        webhook = client.post(
            f"/v1/shows/field/notifications/{critical_id}/previews",
            json={
                "channel": "webhook",
                "target_ref": "hook://fixture/editor-channel",
                "idempotency_key": "issue27-critical-webhook",
            },
            headers=_headers(PRODUCER_TOKEN),
        ).json()
        body = json.loads(webhook["preview"])
        assert body["notification_id"] == critical_id
        assert body["severity"] == "critical"
        assert body["target_ref"] == "hook://fixture/editor-channel"
        assert body["banner"] == notifications.DELIVERY_BANNER

        # The shipped runtime keeps notification delivery draft-only.
        assert notifications.delivery_is_draft_only() is True
        held = client.post(
            f"/v1/shows/field/notification-previews/{preview['id']}/authorize",
            json={
                "authorization_ref": "receipt://human/notification-27",
                "idempotency_key": "issue27-authorize-email",
            },
            headers=_headers(PRODUCER_TOKEN),
        )
        assert held.status_code == 403
        assert "draft-only" in held.json()["detail"]

        # A delivery receipt is impossible without that authorization.
        early = client.post(
            f"/v1/shows/field/notification-previews/{preview['id']}/receipts",
            json={"receipt_ref": "mailbox://fixture/notification-27"},
            headers=_headers(PRODUCER_TOKEN),
        )
        assert early.status_code == 403

        monkeypatch.setattr(notifications, "delivery_is_draft_only", lambda runtime=None: False)
        authorized = client.post(
            f"/v1/shows/field/notification-previews/{preview['id']}/authorize",
            json={
                "authorization_ref": "receipt://human/notification-27",
                "idempotency_key": "issue27-authorize-email",
            },
            headers=_headers(PRODUCER_TOKEN),
        )
        assert authorized.status_code == 200
        assert authorized.json()["status"] == "authorized"
        assert authorized.json()["authorization_receipt_ref"]

        receipted = client.post(
            f"/v1/shows/field/notification-previews/{preview['id']}/receipts",
            json={"receipt_ref": "mailbox://fixture/notification-27"},
            headers=_headers(PRODUCER_TOKEN),
        )
        assert receipted.status_code == 201
        assert receipted.json()["status"] == "delivered"
        assert receipted.json()["delivery_receipt_ref"] == "mailbox://fixture/notification-27"
        assert (
            client.post(
                f"/v1/shows/field/notification-previews/{preview['id']}/receipts",
                json={"receipt_ref": "mailbox://fixture/notification-27"},
                headers=_headers(PRODUCER_TOKEN),
            ).json()["status"]
            == "delivered"
        )
        immutable = client.post(
            f"/v1/shows/field/notification-previews/{preview['id']}/receipts",
            json={"receipt_ref": "mailbox://fixture/notification-27-again"},
            headers=_headers(PRODUCER_TOKEN),
        )
        assert immutable.status_code == 409

        ledger = client.get(
            f"/v1/shows/field/notification-previews?notification_id={critical_id}",
            headers=_headers(PRODUCER_TOKEN),
        ).json()
        assert {row["status"] for row in ledger} == {"delivered", "preview"}

    conn = store.connect(database)
    receipt_row = store.fetch_one(
        conn,
        "SELECT * FROM provider_receipts WHERE capability = 'notification'",
        (),
    )
    assert receipt_row["provider"] == "email"
    assert receipt_row["status"] == "delivered"
    assert receipt_row["details"]["action"] == "notification.delivered"
    assert receipt_row["payload_checksum"] == preview["preview_checksum"]
    authorization = store.fetch_one(
        conn,
        "SELECT * FROM authorization_receipts WHERE action = ?",
        (notifications.DELIVERY_AUTHORIZATION_ACTION,),
    )
    assert authorization["subject_ref"] == preview["id"]
    conn.close()


def test_schema_domain_contract_dashboard_and_docs_are_complete(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue27-schema.sqlite3")
    assert migrations.current_version(conn) >= 18
    assert "team_notifications_and_assignments" in {row["name"] for row in migrations.applied_migrations(conn)}
    notification_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(notifications)")}
    assert {"assignment_id", "severity", "dedupe_key", "read_by", "updated_at"} <= notification_columns
    assignment_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(assignments)")}
    assert {"title", "completed_at", "completed_by"} <= assignment_columns
    delivery_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(notification_deliveries)")}
    assert {
        "notification_id",
        "channel",
        "status",
        "target_ref",
        "preview_checksum",
        "authorization_receipt_ref",
        "delivery_receipt_ref",
        "idempotency_key",
    } <= delivery_columns
    conn.close()

    kernel = yaml.safe_load((ROOT / "config" / "domain_kernel.yaml").read_text(encoding="utf-8"))
    contract = kernel["notification_contract"]
    assert {"Notification", "NotificationDelivery", "Assignment"}.issubset(kernel["entities"])
    assert set(contract["team_roles"]) == notifications.TEAM_ROLES
    assert set(contract["team_roles"]) == {role.value for role in service.HumanRole}
    assert set(contract["assigning_roles"]) == notifications.ASSIGNING_ROLES
    assert set(contract["notification_types"]) == notifications.NOTIFICATION_TYPES
    assert set(contract["assignment_types"]) == notifications.ASSIGNMENT_TYPES
    assert set(contract["assignment_statuses"]) == notifications.ASSIGNMENT_STATUSES
    assert set(contract["severities"]) == notifications.SEVERITIES
    assert set(contract["delivery_channels"]) == notifications.DELIVERY_CHANNELS
    assert set(contract["delivery_statuses"]) == notifications.DELIVERY_STATUSES
    assert contract["delivery_authorization_action"] == notifications.DELIVERY_AUTHORIZATION_ACTION
    assert contract["draft_only_feature"] == notifications.DRAFT_ONLY_FEATURE
    assert set(contract["transitions"]) == set(notifications.TRANSITION_RULES)
    for trigger, declared in contract["transitions"].items():
        rule = notifications.TRANSITION_RULES[trigger]
        assert declared["recipient_role"] == rule.recipient_role
        assert declared["notification_type"] == rule.notification_type
        assert declared["assignment_type"] == rule.assignment_type
        assert declared["severity"] == rule.severity

    documentation = (ROOT / "docs" / "team-notifications.md").read_text(encoding="utf-8")
    for marker in (
        "notification_contract",
        "/v1/shows/{show_id}/my-queue",
        "queue-tasks",
        "notification-previews",
        "notifications_are_draft_only",
        "dedupe_key",
        "403",
    ):
        assert marker in documentation
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/team-notifications.md" in readme

    for root in (ROOT / "dashboard", ROOT / "hospes" / "resources" / "dashboard"):
        markup = (root / "index.html").read_text(encoding="utf-8")
        assert 'id="btn-notifications"' in markup
        assert 'id="notification-badge"' in markup
        assert 'data-view="queue"' in markup
        assert 'id="queue-view"' in markup
        client_api = (root / "assets" / "api.js").read_text(encoding="utf-8")
        for helper in (
            "loadNotificationSummary",
            "markNotificationRead",
            "markAllNotificationsRead",
            "loadMyQueue",
            "completeQueueTask",
        ):
            assert f"export function {helper}" in client_api
        module = (root / "assets" / "notifications.js").read_text(encoding="utf-8")
        assert "escapeHTML" in module
        assert "activateQueueView" in module
    guide = json.loads((ROOT / "dashboard" / "assets" / "guide.json").read_text(encoding="utf-8"))
    assert {"notification-bell", "my-queue"} <= set(guide["elements"])
