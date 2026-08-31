"""Team notifications, role assignments, and draft-only delivery previews.

A notification is an in-app, tenant/show-scoped, role-addressed record. It is
produced by a lifecycle transition (or by an explicit assignment) and it never
carries private text: the title is bounded operational prose and the body is an
opaque reference to the entity that already owns the detail.

Three boundaries hold this module together:

* **Scope.** Every read, write, and read-control applies tenant and show scope
  before touching a row, and only the addressed recipient role may mark its own
  notifications read.
* **Idempotence.** Transition emission is keyed by ``dedupe_key`` and assignment
  creation by ``(tenant, show, assignment_type, entity_ref, assignee_role)``, so
  a replayed transition never duplicates a bell item or a queue task.
* **Draft-only delivery.** ``preview_delivery`` renders an email or webhook
  payload and stores its checksum. Nothing in this module sends. Authorization
  requires a show whose outbound mode permits it *and* a runtime whose
  ``notifications_are_draft_only`` feature default is disabled, and it records
  the same human authorization receipt distribution uses. The delivery receipt
  is written only after a human reports the external send.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any, Mapping

from . import configuration, generation, privacy, providers, store


#: Team roles a notification or assignment may address. This is the union of
#: the authenticated operator roles and the post-production ``editor`` role the
#: show registry already declares.
TEAM_ROLES = frozenset(
    {
        "host",
        "network_operator",
        "producer",
        "editorial_owner",
        "relationship_owner",
        "editor",
    }
)
#: Roles allowed to create or reassign work for another role.
ASSIGNING_ROLES = frozenset({"producer", "editorial_owner", "relationship_owner", "network_operator"})
NOTIFICATION_TYPES = frozenset(
    {
        "draft_ready",
        "brief_ready",
        "clips_needed",
        "booking_confirmed",
        "assignment_assigned",
    }
)
ASSIGNMENT_TYPES = frozenset({"draft_review", "brief_review", "clip_production", "schedule_prep"})
ASSIGNMENT_STATUSES = frozenset({"open", "in_progress", "done", "cancelled"})
OPEN_ASSIGNMENT_STATUSES = frozenset({"open", "in_progress"})
SEVERITIES = frozenset({"normal", "critical"})
DELIVERY_CHANNELS = frozenset({"email", "webhook"})
DELIVERY_STATUSES = frozenset({"preview", "authorized", "delivered"})
#: The authorization action bound to every notification delivery receipt.
DELIVERY_AUTHORIZATION_ACTION = "notification.delivery"
#: Runtime feature default that keeps notification delivery draft-only.
DRAFT_ONLY_FEATURE = "notifications_are_draft_only"
DELIVERY_BANNER = (
    "HOSPES NOTIFICATION PREVIEW — NOT SENT. HOSPES renders team notifications; a human authorizes and delivers them."
)

OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.\-/]{1,199}$")
UUID_IN_REFERENCE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{5,255}$")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_TITLE = 160
MAX_LIMIT = 200


class NotificationError(ValueError):
    """A safe notification validation, authorization, or custody error."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


@dataclass(frozen=True)
class TransitionRule:
    """One lifecycle transition and the team work it produces."""

    trigger: str
    recipient_role: str
    notification_type: str
    assignment_type: str
    title_template: str
    severity: str


#: The transition → notification map. Keys are the lifecycle triggers the
#: service layer reports; nothing outside this table emits a notification.
TRANSITION_RULES: Mapping[str, TransitionRule] = {
    rule.trigger: rule
    for rule in (
        TransitionRule(
            trigger="appearance.approved",
            recipient_role="producer",
            notification_type="draft_ready",
            assignment_type="draft_review",
            title_template="Draft ready for review — {subject}",
            severity="normal",
        ),
        TransitionRule(
            trigger="brief.ready",
            recipient_role="host",
            notification_type="brief_ready",
            assignment_type="brief_review",
            title_template="Brief ready for {subject}",
            severity="normal",
        ),
        TransitionRule(
            trigger="booking.confirmed",
            recipient_role="producer",
            notification_type="booking_confirmed",
            assignment_type="schedule_prep",
            title_template="Booking confirmed — {subject}",
            severity="normal",
        ),
        TransitionRule(
            trigger="recording.completed",
            recipient_role="editor",
            notification_type="clips_needed",
            assignment_type="clip_production",
            title_template="Clips needed for {subject}",
            severity="critical",
        ),
    )
}


