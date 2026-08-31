"""Sponsor and ad-inventory tracking: sold/available slots and revenue.

Migration 008 gave HOSPES a sponsor registry and a sponsorship join.  Neither
could answer the two questions an operator actually asks — *what is still for
sale on this episode* and *what did this episode earn* — because an unsold slot
had no row anywhere.  This module owns that inventory:

* ``sponsor_slots`` is the declared ad inventory for one episode: the slot
  types the show sells, their list rate, and whether the slot is **committed**
  (promised to an advertiser, so leaving it unfilled is a publication defect).
* ``sponsorships`` assigns one declared slot to one sponsor as ``reserved`` or
  ``sold``.  Revenue counts only ``sold``.
* ``sponsor_claims`` is claims governance: every factual sponsor claim carries
  its source, its verification date, and an explicit human approval before the
  episode may publish.
* ``sponsorship_receipts`` is the append-only attributable evidence trail; the
  actor and role always come from the authenticated operator, never a payload.

Money is stored in minor units as integers.  Nothing here sends an invoice,
signs a deal, or commits the show to a sponsor: recording a sale is bookkeeping
about a decision a human already made outside HOSPES.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from . import configuration, generation, platform, privacy, store


class SponsorError(platform.PlatformError):
    """A safe sponsor-inventory validation, authorization, or custody error.

    It subclasses :class:`hospes.platform.PlatformError` so the HTTP adapter
    maps it to a status code through the boundary it already has.
    """


SLOT_TYPES: tuple[str, ...] = ("pre", "mid", "post")
ASSIGNMENT_STATUSES = frozenset({"reserved", "sold"})
SPONSOR_STATUSES = frozenset({"active", "paused", "ended"})
READ_ROLES = frozenset({"host", "network_operator", "producer", "editorial_owner", "relationship_owner"})
WRITE_ROLES = frozenset({"producer", "editorial_owner", "relationship_owner"})
CLAIM_APPROVAL_ROLES = frozenset({"editorial_owner", "relationship_owner"})
MAX_RATE_MINOR = 100_000_000
MAX_CLAIM_LENGTH = 500
CSV_COLUMNS = ("Episode", "Sponsor", "Slot", "Rate", "Date")

_NAME = re.compile(r"^[^\x00-\x1f\x7f]{2,120}$")
_CATEGORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _/&.-]{1,63}$")
_SOURCE_URL = re.compile(
    r"^https?://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
    r"(?::\d{1,5})?(?:/[^\s<>\"']{0,1800})?$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _timestamp(now: datetime | None = None) -> str:
    return (now or generation.now()).astimezone(timezone.utc).isoformat()


def _require_role(actor_role: Any, allowed: frozenset[str], action: str) -> str:
    if not isinstance(actor_role, str) or actor_role not in allowed:
        raise SponsorError(f"operator role cannot {action}", 403)
    return actor_role


def _scope(tenant_id: str, show_id: str) -> tuple[str, str]:
    return platform._scope(tenant_id, show_id)


def _actor(actor_id: Any) -> str:
    return platform._ref(actor_id, "actor_id")


def _name(value: Any) -> str:
    text = str(value or "").strip()
    if _NAME.fullmatch(text) is None:
        raise SponsorError("sponsor name must be 2-120 printable characters")
    if privacy.contact_kind(text) is not None:
        raise SponsorError("sponsor name must not carry contact data")
    return text


def _category(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if _CATEGORY.fullmatch(text) is None:
        raise SponsorError("sponsor category must be a short editorial category")
    return text


def _slot_type(value: Any, policy: configuration.SponsorInventoryPolicy) -> str:
    text = str(value or "").strip().lower()
    if text not in policy.slot_types:
        raise SponsorError(f"slot_type must be one of {list(policy.slot_types)}")
    return text


def _rate_minor(value: Any, field: str = "rate_minor") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SponsorError(f"{field} must be an integer number of minor currency units")
    if not 0 <= value <= MAX_RATE_MINOR:
        raise SponsorError(f"{field} must be between 0 and {MAX_RATE_MINOR}")
    return value


def _committed(value: Any) -> bool:
    if not isinstance(value, bool):
        raise SponsorError("committed must be a boolean")
    return value


def _assignment_status(value: Any) -> str:
    text = str(value or "sold").strip().lower()
    if text not in ASSIGNMENT_STATUSES:
        raise SponsorError(f"status must be one of {sorted(ASSIGNMENT_STATUSES)}")
    return text


def _claim_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise SponsorError("claim text is required")
    if len(text) > MAX_CLAIM_LENGTH:
        raise SponsorError(f"claim exceeds {MAX_CLAIM_LENGTH} characters")
    if _CONTROL.search(text):
        raise SponsorError("claim contains unsupported control characters")
    return text


def _source_url(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 2000 or _SOURCE_URL.fullmatch(text) is None:
        raise SponsorError("source_url must be a public http(s) URL")
    return text


def _verified_date(value: Any, *, now: datetime | None = None) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise SponsorError("verified_date must be an ISO 8601 date") from exc
    today = (now or generation.now()).astimezone(timezone.utc).date()
    if parsed > today:
        raise SponsorError("verified_date cannot be in the future")
    return parsed.isoformat()


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _policy(show_id: str, policy: configuration.SponsorInventoryPolicy | None) -> configuration.SponsorInventoryPolicy:
    return policy or configuration.sponsor_inventory_policy(show_id)


def _receipt(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    event_type: str,
    subject_ref: str,
    actor_id: str,
    actor_role: str,
    episode_id: str | None = None,
    sponsor_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one attributable sponsorship receipt (no workflow state changes)."""
    row = {
        "id": generation.new_id("sponsorship_receipt"),
        "tenant_id": tenant_id,
        "show_id": show_id,
        "event_type": event_type,
        "episode_id": episode_id,
        "sponsor_id": sponsor_id,
        "subject_ref": subject_ref,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "details": dict(details or {}),
        "created_at": _timestamp(now),
    }
    store.insert(conn, "sponsorship_receipts", row)
    return row


