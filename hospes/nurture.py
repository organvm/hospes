"""Relationship-class cadence projection with no-send semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import platform


CADENCE_DAYS = {"C0": 14, "C1": 21, "C2": 30, "C3": 45, "C4": None, "C5": None}


def next_due(*, relationship_class: str, last_contact: datetime | None, now: datetime | None = None) -> datetime | None:
    days = CADENCE_DAYS.get(relationship_class)
    if days is None or last_contact is None:
        return None
    return last_contact.astimezone(timezone.utc) + timedelta(days=days)


def due_for_guest(conn: Any, *, tenant_id: str, show_id: str, guest_id: str, relationship_class: str, last_contact: datetime | None, now: datetime | None = None) -> dict[str, Any]:
    due = next_due(relationship_class=relationship_class, last_contact=last_contact, now=now)
    if due is None:
        return {"guest_id": guest_id, "relationship_class": relationship_class, "due": False, "reason": "protected_or_no_contact"}
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    directive = platform.contact_directive(conn, tenant_id=tenant_id, show_id=show_id, guest_id=guest_id)
    blocked = bool(directive["do_not_contact"])
    return {"guest_id": guest_id, "relationship_class": relationship_class, "due": due <= current and not blocked, "blocked": blocked, "due_at": due.isoformat(), "event": "relationship.nurture_due" if due <= current and not blocked else None}


__all__ = ["CADENCE_DAYS", "due_for_guest", "next_due"]