# ---------------------------------------------------------------------------
# Validation helpers. None of them ever echo a rejected value.
# ---------------------------------------------------------------------------


def _timestamp(value: datetime | None = None) -> str:
    return (value or generation.now()).astimezone(timezone.utc).isoformat()


def _opaque(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or not OPAQUE.fullmatch(text):
        raise NotificationError(f"{field} must be an opaque custody reference")
    # A record identifier is not contact data. UUIDs are masked before the
    # privacy admission check because a UUID whose 12-hex node happens to be all
    # digits reads as a compact phone number, and a 1-in-200 lifecycle
    # transition must not fail on the shape of a random identifier.
    if privacy.contact_kind(UUID_IN_REFERENCE.sub("uuid", text)) is not None:
        raise NotificationError(f"{field} must be an opaque custody reference")
    return text


def _scope(tenant_id: Any, show_id: Any) -> tuple[str, str]:
    return _opaque(tenant_id, "tenant_id"), _opaque(show_id, "show_id")


def _role(value: Any, field: str = "recipient_role") -> str:
    text = str(value or "").strip()
    if text not in TEAM_ROLES:
        raise NotificationError(f"{field} must be one of {sorted(TEAM_ROLES)}")
    return text


def _member(value: Any, allowed: frozenset[str], field: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise NotificationError(f"{field} must be one of {sorted(allowed)}")
    return text


def _title(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise NotificationError("title is required")
    if len(text) > MAX_TITLE:
        raise NotificationError(f"title exceeds {MAX_TITLE} characters")
    if CONTROL.search(text):
        raise NotificationError("title contains unsupported control characters")
    if privacy.private_text_kind(text) is not None:
        raise NotificationError("title must not contain private or contact content")
    return text


def _aware(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        raise NotificationError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NotificationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NotificationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_aware(value: Any, field: str) -> str | None:
    return None if value is None else _aware(value, field)


def _safe_subject(value: Any) -> str:
    """Reduce a display subject to bounded, non-private title text.

    A guest name reaches the title of a transition notification. Names are free
    text, so a name that looks like contact or correspondence content must not
    be able to reject a lifecycle transition: it degrades to a neutral subject
    instead of raising.
    """
    text = re.sub(r"\s+", " ", str(value or "").strip())[:80]
    if not text or CONTROL.search(text) or privacy.private_text_kind(text) is not None:
        return "the episode"
    return text


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise NotificationError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return value


def _idempotency_key(value: Any) -> str:
    text = str(value or "").strip()
    if not IDEMPOTENCY_KEY.fullmatch(text):
        raise NotificationError("idempotency_key must be an opaque 6-256 character key")
    return text


def _commit(
    conn: Any,
    table: str,
    values: dict[str, Any],
    *,
    commit: bool,
    rollback: bool | None = None,
) -> dict[str, Any]:
    """Insert one row, mapping a rejected write to a safe domain conflict.

    ``commit`` says whether this write closes the transaction; ``rollback``
    (defaulting to ``commit``) says whether this call owns it, so a multi-row
    operation whose first insert fails still unwinds instead of leaving a
    half-open transaction behind.
    """
    try:
        store.insert(conn, table, values)
        if commit:
            conn.commit()
    except Exception as exc:
        if commit if rollback is None else rollback:
            conn.rollback()
        raise NotificationError(f"{table} write rejected: {exc}", 409) from exc
    return values


def show_is_registered(conn: Any, *, tenant_id: str, show_id: str) -> bool:
    """True when the tenant/show pair is an active row in the show registry."""
    tenant, show = _scope(tenant_id, show_id)
    return (
        store.fetch_one(
            conn,
            "SELECT id FROM show_registry WHERE tenant_id = ? AND show_id = ? AND status = 'active'",
            (tenant, show),
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Assignments.
# ---------------------------------------------------------------------------


def _assign(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    entity_ref: str,
    assignment_type: str,
    assignee_role: str,
    title: str,
    created_by: str,
    assignee_actor_id: str | None = None,
    due_at: str | None = None,
    notify: bool = True,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Create (or return) one idempotent role assignment for an entity.

    The engine calls this directly for lifecycle transitions; the operator-facing
    :func:`assign` adds the acting-role authorization gate in front of it.
    """
    tenant, show = _scope(tenant_id, show_id)
    entity = _opaque(entity_ref, "entity_ref")
    kind = _member(assignment_type, ASSIGNMENT_TYPES, "assignment_type")
    role = _role(assignee_role, "assignee_role")
    label = _title(title)
    creator = _opaque(created_by, "created_by")
    actor = _opaque(assignee_actor_id, "assignee_actor_id") if assignee_actor_id else None
    deadline = _optional_aware(due_at, "due_at")
    existing = store.fetch_one(
        conn,
        "SELECT * FROM assignments WHERE tenant_id = ? AND show_id = ? "
        "AND assignment_type = ? AND entity_ref = ? AND assignee_role = ?",
        (tenant, show, kind, entity, role),
    )
    if existing is not None:
        return existing
    stamp = _timestamp(now)
    record = {
        "id": generation.new_id("assignment"),
        "tenant_id": tenant,
        "show_id": show,
        "entity_ref": entity,
        "assignment_type": kind,
        "assignee_actor_id": actor,
        "assignee_role": role,
        "status": "open",
        "due_at": deadline,
        "title": label,
        "completed_at": None,
        "completed_by": None,
        "created_by": creator,
        "created_at": stamp,
        "updated_at": stamp,
    }
    _commit(conn, "assignments", record, commit=commit and not notify, rollback=commit)
    if notify:
        emit(
            conn,
            tenant_id=tenant,
            show_id=show,
            recipient_role=role,
            notification_type="assignment_assigned",
            title=label,
            body_ref=entity,
            entity_ref=entity,
            due_at=deadline,
            assignment_id=record["id"],
            dedupe_key=f"assignment_assigned:{record['id']}",
            now=now,
            commit=commit,
        )
    return record


def assign(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    entity_ref: str,
    assignment_type: str,
    assignee_role: str,
    title: str,
    created_by: str,
    actor_role: str,
    assignee_actor_id: str | None = None,
    due_at: str | None = None,
    notify: bool = True,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Assign team work as an authenticated operator. Idempotent per entity."""
    if _role(actor_role, "actor_role") not in ASSIGNING_ROLES:
        raise NotificationError("operator role cannot assign team work", 403)
    return _assign(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        entity_ref=entity_ref,
        assignment_type=assignment_type,
        assignee_role=assignee_role,
        title=title,
        created_by=created_by,
        assignee_actor_id=assignee_actor_id,
        due_at=due_at,
        notify=notify,
        now=now,
        commit=commit,
    )


def list_assignments(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    assignee_role: str | None = None,
    assignee_actor_id: str | None = None,
    status: str | None = None,
    include_done: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return scoped assignments ordered by due date, then creation order."""
    tenant, show = _scope(tenant_id, show_id)
    bounded = _limit(limit)
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if assignee_role is not None:
        clauses.append("assignee_role = ?")
        params.append(_role(assignee_role, "assignee_role"))
    if assignee_actor_id is not None:
        clauses.append("assignee_actor_id = ?")
        params.append(_opaque(assignee_actor_id, "assignee_actor_id"))
    if status is not None:
        clauses.append("status = ?")
        params.append(_member(status, ASSIGNMENT_STATUSES, "status"))
    elif not include_done:
        clauses.append("status IN ('open', 'in_progress')")
    params.append(bounded)
    return store.fetch_all(
        conn,
        "SELECT * FROM assignments WHERE "
        + " AND ".join(clauses)
        + " ORDER BY (due_at IS NULL), due_at, created_at, id LIMIT ?",
        params,
    )


def complete_assignment(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    assignment_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Close one assignment. Only its own role (or a network operator) may."""
    tenant, show = _scope(tenant_id, show_id)
    role = _role(actor_role, "actor_role")
    actor = _opaque(actor_id, "actor_id")
    row = store.fetch_one(
        conn,
        "SELECT * FROM assignments WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, _opaque(assignment_id, "assignment_id")),
    )
    if row is None:
        raise NotificationError("assignment not found in this tenant/show", 404)
    if role != row["assignee_role"] and role != "network_operator":
        raise NotificationError("only the assigned role may complete this task", 403)
    if row["status"] == "done":
        return row
    if row["status"] == "cancelled":
        raise NotificationError("a cancelled assignment cannot be completed", 409)
    stamp = _timestamp(now)
    store.update(
        conn,
        "assignments",
        row["id"],
        {
            "status": "done",
            "completed_at": stamp,
            "completed_by": actor,
            "updated_at": stamp,
        },
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM assignments WHERE id = ?", (row["id"],)) or row


# ---------------------------------------------------------------------------
# Notifications.
# ---------------------------------------------------------------------------


def emit(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    recipient_role: str,
    notification_type: str,
    title: str,
    body_ref: str,
    entity_ref: str,
    severity: str = "normal",
    due_at: str | None = None,
    dedupe_key: str | None = None,
    assignment_id: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Record one role-addressed notification, idempotent on ``dedupe_key``."""
    tenant, show = _scope(tenant_id, show_id)
    role = _role(recipient_role)
    kind = _member(notification_type, NOTIFICATION_TYPES, "notification_type")
    level = _member(severity, SEVERITIES, "severity")
    label = _title(title)
    body = _opaque(body_ref, "body_ref")
    entity = _opaque(entity_ref, "entity_ref")
    deadline = _optional_aware(due_at, "due_at")
    key = _opaque(dedupe_key, "dedupe_key") if dedupe_key else None
    linked = _opaque(assignment_id, "assignment_id") if assignment_id else None
    if key is not None:
        existing = store.fetch_one(
            conn,
            "SELECT * FROM notifications WHERE tenant_id = ? AND show_id = ? AND dedupe_key = ?",
            (tenant, show, key),
        )
        if existing is not None:
            return existing
    stamp = _timestamp(now)
    record = {
        "id": generation.new_id("notification"),
        "tenant_id": tenant,
        "show_id": show,
        "recipient_role": role,
        "notification_type": kind,
        "title": label,
        "body_ref": body,
        "entity_ref": entity,
        "read_at": None,
        "due_at": deadline,
        "created_at": stamp,
        "assignment_id": linked,
        "severity": level,
        "dedupe_key": key,
        "read_by": None,
        "updated_at": stamp,
    }
    return _commit(conn, "notifications", record, commit=commit)


def emit_transition(
    conn: Any,
    *,
    trigger: str,
    tenant_id: str,
    show_id: str,
    entity_ref: str,
    subject: str,
    actor_id: str,
    due_at: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any] | None:
    """Emit the assignment + notification one lifecycle transition produces.

    Returns ``None`` — without writing anything — for a trigger the map does not
    carry, or for a show that is not in the tenant's active show registry. Team
    work is a registered-show capability; an unregistered show has no team, and
    the ``assignments`` foreign key says so.
    """
    rule = TRANSITION_RULES.get(str(trigger))
    if rule is None:
        return None
    tenant, show = _scope(tenant_id, show_id)
    if not show_is_registered(conn, tenant_id=tenant, show_id=show):
        return None
    entity = _opaque(entity_ref, "entity_ref")
    label = _title(rule.title_template.format(subject=_safe_subject(subject)))
    deadline = _optional_aware(due_at, "due_at")
    assignment = _assign(
        conn,
        tenant_id=tenant,
        show_id=show,
        entity_ref=entity,
        assignment_type=rule.assignment_type,
        assignee_role=rule.recipient_role,
        title=label,
        created_by=actor_id,
        due_at=deadline,
        notify=False,
        now=now,
        commit=False,
    )
    return emit(
        conn,
        tenant_id=tenant,
        show_id=show,
        recipient_role=rule.recipient_role,
        notification_type=rule.notification_type,
        title=label,
        body_ref=entity,
        entity_ref=entity,
        severity=rule.severity,
        due_at=deadline,
        dedupe_key=f"{rule.trigger}:{entity}:{rule.recipient_role}",
        assignment_id=assignment["id"],
        now=now,
        commit=commit,
    )


def list_notifications(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    recipient_role: str,
    unread_only: bool = False,
    notification_type: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the recipient role's notification centre, most urgent first."""
    tenant, show = _scope(tenant_id, show_id)
    bounded = _limit(limit)
    clauses = ["tenant_id = ?", "show_id = ?", "recipient_role = ?"]
    params: list[Any] = [tenant, show, _role(recipient_role)]
    if unread_only:
        clauses.append("read_at IS NULL")
    if notification_type is not None:
        clauses.append("notification_type = ?")
        params.append(_member(notification_type, NOTIFICATION_TYPES, "notification_type"))
    if severity is not None:
        clauses.append("severity = ?")
        params.append(_member(severity, SEVERITIES, "severity"))
    params.append(bounded)
    return store.fetch_all(
        conn,
        "SELECT * FROM notifications WHERE "
        + " AND ".join(clauses)
        + " ORDER BY (read_at IS NOT NULL), (due_at IS NULL), due_at, created_at, id LIMIT ?",
        params,
    )


def summary(
    conn: Any, *, tenant_id: str, show_id: str, recipient_role: str, now: datetime | None = None
) -> dict[str, Any]:
    """Return the bell badge: unread counts by type plus queue pressure."""
    tenant, show = _scope(tenant_id, show_id)
    role = _role(recipient_role)
    rows = store.fetch_all(
        conn,
        "SELECT notification_type, severity, read_at FROM notifications "
        "WHERE tenant_id = ? AND show_id = ? AND recipient_role = ?",
        (tenant, show, role),
    )
    by_type = {kind: 0 for kind in sorted(NOTIFICATION_TYPES)}
    unread = 0
    critical = 0
    for row in rows:
        if row["read_at"] is not None:
            continue
        unread += 1
        by_type[str(row["notification_type"])] = by_type.get(str(row["notification_type"]), 0) + 1
        if row["severity"] == "critical":
            critical += 1
    open_rows = store.fetch_all(
        conn,
        "SELECT due_at FROM assignments WHERE tenant_id = ? AND show_id = ? "
        "AND assignee_role = ? AND status IN ('open', 'in_progress')",
        (tenant, show, role),
    )
    current = _timestamp(now)
    overdue = sum(1 for row in open_rows if row["due_at"] is not None and str(row["due_at"]) <= current)
    return {
        "tenant_id": tenant,
        "show_id": show,
        "recipient_role": role,
        "unread": unread,
        "critical_unread": critical,
        "by_type": by_type,
        "open_assignments": len(open_rows),
        "overdue_assignments": overdue,
    }


def mark_read(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    notification_id: str,
    actor_role: str,
    actor_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark one notification read. Only its addressed role may do so."""
    tenant, show = _scope(tenant_id, show_id)
    role = _role(actor_role, "actor_role")
    actor = _opaque(actor_id, "actor_id")
    row = store.fetch_one(
        conn,
        "SELECT * FROM notifications WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, _opaque(notification_id, "notification_id")),
    )
    if row is None:
        raise NotificationError("notification not found in this tenant/show", 404)
    if row["recipient_role"] != role:
        raise NotificationError("only the addressed role may read this notification", 403)
    if row["read_at"] is not None:
        return row
    stamp = _timestamp(now)
    store.update(
        conn,
        "notifications",
        row["id"],
        {"read_at": stamp, "read_by": actor, "updated_at": stamp},
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM notifications WHERE id = ?", (row["id"],)) or row


def mark_all_read(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    actor_id: str,
    notification_type: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Clear the bell for the acting role only, never for another role."""
    tenant, show = _scope(tenant_id, show_id)
    role = _role(actor_role, "actor_role")
    actor = _opaque(actor_id, "actor_id")
    pending = list_notifications(
        conn,
        tenant_id=tenant,
        show_id=show,
        recipient_role=role,
        unread_only=True,
        notification_type=notification_type,
        limit=MAX_LIMIT,
    )
    stamp = _timestamp(now)
    for row in pending:
        store.update(
            conn,
            "notifications",
            row["id"],
            {"read_at": stamp, "read_by": actor, "updated_at": stamp},
        )
    conn.commit()
    return {"recipient_role": role, "marked": len(pending), "read_at": stamp if pending else None}


def my_queue(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    actor_id: str | None = None,
    include_done: bool = False,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the acting role's queue: due-sorted tasks plus its bell state."""
    tenant, show = _scope(tenant_id, show_id)
    role = _role(actor_role, "actor_role")
    assignments = list_assignments(
        conn,
        tenant_id=tenant,
        show_id=show,
        assignee_role=role,
        include_done=include_done,
        limit=limit,
    )
    current = _timestamp(now)
    items = [
        {
            **row,
            "overdue": bool(
                row["due_at"] is not None
                and str(row["due_at"]) <= current
                and row["status"] in OPEN_ASSIGNMENT_STATUSES
            ),
            "mine": bool(actor_id is not None and row["assignee_actor_id"] == actor_id),
        }
        for row in assignments
    ]
    return {
        "tenant_id": tenant,
        "show_id": show,
        "role": role,
        "assignments": items,
        "summary": summary(conn, tenant_id=tenant, show_id=show, recipient_role=role, now=now),
    }


# ---------------------------------------------------------------------------
# Draft-only email / webhook delivery previews.
# ---------------------------------------------------------------------------


def delivery_is_draft_only(runtime: Any | None = None) -> bool:
    """Read the configured notification-delivery posture; default draft-only."""
    try:
        config = runtime if runtime is not None else configuration.load_runtime()
    except configuration.ConfigurationError:  # pragma: no cover - config gate covers this
        return True
    return bool(config.feature_defaults.get(DRAFT_ONLY_FEATURE, True))


def render_delivery_preview(notification: Mapping[str, Any], *, channel: str, target_ref: str) -> str:
    """Render the exact payload a human would deliver. This never sends."""
    kind = _member(channel, DELIVERY_CHANNELS, "channel")
    target = _opaque(target_ref, "target_ref")
    payload = {
        "banner": DELIVERY_BANNER,
        "notification_id": str(notification["id"]),
        "tenant_id": str(notification["tenant_id"]),
        "show_id": str(notification["show_id"]),
        "recipient_role": str(notification["recipient_role"]),
        "notification_type": str(notification["notification_type"]),
        "severity": str(notification["severity"]),
        "title": str(notification["title"]),
        "entity_ref": str(notification["entity_ref"]),
        "due_at": notification["due_at"],
        "target_ref": target,
    }
    if kind == "webhook":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lines = [
        f"<!-- {escape(DELIVERY_BANNER)} -->",
        f"<h1>{escape(payload['title'])}</h1>",
        f"<p>Role: {escape(payload['recipient_role'])} · "
        f"Type: {escape(payload['notification_type'])} · "
        f"Severity: {escape(payload['severity'])}</p>",
        f"<p>Entity: <code>{escape(payload['entity_ref'])}</code></p>",
        f"<p>Route: <code>{escape(target)}</code></p>",
    ]
    if payload["due_at"]:
        lines.append(f"<p>Due: {escape(str(payload['due_at']))}</p>")
    return "\n".join(lines)


def _delivery(conn: Any, *, tenant: str, show: str, delivery_id: str) -> dict[str, Any]:
    row = store.fetch_one(
        conn,
        "SELECT * FROM notification_deliveries WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, _opaque(delivery_id, "delivery_id")),
    )
    if row is None:
        raise NotificationError("notification delivery not found in this tenant/show", 404)
    return row


def preview_delivery(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    notification_id: str,
    channel: str,
    target_ref: str,
    idempotency_key: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render and record one draft-only delivery preview. Retries are exact."""
    if _role(actor_role, "actor_role") not in ASSIGNING_ROLES:
        raise NotificationError("operator role cannot prepare notification delivery", 403)
    tenant, show = _scope(tenant_id, show_id)
    kind = _member(channel, DELIVERY_CHANNELS, "channel")
    target = _opaque(target_ref, "target_ref")
    key = _idempotency_key(idempotency_key)
    creator = _opaque(actor_id, "actor_id")
    notification = store.fetch_one(
        conn,
        "SELECT * FROM notifications WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, _opaque(notification_id, "notification_id")),
    )
    if notification is None:
        raise NotificationError("notification not found in this tenant/show", 404)
    if notification["severity"] != "critical":
        raise NotificationError("external delivery is limited to critical notifications", 409)
    preview = render_delivery_preview(notification, channel=kind, target_ref=target)
    checksum = hashlib.sha256(preview.encode("utf-8")).hexdigest()
    existing = store.fetch_one(
        conn,
        "SELECT * FROM notification_deliveries WHERE tenant_id = ? AND show_id = ? AND idempotency_key = ?",
        (tenant, show, key),
    )
    if existing is not None:
        if (
            existing["notification_id"] != notification["id"]
            or existing["channel"] != kind
            or existing["preview_checksum"] != checksum
        ):
            raise NotificationError("delivery idempotency key is already bound to another preview", 409)
        return {**existing, "preview": preview}
    stamp = _timestamp(now)
    record = {
        "id": generation.new_id("notification_delivery"),
        "tenant_id": tenant,
        "show_id": show,
        "notification_id": notification["id"],
        "channel": kind,
        "status": "preview",
        "target_ref": target,
        "preview_checksum": checksum,
        "authorization_receipt_ref": None,
        "delivery_receipt_ref": None,
        "idempotency_key": key,
        "created_by": creator,
        "created_at": stamp,
        "updated_at": stamp,
    }
    _commit(conn, "notification_deliveries", record, commit=True)
    return {**record, "preview": preview}


def authorize_delivery(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    delivery_id: str,
    outbound_mode: str,
    authorized_by: str,
    authorization_ref: str,
    idempotency_key: str,
    draft_only: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind one human authorization receipt to a prepared delivery."""
    tenant, show = _scope(tenant_id, show_id)
    row = _delivery(conn, tenant=tenant, show=show, delivery_id=delivery_id)
    if draft_only is None:
        draft_only = delivery_is_draft_only()
    if draft_only:
        raise NotificationError("notification delivery is configured draft-only for this runtime", 403)
    if outbound_mode not in {"manual_receipt", "provider_connected"}:
        raise NotificationError("show outbound mode does not permit notification delivery", 403)
    if row["status"] == "delivered":
        return row
    try:
        receipt = providers.require_human_authorization(
            conn,
            tenant_id=tenant,
            show_id=show,
            action=DELIVERY_AUTHORIZATION_ACTION,
            subject_ref=row["id"],
            idempotency_key=_idempotency_key(idempotency_key),
            authorized_by=_opaque(authorized_by, "authorized_by"),
            authorization_ref=_opaque(authorization_ref, "authorization_ref"),
            now=now,
        )
    except providers.ProviderError as exc:
        raise NotificationError(exc.detail, exc.status_code) from exc
    stamp = _timestamp(now)
    store.update(
        conn,
        "notification_deliveries",
        row["id"],
        {
            "status": "authorized",
            "authorization_receipt_ref": receipt["id"],
            "updated_at": stamp,
        },
    )
    conn.commit()
    return _delivery(conn, tenant=tenant, show=show, delivery_id=row["id"])


def record_delivery_receipt(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    delivery_id: str,
    receipt_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the immutable receipt for an externally performed delivery."""
    tenant, show = _scope(tenant_id, show_id)
    row = _delivery(conn, tenant=tenant, show=show, delivery_id=delivery_id)
    reference = _opaque(receipt_ref, "receipt_ref")
    if row["status"] == "delivered":
        if row["delivery_receipt_ref"] == reference:
            return row
        raise NotificationError("delivered notification receipt is immutable", 409)
    if row["status"] != "authorized" or not row["authorization_receipt_ref"]:
        raise NotificationError("recording a delivery requires a human authorization receipt", 403)
    authorization = store.fetch_one(
        conn,
        "SELECT * FROM authorization_receipts WHERE id = ? AND tenant_id = ? "
        "AND show_id = ? AND action = ? AND subject_ref = ?",
        (
            row["authorization_receipt_ref"],
            tenant,
            show,
            DELIVERY_AUTHORIZATION_ACTION,
            row["id"],
        ),
    )
    if authorization is None:
        raise NotificationError("notification authorization receipt is invalid", 403)
    stamp = _timestamp(now)
    store.insert(
        conn,
        "provider_receipts",
        {
            "id": generation.new_id("notification_delivery_receipt"),
            "tenant_id": tenant,
            "show_id": show,
            "capability": "notification",
            "provider": row["channel"],
            "status": "delivered",
            "receipt_ref": reference,
            "details": {
                "action": "notification.delivered",
                "delivery_id": row["id"],
                "notification_id": row["notification_id"],
                "authorization_receipt_id": authorization["id"],
                "authorized_by": authorization["authorized_by"],
            },
            "created_at": stamp,
            "actor_id": authorization["authorized_by"],
            "actor_role": "network_operator",
            "payload_checksum": row["preview_checksum"],
        },
    )
    store.update(
        conn,
        "notification_deliveries",
        row["id"],
        {"status": "delivered", "delivery_receipt_ref": reference, "updated_at": stamp},
    )
    conn.commit()
    return _delivery(conn, tenant=tenant, show=show, delivery_id=row["id"])


def list_deliveries(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    notification_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the scoped delivery ledger, newest first."""
    tenant, show = _scope(tenant_id, show_id)
    bounded = _limit(limit)
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if notification_id is not None:
        clauses.append("notification_id = ?")
        params.append(_opaque(notification_id, "notification_id"))
    params.append(bounded)
    return store.fetch_all(
        conn,
        "SELECT * FROM notification_deliveries WHERE "
        + " AND ".join(clauses)
        + " ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    )


__all__ = [
    "ASSIGNING_ROLES",
    "ASSIGNMENT_STATUSES",
    "ASSIGNMENT_TYPES",
    "DELIVERY_AUTHORIZATION_ACTION",
    "DELIVERY_BANNER",
    "DELIVERY_CHANNELS",
    "DELIVERY_STATUSES",
    "DRAFT_ONLY_FEATURE",
    "NOTIFICATION_TYPES",
    "NotificationError",
    "SEVERITIES",
    "TEAM_ROLES",
    "TRANSITION_RULES",
    "TransitionRule",
    "assign",
    "authorize_delivery",
    "complete_assignment",
    "delivery_is_draft_only",
    "emit",
    "emit_transition",
    "list_assignments",
    "list_deliveries",
    "list_notifications",
    "mark_all_read",
    "mark_read",
    "my_queue",
    "preview_delivery",
    "record_delivery_receipt",
    "render_delivery_preview",
    "show_is_registered",
    "summary",
]