# ---------------------------------------------------------------------------
# Sponsors.
# ---------------------------------------------------------------------------


def create_sponsor(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    name: str,
    contact_ref: str,
    terms_ref: str,
    category: str | None = None,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Register one sponsor. Contact and terms stay opaque custody references."""
    role = _require_role(actor_role, WRITE_ROLES, "manage sponsors")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    sponsor_name = _name(name)
    contact = platform._ref(contact_ref, "contact_ref")
    terms = platform._ref(terms_ref, "terms_ref")
    sponsor_category = _category(category)
    timestamp = _timestamp(now)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM sponsors WHERE tenant_id = ? AND show_id = ? AND name = ?",
        (tenant, show, sponsor_name),
    )
    if existing is not None:
        if existing["contact_ref"] != contact or existing["terms_ref"] != terms:
            raise SponsorError("sponsor already exists with different custody references", 409)
        return existing
    row = {
        "id": generation.new_id("sponsor"),
        "tenant_id": tenant,
        "show_id": show,
        "name": sponsor_name,
        "category": sponsor_category,
        "contact_ref": contact,
        "terms_ref": terms,
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    store.insert(conn, "sponsors", row)
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        event_type="sponsor.registered",
        subject_ref=f"sponsor://{row['id']}",
        actor_id=actor,
        actor_role=role,
        sponsor_id=row["id"],
        details={"name": sponsor_name, "category": sponsor_category},
        now=now,
    )
    conn.commit()
    return row


def _sponsor_row(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, sponsor_id: Any) -> dict[str, Any]:
    identifier = platform._ref(sponsor_id, "sponsor_id")
    row = store.fetch_one(
        conn,
        "SELECT * FROM sponsors WHERE id = ? AND tenant_id = ? AND show_id = ?",
        (identifier, tenant_id, show_id),
    )
    if row is None:
        raise SponsorError("sponsor not found in this tenant/show", 404)
    return row


def set_sponsor_status(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    sponsor_id: str,
    status: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pause or end a sponsor without deleting its historical revenue rows."""
    role = _require_role(actor_role, WRITE_ROLES, "manage sponsors")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    if status not in SPONSOR_STATUSES:
        raise SponsorError(f"sponsor status must be one of {sorted(SPONSOR_STATUSES)}")
    sponsor = _sponsor_row(conn, tenant_id=tenant, show_id=show, sponsor_id=sponsor_id)
    store.update(conn, "sponsors", sponsor["id"], {"status": status, "updated_at": _timestamp(now)})
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        event_type="sponsor.status_changed",
        subject_ref=f"sponsor://{sponsor['id']}",
        actor_id=actor,
        actor_role=role,
        sponsor_id=sponsor["id"],
        details={"from": sponsor["status"], "to": status},
        now=now,
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM sponsors WHERE id = ?", (sponsor["id"],)) or sponsor


def list_sponsors(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    include_claims: bool = True,
) -> list[dict[str, Any]]:
    """Return every sponsor in scope with its claims-governance state."""
    _require_role(actor_role, READ_ROLES, "read sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    rows = store.fetch_all(
        conn,
        "SELECT * FROM sponsors WHERE tenant_id = ? AND show_id = ? ORDER BY name",
        (tenant, show),
    )
    if not include_claims:
        return rows
    claims = store.fetch_all(
        conn,
        "SELECT * FROM sponsor_claims WHERE tenant_id = ? AND show_id = ? ORDER BY created_at, id",
        (tenant, show),
    )
    by_sponsor: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        by_sponsor.setdefault(str(claim["sponsor_id"]), []).append(_public_claim(claim))
    for row in rows:
        sponsor_claims = by_sponsor.get(str(row["id"]), [])
        row["claims"] = sponsor_claims
        row["unapproved_claim_count"] = sum(1 for claim in sponsor_claims if not claim["approved_for_external_use"])
    return rows


# ---------------------------------------------------------------------------
# Claims governance.
# ---------------------------------------------------------------------------


def _public_claim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": row["id"],
        "sponsor_id": row["sponsor_id"],
        "claim": row["claim"],
        "source_url": row["source_url"],
        "verified_date": row["verified_date"],
        "approved_for_external_use": bool(row["approved_for_external_use"]),
        "approved_by": row.get("approved_by"),
        "approved_by_role": row.get("approved_by_role"),
        "approved_at": row.get("approved_at"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def record_claim(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    sponsor_id: str,
    claim: str,
    source_url: str,
    verified_date: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one cited sponsor claim. It is unapproved until a human approves."""
    role = _require_role(actor_role, WRITE_ROLES, "record sponsor claims")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    sponsor = _sponsor_row(conn, tenant_id=tenant, show_id=show, sponsor_id=sponsor_id)
    text = _claim_text(claim)
    url = _source_url(source_url)
    verified = _verified_date(verified_date, now=now)
    checksum = _checksum(text)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM sponsor_claims WHERE tenant_id = ? AND show_id = ? AND sponsor_id = ? AND claim_checksum = ?",
        (tenant, show, sponsor["id"], checksum),
    )
    if existing is not None:
        if existing["source_url"] != url or existing["verified_date"] != verified:
            raise SponsorError("claim already exists with different evidence", 409)
        return _public_claim(existing)
    timestamp = _timestamp(now)
    row = {
        "id": generation.new_id("sponsor_claim"),
        "tenant_id": tenant,
        "show_id": show,
        "sponsor_id": sponsor["id"],
        "claim": text,
        "claim_checksum": checksum,
        "source_url": url,
        "verified_date": verified,
        "approved_for_external_use": False,
        "approved_by": None,
        "approved_by_role": None,
        "approved_at": None,
        "recorded_by": actor,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    store.insert(conn, "sponsor_claims", row)
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        event_type="sponsor.claim_recorded",
        subject_ref=f"sponsor-claim://{row['id']}",
        actor_id=actor,
        actor_role=role,
        sponsor_id=sponsor["id"],
        details={
            "claim_checksum": checksum,
            "source_url": url,
            "verified_date": verified,
        },
        now=now,
    )
    conn.commit()
    return _public_claim(row)


def approve_claim(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    sponsor_id: str,
    claim_id: str,
    actor_id: str,
    actor_role: str,
    approved: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Approve or withdraw approval for one sponsor claim's external use."""
    role = _require_role(actor_role, CLAIM_APPROVAL_ROLES, "approve sponsor claims")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    if not isinstance(approved, bool):
        raise SponsorError("approved must be a boolean")
    sponsor = _sponsor_row(conn, tenant_id=tenant, show_id=show, sponsor_id=sponsor_id)
    identifier = platform._ref(claim_id, "claim_id")
    row = store.fetch_one(
        conn,
        "SELECT * FROM sponsor_claims WHERE id = ? AND tenant_id = ? AND show_id = ? AND sponsor_id = ?",
        (identifier, tenant, show, sponsor["id"]),
    )
    if row is None:
        raise SponsorError("sponsor claim not found in this tenant/show", 404)
    timestamp = _timestamp(now)
    store.update(
        conn,
        "sponsor_claims",
        row["id"],
        {
            "approved_for_external_use": approved,
            "approved_by": actor if approved else None,
            "approved_by_role": role if approved else None,
            "approved_at": timestamp if approved else None,
            "updated_at": timestamp,
        },
    )
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        event_type=("sponsor.claim_approved" if approved else "sponsor.claim_approval_withdrawn"),
        subject_ref=f"sponsor-claim://{row['id']}",
        actor_id=actor,
        actor_role=role,
        sponsor_id=sponsor["id"],
        details={"claim_checksum": row["claim_checksum"]},
        now=now,
    )
    conn.commit()
    updated = store.fetch_one(conn, "SELECT * FROM sponsor_claims WHERE id = ?", (row["id"],))
    return _public_claim(updated or row)


def list_claims(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    sponsor_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the claims register for one show, or one sponsor inside it."""
    _require_role(actor_role, READ_ROLES, "read sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if sponsor_id is not None:
        clauses.append("sponsor_id = ?")
        params.append(platform._ref(sponsor_id, "sponsor_id"))
    rows = store.fetch_all(
        conn,
        "SELECT * FROM sponsor_claims WHERE " + " AND ".join(clauses) + " ORDER BY created_at, id",
        params,
    )
    return [_public_claim(row) for row in rows]


# ---------------------------------------------------------------------------
# Inventory.
# ---------------------------------------------------------------------------


def declare_slots(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    slots: Sequence[Mapping[str, Any]],
    actor_id: str,
    actor_role: str,
    policy: configuration.SponsorInventoryPolicy | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Declare the complete ad inventory for one episode (an idempotent PUT).

    A slot the payload omits is withdrawn, but only while nothing is assigned to
    it: withdrawing inventory a sponsor already holds would silently erase a
    sale, so that case is a conflict the operator has to resolve explicitly.
    """
    role = _require_role(actor_role, WRITE_ROLES, "manage sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    episode = platform._ref(episode_id, "episode_id")
    resolved = _policy(show, policy)
    if not isinstance(slots, Sequence) or isinstance(slots, (str, bytes)):
        raise SponsorError("slots must be a list of slot declarations")
    declared: dict[str, dict[str, Any]] = {}
    for item in slots:
        if not isinstance(item, Mapping):
            raise SponsorError("each slot declaration must be an object")
        unknown = sorted(set(item) - {"slot_type", "rate_minor", "committed"})
        if unknown:
            raise SponsorError(f"slot declaration has unsupported fields: {unknown}")
        slot_type = _slot_type(item.get("slot_type"), resolved)
        if slot_type in declared:
            raise SponsorError(f"slot_type {slot_type!r} is declared twice")
        declared[slot_type] = {
            "slot_type": slot_type,
            "rate_minor": _rate_minor(item.get("rate_minor")),
            "committed": _committed(item.get("committed", False)),
        }
    existing = {
        str(row["slot_type"]): row
        for row in store.fetch_all(
            conn,
            "SELECT * FROM sponsor_slots WHERE tenant_id = ? AND show_id = ? AND episode_id = ?",
            (tenant, show, episode),
        )
    }
    assigned = {
        str(row["slot_type"])
        for row in store.fetch_all(
            conn,
            "SELECT slot_type FROM sponsorships WHERE tenant_id = ? AND show_id = ? AND episode_id = ?",
            (tenant, show, episode),
        )
    }
    withdrawn = sorted(set(existing) - set(declared))
    blocked = sorted(slot_type for slot_type in withdrawn if slot_type in assigned)
    if blocked:
        raise SponsorError(f"release the sponsorship before withdrawing slots: {blocked}", 409)
    timestamp = _timestamp(now)
    for slot_type in withdrawn:
        conn.execute("DELETE FROM sponsor_slots WHERE id = ?", (existing[slot_type]["id"],))
    for slot_type, values in declared.items():
        current = existing.get(slot_type)
        if current is None:
            store.insert(
                conn,
                "sponsor_slots",
                {
                    "id": generation.new_id("sponsor_slot"),
                    "tenant_id": tenant,
                    "show_id": show,
                    "episode_id": episode,
                    "slot_type": slot_type,
                    "rate_minor": values["rate_minor"],
                    "rate_currency": resolved.currency,
                    "committed": values["committed"],
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            continue
        store.update(
            conn,
            "sponsor_slots",
            current["id"],
            {
                "rate_minor": values["rate_minor"],
                "rate_currency": resolved.currency,
                "committed": values["committed"],
                "updated_at": timestamp,
            },
        )
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        event_type="sponsorship.inventory_declared",
        subject_ref=f"episode://{episode}",
        actor_id=actor,
        actor_role=role,
        episode_id=episode,
        details={
            "declared": sorted(declared),
            "withdrawn": withdrawn,
            "committed": sorted(key for key, value in declared.items() if value["committed"]),
        },
        now=now,
    )
    conn.commit()
    return list_slots(conn, tenant_id=tenant, show_id=show, actor_role=role, episode_id=episode)


def _inventory_rows(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str | None,
) -> list[dict[str, Any]]:
    """Merge declared inventory with assignments, keeping undeclared sales visible."""
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant_id, show_id]
    if episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(platform._ref(episode_id, "episode_id"))
    where = " AND ".join(clauses)
    slots = store.fetch_all(conn, f"SELECT * FROM sponsor_slots WHERE {where}", list(params))
    assignments = store.fetch_all(conn, f"SELECT * FROM sponsorships WHERE {where}", list(params))
    names = {
        str(row["id"]): row
        for row in store.fetch_all(
            conn,
            "SELECT * FROM sponsors WHERE tenant_id = ? AND show_id = ?",
            (tenant_id, show_id),
        )
    }
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for slot in slots:
        key = (str(slot["episode_id"]), str(slot["slot_type"]))
        merged[key] = {
            "slot_id": slot["id"],
            "episode_id": slot["episode_id"],
            "slot_type": slot["slot_type"],
            "rate_minor": int(slot["rate_minor"]),
            "currency": slot.get("rate_currency") or "USD",
            "committed": bool(slot["committed"]),
            "declared": True,
            "status": "available",
            "sponsor_id": None,
            "sponsor_name": None,
            "sponsorship_id": None,
            "sold_at": None,
        }
    for assignment in assignments:
        key = (str(assignment["episode_id"]), str(assignment["slot_type"]))
        entry = merged.get(key)
        if entry is None:
            entry = {
                "slot_id": None,
                "episode_id": assignment["episode_id"],
                "slot_type": assignment["slot_type"],
                "rate_minor": int(assignment["rate_minor"]),
                "currency": assignment.get("rate_currency") or "USD",
                "committed": False,
                "declared": False,
                "status": "available",
                "sponsor_id": None,
                "sponsor_name": None,
                "sponsorship_id": None,
                "sold_at": None,
            }
            merged[key] = entry
        sponsor = names.get(str(assignment["sponsor_id"]))
        entry["status"] = str(assignment["status"])
        entry["sponsor_id"] = assignment["sponsor_id"]
        entry["sponsor_name"] = sponsor["name"] if sponsor else None
        entry["sponsorship_id"] = assignment["id"]
        entry["rate_minor"] = int(assignment["rate_minor"])
        entry["sold_at"] = assignment.get("sold_at")
    return [merged[key] for key in sorted(merged)]


def list_slots(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return every slot in scope with its sold/available state."""
    _require_role(actor_role, READ_ROLES, "read sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    return _inventory_rows(conn, tenant_id=tenant, show_id=show, episode_id=episode_id)


def assign_slot(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    slot_type: str,
    sponsor_id: str,
    actor_id: str,
    actor_role: str,
    status: str = "sold",
    rate_minor: int | None = None,
    policy: configuration.SponsorInventoryPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assign one declared slot to one sponsor as ``reserved`` or ``sold``."""
    role = _require_role(actor_role, WRITE_ROLES, "sell sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    episode = platform._ref(episode_id, "episode_id")
    resolved = _policy(show, policy)
    selected_type = _slot_type(slot_type, resolved)
    assignment_status = _assignment_status(status)
    sponsor = _sponsor_row(conn, tenant_id=tenant, show_id=show, sponsor_id=sponsor_id)
    if sponsor["status"] != "active":
        raise SponsorError("sponsor is not active in this show", 409)
    slot = store.fetch_one(
        conn,
        "SELECT * FROM sponsor_slots WHERE tenant_id = ? AND show_id = ? AND episode_id = ? AND slot_type = ?",
        (tenant, show, episode, selected_type),
    )
    if slot is None:
        raise SponsorError("declare the episode ad inventory before selling a slot", 404)
    rate = int(slot["rate_minor"]) if rate_minor is None else _rate_minor(rate_minor)
    timestamp = _timestamp(now)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM sponsorships WHERE tenant_id = ? AND show_id = ? AND episode_id = ? AND slot_type = ?",
        (tenant, show, episode, selected_type),
    )
    if existing is not None and str(existing["sponsor_id"]) != str(sponsor["id"]):
        raise SponsorError("slot is already held by another sponsor", 409)
    if existing is not None:
        store.update(
            conn,
            "sponsorships",
            existing["id"],
            {
                "status": assignment_status,
                "rate_minor": rate,
                "rate_currency": resolved.currency,
                "slot_id": slot["id"],
                "sold_at": timestamp if assignment_status == "sold" else None,
                "updated_at": timestamp,
                "recorded_by": actor,
                "recorded_by_role": role,
            },
        )
        record_id = str(existing["id"])
    else:
        record_id = generation.new_id("sponsorship")
        store.insert(
            conn,
            "sponsorships",
            {
                "id": record_id,
                "tenant_id": tenant,
                "show_id": show,
                "sponsor_id": sponsor["id"],
                "episode_id": episode,
                "slot_type": selected_type,
                "slot_id": slot["id"],
                "rate_minor": rate,
                "rate_currency": resolved.currency,
                "status": assignment_status,
                "sold_at": timestamp if assignment_status == "sold" else None,
                "recorded_by": actor,
                "recorded_by_role": role,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        event_type="sponsorship.slot_assigned",
        subject_ref=f"sponsorship://{record_id}",
        actor_id=actor,
        actor_role=role,
        episode_id=episode,
        sponsor_id=str(sponsor["id"]),
        details={
            "slot_type": selected_type,
            "status": assignment_status,
            "rate_minor": rate,
        },
        now=now,
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM sponsorships WHERE id = ?", (record_id,)) or {}


def release_slot(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    slot_type: str,
    actor_id: str,
    actor_role: str,
    policy: configuration.SponsorInventoryPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one slot to available inventory and keep the release attributable."""
    role = _require_role(actor_role, WRITE_ROLES, "sell sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    episode = platform._ref(episode_id, "episode_id")
    resolved = _policy(show, policy)
    selected_type = _slot_type(slot_type, resolved)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM sponsorships WHERE tenant_id = ? AND show_id = ? AND episode_id = ? AND slot_type = ?",
        (tenant, show, episode, selected_type),
    )
    if existing is None:
        raise SponsorError("no sponsorship holds this slot", 404)
    conn.execute("DELETE FROM sponsorships WHERE id = ?", (existing["id"],))
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        event_type="sponsorship.slot_released",
        subject_ref=f"sponsorship://{existing['id']}",
        actor_id=actor,
        actor_role=role,
        episode_id=episode,
        sponsor_id=str(existing["sponsor_id"]),
        details={"slot_type": selected_type, "released_status": existing["status"]},
        now=now,
    )
    conn.commit()
    return {
        "episode_id": episode,
        "slot_type": selected_type,
        "status": "available",
        "released_sponsorship_id": existing["id"],
    }


# ---------------------------------------------------------------------------
# Revenue, export, and the publication gate.
# ---------------------------------------------------------------------------


def _unapproved_claim_sponsors(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str) -> dict[str, int]:
    rows = store.fetch_all(
        conn,
        "SELECT sponsor_id, COUNT(*) AS count FROM sponsor_claims "
        "WHERE tenant_id = ? AND show_id = ? AND approved_for_external_use = 0 "
        "GROUP BY sponsor_id",
        (tenant_id, show_id),
    )
    return {str(row["sponsor_id"]): int(row["count"]) for row in rows}


def publication_blockers(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    policy: configuration.SponsorInventoryPolicy | None = None,
) -> list[dict[str, Any]]:
    """Return the sponsor reasons this episode may not publish.

    Two configurable rules: a **committed** slot that is not sold breaks a
    promise the show already made, and a sold sponsor whose factual claims are
    not yet approved for external use would put an unverified claim on air.
    """
    tenant, show = _scope(tenant_id, show_id)
    episode = platform._ref(episode_id, "episode_id")
    resolved = _policy(show, policy)
    blockers: list[dict[str, Any]] = []
    rows = _inventory_rows(conn, tenant_id=tenant, show_id=show, episode_id=episode)
    if resolved.block_publication_on_unfilled_committed_slots:
        for row in rows:
            if row["committed"] and row["status"] != "sold":
                blockers.append(
                    {
                        "kind": "sponsor_slot_unfilled",
                        "status": "unfilled",
                        "episode_id": episode,
                        "slot_type": row["slot_type"],
                        "slot_id": row["slot_id"],
                        "detail": (f"committed {row['slot_type']} slot is {row['status']}, not sold"),
                    }
                )
    if resolved.require_approved_claims_before_publication:
        unapproved = _unapproved_claim_sponsors(conn, tenant_id=tenant, show_id=show)
        seen: set[str] = set()
        for row in rows:
            sponsor_id = row["sponsor_id"]
            if row["status"] != "sold" or sponsor_id is None:
                continue
            key = str(sponsor_id)
            if key in seen or key not in unapproved:
                continue
            seen.add(key)
            blockers.append(
                {
                    "kind": "sponsor_claim_unapproved",
                    "status": "unapproved",
                    "episode_id": episode,
                    "sponsor_id": key,
                    "sponsor_name": row["sponsor_name"],
                    "unapproved_claim_count": unapproved[key],
                    "detail": (f"{unapproved[key]} sponsor claim(s) are not approved for external use"),
                }
            )
    return blockers


def revenue_report(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
    policy: configuration.SponsorInventoryPolicy | None = None,
) -> dict[str, Any]:
    """Return the Revenue view: per episode, per sponsor, sold/total and money."""
    _require_role(actor_role, READ_ROLES, "read sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    resolved = _policy(show, policy)
    rows = _inventory_rows(conn, tenant_id=tenant, show_id=show, episode_id=episode_id)
    unapproved = _unapproved_claim_sponsors(conn, tenant_id=tenant, show_id=show)
    episodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        episode = str(row["episode_id"])
        entry = episodes.setdefault(
            episode,
            {
                "episode_id": episode,
                "currency": resolved.currency,
                "slots_total": 0,
                "slots_sold": 0,
                "slots_reserved": 0,
                "slots_available": 0,
                "committed_slots": 0,
                "committed_unfilled": 0,
                "revenue_minor": 0,
                "sponsors": {},
            },
        )
        entry["slots_total"] += 1
        if row["committed"]:
            entry["committed_slots"] += 1
        if row["status"] == "sold":
            entry["slots_sold"] += 1
            entry["revenue_minor"] += int(row["rate_minor"])
        elif row["status"] == "reserved":
            entry["slots_reserved"] += 1
        else:
            entry["slots_available"] += 1
        if row["committed"] and row["status"] != "sold":
            entry["committed_unfilled"] += 1
        sponsor_id = row["sponsor_id"]
        if sponsor_id is None:
            continue
        sponsor_entry = entry["sponsors"].setdefault(
            str(sponsor_id),
            {
                "sponsor_id": str(sponsor_id),
                "sponsor_name": row["sponsor_name"],
                "slots_held": 0,
                "slots_sold": 0,
                "revenue_minor": 0,
                "unapproved_claim_count": unapproved.get(str(sponsor_id), 0),
            },
        )
        sponsor_entry["slots_held"] += 1
        if row["status"] == "sold":
            sponsor_entry["slots_sold"] += 1
            sponsor_entry["revenue_minor"] += int(row["rate_minor"])
    episode_rows: list[dict[str, Any]] = []
    for episode in sorted(episodes):
        entry = episodes[episode]
        sponsors = list(entry["sponsors"].values())
        sponsors.sort(key=lambda item: (str(item["sponsor_name"] or ""), item["sponsor_id"]))
        entry["sponsors"] = sponsors
        entry["publication_blockers"] = publication_blockers(
            conn,
            tenant_id=tenant,
            show_id=show,
            episode_id=episode,
            policy=resolved,
        )
        entry["publication_blocked"] = bool(entry["publication_blockers"])
        episode_rows.append(entry)
    totals = {
        "episodes": len(episode_rows),
        "slots_total": sum(item["slots_total"] for item in episode_rows),
        "slots_sold": sum(item["slots_sold"] for item in episode_rows),
        "slots_reserved": sum(item["slots_reserved"] for item in episode_rows),
        "slots_available": sum(item["slots_available"] for item in episode_rows),
        "committed_unfilled": sum(item["committed_unfilled"] for item in episode_rows),
        "revenue_minor": sum(item["revenue_minor"] for item in episode_rows),
        "blocked_episodes": sum(1 for item in episode_rows if item["publication_blocked"]),
    }
    return {
        "tenant_id": tenant,
        "show_id": show,
        "currency": resolved.currency,
        "policy": {
            "block_publication_on_unfilled_committed_slots": (resolved.block_publication_on_unfilled_committed_slots),
            "require_approved_claims_before_publication": (resolved.require_approved_claims_before_publication),
            "source": resolved.source,
        },
        "episodes": episode_rows,
        "totals": totals,
    }


def _csv_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _major_units(rate_minor: int) -> str:
    sign = "-" if rate_minor < 0 else ""
    absolute = abs(int(rate_minor))
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def accounting_csv(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
) -> str:
    """Render the accounting export: Episode, Sponsor, Slot, Rate, Date."""
    _require_role(actor_role, READ_ROLES, "read sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    rows = _inventory_rows(conn, tenant_id=tenant, show_id=show, episode_id=episode_id)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        if row["status"] != "sold":
            continue
        sold_at = str(row.get("sold_at") or "")
        writer.writerow(
            [
                _csv_cell(row["episode_id"]),
                _csv_cell(row["sponsor_name"] or row["sponsor_id"]),
                _csv_cell(row["slot_type"]),
                _csv_cell(_major_units(int(row["rate_minor"]))),
                _csv_cell(sold_at[:10]),
            ]
        )
    return output.getvalue()


def list_receipts(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the newest-first attributable sponsorship receipt trail."""
    _require_role(actor_role, READ_ROLES, "read sponsor inventory")
    tenant, show = _scope(tenant_id, show_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise SponsorError("limit must be an integer between 1 and 500")
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(platform._ref(episode_id, "episode_id"))
    params.append(limit)
    return store.fetch_all(
        conn,
        "SELECT * FROM sponsorship_receipts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    )


__all__ = [
    "ASSIGNMENT_STATUSES",
    "CLAIM_APPROVAL_ROLES",
    "CSV_COLUMNS",
    "MAX_RATE_MINOR",
    "READ_ROLES",
    "SLOT_TYPES",
    "SPONSOR_STATUSES",
    "SponsorError",
    "WRITE_ROLES",
    "accounting_csv",
    "approve_claim",
    "assign_slot",
    "create_sponsor",
    "declare_slots",
    "list_claims",
    "list_receipts",
    "list_slots",
    "list_sponsors",
    "publication_blockers",
    "record_claim",
    "release_slot",
    "revenue_report",
    "set_sponsor_status",
]
