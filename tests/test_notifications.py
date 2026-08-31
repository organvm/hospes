"""Error and boundary branches of the team-notification service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hospes import configuration, notifications, platform, store

TENANT = "tenant-a"
SHOW = "flagship"
ENTITY = "opportunity://fixture-notification-27"
FUTURE = (datetime.now(UTC) + timedelta(days=1)).isoformat()
PAST = (datetime.now(UTC) - timedelta(days=1)).isoformat()


def _conn(tmp_path: Path, name: str = "notifications.sqlite3"):
    conn = store.connect(tmp_path / name)
    platform.register_show(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        label="Flagship",
        config_ref="config://flagship",
    )
    conn.commit()
    return conn


def _emit(conn, **overrides):
    payload = {
        "tenant_id": TENANT,
        "show_id": SHOW,
        "recipient_role": "producer",
        "notification_type": "draft_ready",
        "title": "Draft ready for review",
        "body_ref": ENTITY,
        "entity_ref": ENTITY,
    }
    payload.update(overrides)
    return notifications.emit(conn, **payload)


def _critical(conn, **overrides):
    return _emit(
        conn,
        recipient_role="editor",
        notification_type="clips_needed",
        title="Clips needed",
        severity="critical",
        **overrides,
    )


def _preview(conn, notification, *, key="notify-preview-key", channel="email"):
    return notifications.preview_delivery(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        notification_id=notification["id"],
        channel=channel,
        target_ref="contact-route://fixture/editor-inbox",
        idempotency_key=key,
        actor_id="producer_fixture",
        actor_role="producer",
    )


def _error(excinfo) -> tuple[int, str]:
    return excinfo.value.status_code, excinfo.value.detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_ref", ""),
        ("entity_ref", "has spaces"),
        ("entity_ref", "route://producer@example.test"),
        ("entity_ref", "route://15551234567"),
        ("body_ref", "\\backslash"),
    ],
)
def test_opaque_references_reject_empty_spaced_and_contact_like_values(tmp_path: Path, field: str, value: str) -> None:
    conn = _conn(tmp_path)
    with pytest.raises(notifications.NotificationError) as excinfo:
        _emit(conn, **{field: value})
    status, detail = _error(excinfo)
    assert status == 422
    assert detail == f"{field} must be an opaque custody reference"
    conn.close()


def test_a_uuid_reference_whose_node_is_all_digits_is_still_opaque(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    reference = "opportunity://1a2b3c4d-1234-4123-8123-123456789012"
    row = _emit(conn, entity_ref=reference, body_ref=reference)
    assert row["entity_ref"] == reference
    conn.close()


def test_unknown_roles_types_and_severities_are_rejected(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    with pytest.raises(notifications.NotificationError, match="recipient_role must be one of"):
        _emit(conn, recipient_role="stage_manager")
    with pytest.raises(notifications.NotificationError, match="notification_type must be one of"):
        _emit(conn, notification_type="everything_is_fine")
    with pytest.raises(notifications.NotificationError, match="severity must be one of"):
        _emit(conn, severity="apocalyptic")
    with pytest.raises(notifications.NotificationError, match="assignment_type must be one of"):
        notifications.assign(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            entity_ref=ENTITY,
            assignment_type="vibes",
            assignee_role="producer",
            title="Nope",
            created_by="producer_fixture",
            actor_role="producer",
        )
    conn.close()


@pytest.mark.parametrize(
    ("title", "detail"),
    [
        ("   ", "title is required"),
        ("x" * 161, "title exceeds 160 characters"),
        ("bad\x07title", "title contains unsupported control characters"),
        (
            "Reach producer@example.test",
            "title must not contain private or contact content",
        ),
    ],
)
def test_titles_are_bounded_and_privacy_checked(tmp_path: Path, title: str, detail: str) -> None:
    conn = _conn(tmp_path)
    with pytest.raises(notifications.NotificationError) as excinfo:
        _emit(conn, title=title)
    assert _error(excinfo) == (422, detail)
    conn.close()


@pytest.mark.parametrize("due_at", [12, "not-a-timestamp", "2026-08-14T10:00:00", "x" * 90])
def test_due_dates_must_be_aware_iso_timestamps(tmp_path: Path, due_at) -> None:
    conn = _conn(tmp_path)
    with pytest.raises(notifications.NotificationError) as excinfo:
        _emit(conn, due_at=due_at)
    status, detail = _error(excinfo)
    assert status == 422
    assert detail.startswith("due_at must ")
    conn.close()


def test_a_private_looking_subject_degrades_instead_of_blocking_the_transition(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    emitted = notifications.emit_transition(
        conn,
        trigger="appearance.approved",
        tenant_id=TENANT,
        show_id=SHOW,
        entity_ref=ENTITY,
        subject="producer@example.test",
        actor_id="producer_fixture",
    )
    assert emitted["title"] == "Draft ready for review — the episode"
    assert notifications._safe_subject("  Theo   Von  ") == "Theo Von"
    assert notifications._safe_subject(None) == "the episode"
    conn.close()


def test_an_unknown_trigger_writes_nothing(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    assert (
        notifications.emit_transition(
            conn,
            trigger="nothing.happened",
            tenant_id=TENANT,
            show_id=SHOW,
            entity_ref=ENTITY,
            subject="Synthetic Guest",
            actor_id="producer_fixture",
        )
        is None
    )
    assert notifications.list_assignments(conn, tenant_id=TENANT, show_id=SHOW) == []
    conn.close()


def test_a_write_against_an_unregistered_show_is_a_domain_conflict(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    with pytest.raises(notifications.NotificationError) as excinfo:
        notifications.assign(
            conn,
            tenant_id=TENANT,
            show_id="never-registered",
            entity_ref=ENTITY,
            assignment_type="draft_review",
            assignee_role="producer",
            title="Orphan work",
            created_by="producer_fixture",
            actor_role="producer",
        )
    status, detail = _error(excinfo)
    assert status == 409
    assert detail.startswith("assignments write rejected")
    conn.close()


def test_assignment_filters_and_completion_boundaries(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    task = notifications.assign(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        entity_ref=ENTITY,
        assignment_type="clip_production",
        assignee_role="editor",
        title="Pull the cold open",
        created_by="producer_fixture",
        actor_role="producer",
        assignee_actor_id="editor_fixture",
        due_at=FUTURE,
    )
    # Creating the same work twice returns the original row.
    assert (
        notifications.assign(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            entity_ref=ENTITY,
            assignment_type="clip_production",
            assignee_role="editor",
            title="A different label",
            created_by="producer_fixture",
            actor_role="producer",
        )["id"]
        == task["id"]
    )
    assert [
        row["id"]
        for row in notifications.list_assignments(
            conn, tenant_id=TENANT, show_id=SHOW, assignee_actor_id="editor_fixture", status="open"
        )
    ] == [task["id"]]
    assert notifications.list_assignments(conn, tenant_id=TENANT, show_id=SHOW, assignee_actor_id="someone_else") == []
    with pytest.raises(notifications.NotificationError, match="limit must be an integer"):
        notifications.list_assignments(conn, tenant_id=TENANT, show_id=SHOW, limit=True)
    with pytest.raises(notifications.NotificationError) as missing:
        notifications.complete_assignment(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            assignment_id="00000000-0000-4000-8000-00000000dead",
            actor_id="editor_fixture",
            actor_role="editor",
        )
    assert _error(missing) == (404, "assignment not found in this tenant/show")

    # A network operator may close another role's work; a cancelled task cannot be closed.
    store.update(conn, "assignments", task["id"], {"status": "cancelled"})
    conn.commit()
    with pytest.raises(notifications.NotificationError) as cancelled:
        notifications.complete_assignment(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            assignment_id=task["id"],
            actor_id="ops_fixture",
            actor_role="network_operator",
        )
    assert _error(cancelled) == (409, "a cancelled assignment cannot be completed")
    store.update(conn, "assignments", task["id"], {"status": "open"})
    conn.commit()
    closed = notifications.complete_assignment(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        assignment_id=task["id"],
        actor_id="ops_fixture",
        actor_role="network_operator",
    )
    assert closed["status"] == "done"
    assert closed["completed_by"] == "ops_fixture"
    conn.close()


def test_notification_filters_and_scoped_mark_all(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _emit(conn, dedupe_key="draft:one")
    _emit(
        conn,
        notification_type="booking_confirmed",
        title="Booking confirmed",
        due_at=FUTURE,
        dedupe_key="booking:one",
    )
    _critical(conn, dedupe_key="clips:one")
    assert [
        row["notification_type"]
        for row in notifications.list_notifications(
            conn, tenant_id=TENANT, show_id=SHOW, recipient_role="editor", severity="critical"
        )
    ] == ["clips_needed"]
    assert (
        notifications.list_notifications(
            conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer", severity="critical"
        )
        == []
    )
    # A dedupe key returns the original row rather than a second bell item.
    assert _emit(conn, dedupe_key="draft:one", title="Different")["title"] == ("Draft ready for review")

    cleared = notifications.mark_all_read(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        actor_id="producer_fixture",
        notification_type="draft_ready",
    )
    assert cleared["marked"] == 1
    assert notifications.summary(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer")["unread"] == 1
    conn.close()


def test_summary_counts_overdue_open_work(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    notifications.assign(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        entity_ref=ENTITY,
        assignment_type="draft_review",
        assignee_role="producer",
        title="Overdue review",
        created_by="producer_fixture",
        actor_role="producer",
        due_at=PAST,
        notify=False,
    )
    summary = notifications.summary(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer")
    assert summary["open_assignments"] == 1
    assert summary["overdue_assignments"] == 1
    assert summary["unread"] == 0
    conn.close()


def test_delivery_previews_reject_bad_channels_keys_roles_and_subjects(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    critical = _critical(conn, dedupe_key="clips:preview")
    with pytest.raises(notifications.NotificationError, match="channel must be one of"):
        notifications.render_delivery_preview(
            critical, channel="carrier_pigeon", target_ref="contact-route://fixture/inbox"
        )
    with pytest.raises(notifications.NotificationError, match="idempotency_key must be"):
        _preview(conn, critical, key="tiny")
    with pytest.raises(notifications.NotificationError) as role:
        notifications.preview_delivery(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            notification_id=critical["id"],
            channel="email",
            target_ref="contact-route://fixture/inbox",
            idempotency_key="notify-preview-role",
            actor_id="editor_fixture",
            actor_role="editor",
        )
    assert _error(role) == (403, "operator role cannot prepare notification delivery")
    with pytest.raises(notifications.NotificationError) as absent:
        notifications.preview_delivery(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            notification_id="00000000-0000-4000-8000-00000000dead",
            channel="email",
            target_ref="contact-route://fixture/inbox",
            idempotency_key="notify-preview-absent",
            actor_id="producer_fixture",
            actor_role="producer",
        )
    assert _error(absent) == (404, "notification not found in this tenant/show")
    conn.close()


def test_an_email_preview_renders_the_due_date(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    critical = _critical(conn, due_at=FUTURE, dedupe_key="clips:due")
    preview = _preview(conn, critical, key="notify-preview-due")["preview"]
    assert "<p>Due: " in preview
    assert notifications.DELIVERY_BANNER in preview
    conn.close()


def test_authorization_requires_a_permitting_outbound_mode(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    prepared = _preview(conn, _critical(conn, dedupe_key="clips:mode"), key="notify-mode-key")
    with pytest.raises(notifications.NotificationError) as excinfo:
        notifications.authorize_delivery(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            delivery_id=prepared["id"],
            outbound_mode="draft_only",
            authorized_by="ops_fixture",
            authorization_ref="receipt://human/notification-mode",
            idempotency_key="notify-mode-authorization",
            draft_only=False,
        )
    assert _error(excinfo) == (
        403,
        "show outbound mode does not permit notification delivery",
    )
    with pytest.raises(notifications.NotificationError) as missing:
        notifications.authorize_delivery(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            delivery_id="00000000-0000-4000-8000-00000000dead",
            outbound_mode="manual_receipt",
            authorized_by="ops_fixture",
            authorization_ref="receipt://human/notification-missing",
            idempotency_key="notify-missing-authorization",
            draft_only=False,
        )
    assert _error(missing) == (404, "notification delivery not found in this tenant/show")
    conn.close()


def test_one_authorization_key_cannot_cover_two_previews(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    first = _preview(conn, _critical(conn, dedupe_key="clips:a"), key="notify-first-key")
    second = _preview(
        conn,
        _critical(conn, entity_ref="opportunity://second", body_ref="opportunity://second", dedupe_key="clips:b"),
        key="notify-second-key",
        channel="webhook",
    )
    for delivery_id in (first["id"], second["id"]):
        call = lambda: notifications.authorize_delivery(  # noqa: E731 - bound per iteration
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            delivery_id=delivery_id,
            outbound_mode="manual_receipt",
            authorized_by="ops_fixture",
            authorization_ref="receipt://human/notification-shared",
            idempotency_key="notify-shared-authorization",
            draft_only=False,
        )
        if delivery_id == first["id"]:
            assert call()["status"] == "authorized"
        else:
            with pytest.raises(notifications.NotificationError) as excinfo:
                call()
            assert _error(excinfo)[0] == 409
    conn.close()


def test_a_receipt_requires_a_live_authorization_and_is_immutable(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    prepared = _preview(conn, _critical(conn, dedupe_key="clips:receipt"), key="notify-receipt-key")
    with pytest.raises(notifications.NotificationError) as unauthorized:
        notifications.record_delivery_receipt(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            delivery_id=prepared["id"],
            receipt_ref="mailbox://fixture/notification",
        )
    assert _error(unauthorized) == (
        403,
        "recording a delivery requires a human authorization receipt",
    )
    authorized = notifications.authorize_delivery(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        delivery_id=prepared["id"],
        outbound_mode="provider_connected",
        authorized_by="ops_fixture",
        authorization_ref="receipt://human/notification-receipt",
        idempotency_key="notify-receipt-authorization",
        draft_only=False,
    )
    # An authorization receipt that no longer binds this preview fails closed.
    conn.execute(
        "DELETE FROM authorization_receipts WHERE id = ?",
        (authorized["authorization_receipt_ref"],),
    )
    conn.commit()
    with pytest.raises(notifications.NotificationError) as invalid:
        notifications.record_delivery_receipt(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            delivery_id=prepared["id"],
            receipt_ref="mailbox://fixture/notification",
        )
    assert _error(invalid) == (403, "notification authorization receipt is invalid")
    conn.close()


def test_an_authorized_delivery_stays_authorized_once_delivered(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    prepared = _preview(conn, _critical(conn, dedupe_key="clips:final"), key="notify-final-key")
    notifications.authorize_delivery(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        delivery_id=prepared["id"],
        outbound_mode="manual_receipt",
        authorized_by="ops_fixture",
        authorization_ref="receipt://human/notification-final",
        idempotency_key="notify-final-authorization",
        draft_only=False,
    )
    delivered = notifications.record_delivery_receipt(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        delivery_id=prepared["id"],
        receipt_ref="mailbox://fixture/notification-final",
    )
    assert delivered["status"] == "delivered"
    # Re-authorizing a delivered row is a no-op rather than a second receipt.
    assert (
        notifications.authorize_delivery(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            delivery_id=prepared["id"],
            outbound_mode="manual_receipt",
            authorized_by="ops_fixture",
            authorization_ref="receipt://human/notification-final",
            idempotency_key="notify-final-authorization",
            draft_only=False,
        )["status"]
        == "delivered"
    )
    conn.close()


def test_the_shipped_runtime_keeps_notification_delivery_draft_only() -> None:
    runtime = configuration.load_runtime()
    assert runtime.feature_defaults[notifications.DRAFT_ONLY_FEATURE] is True
    assert notifications.delivery_is_draft_only() is True
    assert notifications.delivery_is_draft_only(runtime) is True


def test_platform_notification_entry_points_delegate(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    row = platform.notify(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        recipient_role="producer",
        notification_type="draft_ready",
        title="Review",
        body_ref="vault://notification/1",
        entity_ref="episode-2",
    )
    assert row["severity"] == "normal"
    assert (
        len(
            platform.list_notifications(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                recipient_role="producer",
                unread_only=True,
            )
        )
        == 1
    )
    read = platform.mark_notification_read(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        notification_id=row["id"],
        actor_role="producer",
        actor_id="producer_fixture",
    )
    assert read["read_by"] == "producer_fixture"
    assert (
        platform.list_notifications(conn, tenant_id=TENANT, show_id=SHOW, recipient_role="producer", unread_only=True)
        == []
    )
    conn.close()
