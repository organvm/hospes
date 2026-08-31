"""Tenant/show-scoped product services for HOSPES 1.0.

The functions in this module are deliberately boring persistence commands.
They keep private values outside the repository, make every record carry both
tenant and show scope, and return JSON-ready records for the API and dashboard.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from . import generation, guest_crm, notifications, privacy, store


class PlatformError(ValueError):
    """Raised when a product operation violates a domain boundary."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.\-/]{1,199}$")
GUEST_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
UUID_REF = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
CHANNELS = frozenset({"text", "email", "ig", "in-person", "phone", "hallway"})
SLOT_TYPES = frozenset({"pre", "mid", "post"})
RELATIONSHIP_EDGE_TYPES = frozenset({"worked_with", "knows", "represented_by", "introduced_by", "appeared_with"})


def _timestamp(value: datetime | None = None) -> str:
    return (value or generation.now()).astimezone(timezone.utc).isoformat()


def _ref(value: Any, field: str, *, allow_url: bool = False) -> str:
    text = str(value or "").strip()
    if (
        not text
        or (not allow_url and not OPAQUE.fullmatch(text))
        or (not allow_url and any(char in text for char in "@\\"))
        or (not allow_url and not UUID_REF.fullmatch(text) and privacy.contact_kind(text) is not None)
    ):
        raise PlatformError(f"{field} must be an opaque custody reference")
    return text


def _past_timestamp(value: Any, field: str, *, now: datetime | None = None) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlatformError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlatformError(f"{field} must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    current = (now or generation.now()).astimezone(timezone.utc)
    if normalized > current:
        raise PlatformError(f"{field} cannot be in the future")
    return normalized.isoformat()


def _guest(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not GUEST_ID.fullmatch(text):
        raise PlatformError("guest_id must be an opaque lower-case identifier")
    return text


def _relationship_edge_type(value: Any) -> str:
    text = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    if text not in RELATIONSHIP_EDGE_TYPES:
        raise PlatformError("edge_type is not in the relationship-map taxonomy")
    return text


def _scope(tenant_id: str, show_id: str) -> tuple[str, str]:
    tenant = _ref(tenant_id, "tenant_id")
    show = _ref(show_id, "show_id")
    return tenant, show


def _insert(conn: Any, table: str, values: dict[str, Any]) -> dict[str, Any]:
    try:
        store.insert(conn, table, values)
        conn.commit()
    except Exception as exc:  # sqlite integrity errors become domain errors
        conn.rollback()
        raise PlatformError(f"{table} write rejected: {exc}", 409) from exc
    return values


def register_show(
    conn: Any, *, tenant_id: str, show_id: str, label: str, config_ref: str, now: datetime | None = None
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    current = store.fetch_one(conn, "SELECT * FROM show_registry WHERE tenant_id = ? AND show_id = ?", (tenant, show))
    if current:
        return current
    timestamp = _timestamp(now)
    return _insert(
        conn,
        "show_registry",
        {
            "id": generation.new_id("show"),
            "tenant_id": tenant,
            "show_id": show,
            "label": str(label).strip() or show,
            "config_ref": _ref(config_ref, "config_ref"),
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )


def list_shows(conn: Any, *, tenant_id: str) -> list[dict[str, Any]]:
    return store.fetch_all(
        conn,
        "SELECT * FROM show_registry WHERE tenant_id = ? AND status = 'active' ORDER BY label",
        (_ref(tenant_id, "tenant_id"),),
    )


def suggest_guests(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    network_id: str | None = None,
    relationship_class: str | None = None,
    max_social_cost: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    tenant, show = _scope(tenant_id, show_id)
    if not 1 <= limit <= 100:
        raise PlatformError("limit must be between 1 and 100")
    params: list[Any] = [tenant, show]
    where = (
        "o.tenant_id = ? AND o.show_id = ? "
        "AND o.relationship_class IN ('C1', 'C2', 'C3') "
        "AND o.status NOT IN ('REJECTED', 'DO_NOT_CONTACT')"
    )
    if network_id:
        where += " AND o.network_id = ?"
        params.append(_ref(network_id, "network_id"))
    if relationship_class is not None:
        normalized_class = relationship_class.upper()
        if normalized_class not in {"C1", "C2", "C3"}:
            raise PlatformError("relationship_class must be C1, C2, or C3")
        where += " AND o.relationship_class = ?"
        params.append(normalized_class)
    if max_social_cost is not None:
        if not 1 <= max_social_cost <= 5:
            raise PlatformError("max_social_cost must be between 1 and 5")
        where += " AND o.social_cost_1_5 <= ?"
        params.append(max_social_cost)
    params.append(limit)
    return store.fetch_all(
        conn,
        f"SELECT o.*, 0 AS do_not_contact FROM appearance_opportunities o "
        f"WHERE {where} AND COALESCE("
        "(SELECT d.do_not_contact FROM guest_directives d "
        "WHERE d.tenant_id = o.tenant_id AND d.show_id = o.show_id "
        "AND d.guest_id = COALESCE(o.guest_id, o.source_key)), "
        "(SELECT MAX(h.do_not_contact) FROM guest_history h "
        "WHERE h.tenant_id = o.tenant_id AND h.show_id = o.show_id "
        "AND h.guest_id = COALESCE(o.guest_id, o.source_key)), 0) = 0 "
        "ORDER BY o.social_cost_1_5 ASC, o.updated_at DESC LIMIT ?",
        params,
    )


def record_contact_route(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    route_type: str,
    route_ref: str,
    provenance_ref: str,
    verified_at: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    guest = _guest(guest_id)
    return _insert(
        conn,
        "guest_contact_routes",
        {
            "id": generation.new_id("guest_route"),
            "tenant_id": tenant,
            "show_id": show,
            "guest_id": guest,
            "route_type": _ref(route_type, "route_type"),
            "route_ref": _ref(route_ref, "route_ref"),
            "provenance_ref": _ref(provenance_ref, "provenance_ref"),
            "verified_at": _past_timestamp(verified_at, "verified_at", now=now),
            "status": "active",
            "created_at": _timestamp(now),
        },
    )


def record_touchpoint(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    channel: str,
    notes_ref: str,
    occurred_at: str,
    initiator: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    if channel not in CHANNELS:
        raise PlatformError(f"channel must be one of {sorted(CHANNELS)}")
    return _insert(
        conn,
        "touchpoint_receipts",
        {
            "id": generation.new_id("touchpoint"),
            "tenant_id": tenant,
            "show_id": show,
            "guest_id": _guest(guest_id),
            "channel": channel,
            "notes_ref": _ref(notes_ref, "notes_ref"),
            "occurred_at": _past_timestamp(occurred_at, "occurred_at", now=now),
            "initiator": _ref(initiator, "initiator"),
            "created_at": _timestamp(now),
        },
    )


def add_relationship_edge(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    source_guest_id: str,
    target_guest_id: str,
    edge_type: str,
    relationship_class: str,
    provenance_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    source = _guest(source_guest_id)
    target = _guest(target_guest_id)
    if source == target:
        raise PlatformError("relationship edges cannot point to themselves")
    if relationship_class not in {"C0", "C1", "C2", "C3", "C4", "C5"}:
        raise PlatformError("relationship_class must be C0 through C5")
    return _insert(
        conn,
        "relationship_edges",
        {
            "id": generation.new_id("relationship_edge"),
            "tenant_id": tenant,
            "show_id": show,
            "source_guest_id": source,
            "target_guest_id": target,
            "edge_type": _relationship_edge_type(edge_type),
            "relationship_class": relationship_class,
            "provenance_ref": _ref(provenance_ref, "provenance_ref"),
            "created_at": _timestamp(now),
        },
    )


def record_guest_history(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    season: str,
    episode: str,
    disposition: str,
    notes_ref: str,
    do_not_contact: bool = False,
    occurred_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    season_text = str(season or "").strip()
    episode_text = str(episode or "").strip()
    if not season_text or not episode_text:
        raise PlatformError("season and episode are required")
    if not isinstance(do_not_contact, bool):
        raise PlatformError("do_not_contact must be a boolean")
    normalized_occurred_at = (
        _past_timestamp(occurred_at, "occurred_at", now=now) if occurred_at is not None else _timestamp(now)
    )
    return _insert(
        conn,
        "guest_history",
        {
            "id": generation.new_id("guest_history"),
            "tenant_id": tenant,
            "show_id": show,
            "guest_id": _guest(guest_id),
            "season": season_text,
            "episode": episode_text,
            "disposition": _ref(disposition, "disposition"),
            "occurred_at": normalized_occurred_at,
            "notes_ref": _ref(notes_ref, "notes_ref"),
            "do_not_contact": do_not_contact,
            "created_at": _timestamp(now),
        },
    )


def set_do_not_contact(
    conn: Any, *, tenant_id: str, show_id: str, guest_id: str, notes_ref: str, now: datetime | None = None
) -> dict[str, Any]:
    return record_guest_history(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        guest_id=guest_id,
        season="current",
        episode="registry",
        disposition="DO_NOT_CONTACT",
        notes_ref=notes_ref,
        do_not_contact=True,
        now=now,
    )


def contact_directive(conn: Any, *, tenant_id: str, show_id: str, guest_id: str) -> dict[str, Any]:
    """Return the live do-not-contact directive for one scoped guest identity.

    The directive table is authoritative; when a guest predates it the reader
    falls back to the append-only ``guest_history`` marker so a legacy block is
    never lost.
    """
    tenant, show = _scope(tenant_id, show_id)
    return guest_crm.directive_state(conn, tenant_id=tenant, show_id=show, guest_id=_guest(guest_id))


def guest_history(conn: Any, *, tenant_id: str, show_id: str, guest_id: str) -> list[dict[str, Any]]:
    tenant, show = _scope(tenant_id, show_id)
    return store.fetch_all(
        conn,
        "SELECT * FROM guest_history WHERE tenant_id = ? AND show_id = ? AND guest_id = ? ORDER BY occurred_at",
        (tenant, show, _guest(guest_id)),
    )


def add_sponsor(
    conn: Any, *, tenant_id: str, show_id: str, name: str, contact_ref: str, terms_ref: str, now: datetime | None = None
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    return _insert(
        conn,
        "sponsors",
        {
            "id": generation.new_id("sponsor"),
            "tenant_id": tenant,
            "show_id": show,
            "name": str(name).strip(),
            "contact_ref": _ref(contact_ref, "contact_ref"),
            "terms_ref": _ref(terms_ref, "terms_ref"),
            "status": "active",
            "created_at": _timestamp(now),
        },
    )


def add_sponsorship(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    sponsor_id: str,
    episode_id: str,
    slot_type: str,
    rate_minor: int,
    status: str = "sold",
    now: datetime | None = None,
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    if slot_type not in SLOT_TYPES or int(rate_minor) < 0:
        raise PlatformError("slot_type must be pre, mid, or post and rate must be non-negative")
    sponsor = _ref(sponsor_id, "sponsor_id")
    if not store.fetch_one(
        conn,
        "SELECT id FROM sponsors WHERE id = ? AND tenant_id = ? AND show_id = ?",
        (sponsor, tenant, show),
    ):
        raise PlatformError("sponsor not found in the requested tenant/show", 404)
    return _insert(
        conn,
        "sponsorships",
        {
            "id": generation.new_id("sponsorship"),
            "tenant_id": tenant,
            "show_id": show,
            "sponsor_id": sponsor,
            "episode_id": _ref(episode_id, "episode_id"),
            "slot_type": slot_type,
            "rate_minor": int(rate_minor),
            "status": _ref(status, "status"),
            "created_at": _timestamp(now),
        },
    )


def add_clearance(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    clearance_type: str,
    rights_holder_ref: str,
    license_terms_ref: str,
    cost_minor: int = 0,
    due_date: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    timestamp = _timestamp(now)
    return _insert(
        conn,
        "clearances",
        {
            "id": generation.new_id("clearance"),
            "tenant_id": tenant,
            "show_id": show,
            "episode_id": _ref(episode_id, "episode_id"),
            "clearance_type": _ref(clearance_type, "clearance_type"),
            "status": "pending",
            "rights_holder_ref": _ref(rights_holder_ref, "rights_holder_ref"),
            "license_terms_ref": _ref(license_terms_ref, "license_terms_ref"),
            "cost_minor": int(cost_minor),
            "due_date": due_date,
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )


def update_clearance(
    conn: Any, *, tenant_id: str, show_id: str, clearance_id: str, status: str, now: datetime | None = None
) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)
    if status not in {"pending", "cleared", "denied"}:
        raise PlatformError("clearance status must be pending, cleared, or denied")
    row = store.fetch_one(
        conn, "SELECT * FROM clearances WHERE id = ? AND tenant_id = ? AND show_id = ?", (clearance_id, tenant, show)
    )
    if not row:
        raise PlatformError("clearance not found", 404)
    store.update(conn, "clearances", clearance_id, {"status": status, "updated_at": _timestamp(now)})
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM clearances WHERE id = ?", (clearance_id,)) or row


def publish_blockers(conn: Any, *, tenant_id: str, show_id: str, episode_id: str) -> list[dict[str, Any]]:
    """Return every reason this episode may not publish: rights, then sponsors.

    Uncleared rights items keep their position at the head of the list because
    callers already read them positionally. The sponsor gate is imported here
    rather than at module scope: sponsors builds on this module's primitives,
    so the dependency only closes at call time.
    """
    from . import sponsors

    tenant, show = _scope(tenant_id, show_id)
    episode = _ref(episode_id, "episode_id")
    blockers = store.fetch_all(
        conn,
        "SELECT * FROM clearances WHERE tenant_id = ? AND show_id = ? AND episode_id = ? AND status <> 'cleared'",
        (tenant, show, episode),
    )
    blockers.extend(sponsors.publication_blockers(conn, tenant_id=tenant, show_id=show, episode_id=episode))
    return blockers


# Team notifications and assignments are owned by hospes.notifications. These
# three names stay the historical platform entry points and delegate to it, so
# the team-notification contract has exactly one implementation.


def notify(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    recipient_role: str,
    notification_type: str,
    title: str,
    body_ref: str,
    entity_ref: str,
    due_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return notifications.emit(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        recipient_role=recipient_role,
        notification_type=notification_type,
        title=title,
        body_ref=body_ref,
        entity_ref=entity_ref,
        due_at=due_at,
        now=now,
    )


def list_notifications(
    conn: Any, *, tenant_id: str, show_id: str, recipient_role: str, unread_only: bool = False
) -> list[dict[str, Any]]:
    return notifications.list_notifications(
        conn, tenant_id=tenant_id, show_id=show_id, recipient_role=recipient_role, unread_only=unread_only
    )


def mark_notification_read(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    notification_id: str,
    actor_role: str,
    actor_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    return notifications.mark_read(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        notification_id=notification_id,
        actor_role=actor_role,
        actor_id=actor_id,
        now=now,
    )


def tenant_summary(conn: Any, *, tenant_id: str, show_id: str) -> dict[str, Any]:
    tenant, show = _scope(tenant_id, show_id)

    def count(table: str, where: str = "") -> int:
        row = store.fetch_one(
            conn, f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id = ? AND show_id = ? {where}", (tenant, show)
        )
        return int(row["count"]) if row else 0

    return {
        "tenant_id": tenant,
        "show_id": show,
        "candidates": count("appearance_opportunities"),
        "contacts": count("guest_contact_routes"),
        "touchpoints": count("touchpoint_receipts"),
        "notifications": count("notifications", "AND read_at IS NULL"),
        "pending_clearances": count("clearances", "AND status <> 'cleared'"),
        "distribution_jobs": count("distributions"),
    }


__all__ = [
    "CHANNELS",
    "PlatformError",
    "add_clearance",
    "add_relationship_edge",
    "add_sponsor",
    "add_sponsorship",
    "contact_directive",
    "guest_history",
    "list_notifications",
    "list_shows",
    "mark_notification_read",
    "notify",
    "publish_blockers",
    "record_contact_route",
    "record_guest_history",
    "record_touchpoint",
    "register_show",
    "set_do_not_contact",
    "suggest_guests",
    "tenant_summary",
    "update_clearance",
]
