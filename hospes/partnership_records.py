"""Revisioned partnership writes, links, reviews, and pilot selection."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping
from . import generation, privacy, store
from .partnerships import (
    PILOT_SLOT_LABELS,
    PILOT_SLOT_INELIGIBLE_STATES,
    RESOURCE_TYPES,
    REVIEW_KINDS,
    PartnershipError,
    PartnershipItemInput,
    _OPAQUE_REFERENCE,
    _audit,
    _bounded,
    _item_id,
    _item_values,
    _now_iso,
    _partnership,
    _record_revision,
)

def _require_owner_role(actor_role: str) -> None:
    if actor_role not in {"producer", "editorial_owner", "relationship_owner"}:
        raise PartnershipError(403, "partnership updates require an explicit owner role")


def create_item(
    conn: sqlite3.Connection,
    partnership_id: str,
    payload: PartnershipItemInput,
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    partnership = _partnership(conn, partnership_id, tenant_id)
    _require_owner_role(actor_role)
    timestamp = _now_iso()
    existing = store.fetch_one(
        conn,
        "SELECT * FROM partnership_items WHERE partnership_id = ? AND item_key = ?",
        (partnership_id, payload.item_key),
    )
    if existing is not None:
        raise PartnershipError(409, "item_key already exists; use revision-checked update")
    item_id = _item_id(
        tenant_id,
        str(partnership["show_id"]),
        str(partnership.get("template_key") or partnership["partnership_key"]),
        payload.item_key,
    )
    record = {
        "id": item_id,
        "tenant_id": tenant_id,
        "show_id": partnership["show_id"],
        "partnership_id": partnership_id,
        "item_key": payload.item_key,
        **_item_values(payload),
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    store.insert(conn, "partnership_items", record)
    _record_revision(
        conn, record, change_kind="created", actor_id=actor_id,
        actor_role=actor_role, timestamp=timestamp,
    )
    _audit(
        conn, partnership_id, tenant_id, actor_id, actor_role,
        "partnership.item_created", {"item_key": payload.item_key, "revision": 1}, timestamp,
    )
    store.update(conn, "partnerships", partnership_id, {"updated_at": timestamp})
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM partnership_items WHERE id = ?", (item_id,))


def update_item(
    conn: sqlite3.Connection,
    partnership_id: str,
    item_id: str,
    payload: PartnershipItemInput,
    *,
    expected_revision: int,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    _partnership(conn, partnership_id, tenant_id)
    _require_owner_role(actor_role)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM partnership_items WHERE id = ? AND partnership_id = ? AND tenant_id = ?",
        (item_id, partnership_id, tenant_id),
    )
    if existing is None:
        raise PartnershipError(404, "partnership item not found")
    if existing["state"] == "superseded":
        raise PartnershipError(409, "superseded items are immutable")
    if payload.item_key != existing["item_key"]:
        raise PartnershipError(422, "item_key cannot change across revisions")
    if not isinstance(expected_revision, int) or expected_revision < 1:
        raise PartnershipError(422, "expected_revision must be a positive integer")
    if int(existing["revision"]) != expected_revision:
        raise PartnershipError(409, "stale partnership item revision")
    timestamp = _now_iso()
    next_revision = expected_revision + 1
    store.update(conn, "partnership_items", item_id, {
        **_item_values(payload),
        "revision": next_revision,
        "updated_at": timestamp,
    })
    revised = store.fetch_one(conn, "SELECT * FROM partnership_items WHERE id = ?", (item_id,))
    _record_revision(
        conn, revised, change_kind="updated", actor_id=actor_id,
        actor_role=actor_role, timestamp=timestamp,
    )
    _audit(
        conn, partnership_id, tenant_id, actor_id, actor_role,
        "partnership.item_revised",
        {"item_key": payload.item_key, "revision": next_revision},
        timestamp,
    )
    store.update(conn, "partnerships", partnership_id, {"updated_at": timestamp})
    conn.commit()
    return revised


def supersede_item(
    conn: sqlite3.Connection,
    partnership_id: str,
    item_id: str,
    *,
    expected_revision: int,
    successor_item_id: str,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    _partnership(conn, partnership_id, tenant_id)
    _require_owner_role(actor_role)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM partnership_items WHERE id = ? AND partnership_id = ? AND tenant_id = ?",
        (item_id, partnership_id, tenant_id),
    )
    successor = store.fetch_one(
        conn,
        "SELECT * FROM partnership_items WHERE id = ? AND partnership_id = ? AND tenant_id = ?",
        (successor_item_id, partnership_id, tenant_id),
    )
    if existing is None or successor is None:
        raise PartnershipError(404, "item or successor not found")
    if item_id == successor_item_id:
        raise PartnershipError(422, "an item cannot supersede itself")
    if successor["state"] == "superseded":
        raise PartnershipError(409, "successor item is already superseded")
    if existing["state"] == "superseded":
        raise PartnershipError(409, "item is already superseded")
    if int(existing["revision"]) != expected_revision:
        raise PartnershipError(409, "stale partnership item revision")
    timestamp = _now_iso()
    next_revision = expected_revision + 1
    store.update(conn, "partnership_items", item_id, {
        "state": "superseded",
        "revision": next_revision,
        "superseded_at": timestamp,
        "superseded_by_item_id": successor_item_id,
        "updated_at": timestamp,
    })
    revised = store.fetch_one(conn, "SELECT * FROM partnership_items WHERE id = ?", (item_id,))
    _record_revision(
        conn, revised, change_kind="superseded", actor_id=actor_id,
        actor_role=actor_role, timestamp=timestamp,
    )
    _audit(
        conn, partnership_id, tenant_id, actor_id, actor_role,
        "partnership.item_superseded",
        {"item_key": existing["item_key"], "successor_item_id": successor_item_id},
        timestamp,
    )
    conn.commit()
    return revised


def link_resource(
    conn: sqlite3.Connection,
    partnership_id: str,
    payload: Mapping[str, Any],
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    partnership = _partnership(conn, partnership_id, tenant_id)
    _require_owner_role(actor_role)
    resource_type = _bounded(payload, "resource_type", 2, 40)
    if resource_type not in RESOURCE_TYPES:
        raise PartnershipError(422, f"resource_type must be one of {sorted(RESOURCE_TYPES)}")
    reference = _bounded(payload, "resource_reference", 4, 240)
    if not _OPAQUE_REFERENCE.fullmatch(reference) or privacy.private_text_kind(reference):
        raise PartnershipError(422, "resource_reference must be a safe opaque reference")
    label = _bounded(payload, "label", 2, 120)
    if privacy.private_text_kind(label):
        raise PartnershipError(422, "resource label contains private content")
    item_id = payload.get("item_id")
    if item_id is not None:
        item = store.fetch_one(
            conn,
            "SELECT id FROM partnership_items WHERE id = ? AND partnership_id = ?",
            (item_id, partnership_id),
        )
        if item is None:
            raise PartnershipError(404, "linked partnership item not found")
    existing = store.fetch_one(
        conn,
        "SELECT * FROM partnership_resource_links WHERE partnership_id = ? "
        "AND item_id IS ? AND resource_type = ? AND resource_reference = ?",
        (partnership_id, item_id, resource_type, reference),
    )
    if existing is not None:
        return existing
    timestamp = _now_iso()
    record = {
        "id": generation.new_id("partnership_resource_link"),
        "tenant_id": tenant_id,
        "show_id": partnership["show_id"],
        "partnership_id": partnership_id,
        "item_id": item_id,
        "resource_type": resource_type,
        "resource_reference": reference,
        "label": label,
        "created_by": actor_id,
        "created_role": actor_role,
        "created_at": timestamp,
    }
    store.insert(conn, "partnership_resource_links", record)
    _audit(
        conn, partnership_id, tenant_id, actor_id, actor_role,
        "partnership.resource_linked", {"resource_type": resource_type, "label": label},
        timestamp,
    )
    conn.commit()
    return record


def record_review(
    conn: sqlite3.Connection,
    partnership_id: str,
    payload: Mapping[str, Any],
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    partnership = _partnership(conn, partnership_id, tenant_id)
    review_kind = _bounded(payload, "review_kind", 2, 40)
    if review_kind not in REVIEW_KINDS:
        raise PartnershipError(422, f"review_kind must be one of {sorted(REVIEW_KINDS)}")
    if review_kind == "ari_review" and actor_role != "relationship_owner":
        raise PartnershipError(403, "ari_review requires the relationship owner")
    decisions_count = payload.get("decisions_count")
    coverage_met = payload.get("coverage_met")
    coverage_total = payload.get("coverage_total")
    if not all(type(value) is int and 0 <= value <= 100 for value in (
        decisions_count, coverage_met, coverage_total
    )):
        raise PartnershipError(422, "review counts must be integers between 0 and 100")
    if coverage_met > coverage_total:
        raise PartnershipError(422, "coverage_met cannot exceed coverage_total")
    reference = payload.get("external_reference")
    if reference is not None and (
        not isinstance(reference, str)
        or not _OPAQUE_REFERENCE.fullmatch(reference)
        or privacy.private_text_kind(reference)
    ):
        raise PartnershipError(422, "external_reference must be a safe opaque reference")
    if review_kind in {"technical_rehearsal", "pilot_scorecard"} and not reference:
        raise PartnershipError(
            422, f"{review_kind} requires an opaque external evidence reference"
        )
    occurred_raw = payload.get("occurred_at")
    try:
        occurred = datetime.fromisoformat(str(occurred_raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PartnershipError(422, "occurred_at must be an ISO timestamp") from exc
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise PartnershipError(422, "occurred_at must include a timezone")
    current = (now or generation.now()).astimezone(timezone.utc)
    occurred = occurred.astimezone(timezone.utc)
    if occurred > current:
        raise PartnershipError(422, "review time cannot be in the future")
    timestamp = _now_iso(current)
    record = {
        "id": generation.new_id("partnership_review"),
        "tenant_id": tenant_id,
        "show_id": partnership["show_id"],
        "partnership_id": partnership_id,
        "review_kind": review_kind,
        "decisions_count": decisions_count,
        "coverage_met": coverage_met,
        "coverage_total": coverage_total,
        "external_reference": reference,
        "reviewer_id": actor_id,
        "reviewer_role": actor_role,
        "occurred_at": occurred.isoformat(),
        "created_at": timestamp,
    }
    store.insert(conn, "partnership_reviews", record)
    _audit(
        conn, partnership_id, tenant_id, actor_id, actor_role,
        "partnership.review_recorded",
        {"review_kind": review_kind, "decisions_count": decisions_count}, timestamp,
    )
    from . import pilot_service

    try:
        pilot_service.apply_partnership_review_evidence(
            conn,
            partnership_id,
            tenant_id=tenant_id,
            review_kind=review_kind,
            actor_id=actor_id,
            actor_role=actor_role,
            occurred_at=occurred,
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return record


def record_informal_touchpoint(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    tenant_id: str,
    show_id: str,
    actor_id: str,
    actor_role: str,
    field_vault: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record an encrypted conversation note without advancing Pilot state."""
    from . import touchpoints

    parsed = touchpoints.TouchpointInput.from_mapping(payload, now=now)
    return touchpoints.record_touchpoint(
        conn,
        field_vault,
        parsed,
        tenant_id=tenant_id,
        show_id=show_id,
        actor_id=actor_id,
        actor_role=actor_role,
        now=now,
    )


def select_pilot_candidate(
    conn: sqlite3.Connection,
    partnership_id: str,
    opportunity_id: str,
    slot: int,
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    partnership = _partnership(conn, partnership_id, tenant_id)
    if actor_role not in {"host", "editorial_owner", "relationship_owner"}:
        raise PartnershipError(403, "candidate selection requires a host or explicit owner")
    if slot not in PILOT_SLOT_LABELS:
        raise PartnershipError(422, "slot must be 1 (primary), 2, or 3")
    opportunity = store.fetch_one(
        conn,
        "SELECT * FROM appearance_opportunities WHERE id = ? AND tenant_id = ? "
        "AND show_id = ?",
        (opportunity_id, tenant_id, partnership["show_id"]),
    )
    if opportunity is None:
        raise PartnershipError(404, "opportunity not found in this tenant")
    if opportunity["disposition"] != "APPROVED":
        raise PartnershipError(409, "only an approved candidate may enter the pilot slate")
    if opportunity["status"] in PILOT_SLOT_INELIGIBLE_STATES:
        raise PartnershipError(
            409, f"a candidate in {opportunity['status']} cannot enter the pilot slate"
        )
    if opportunity["relationship_class"] in {"C4", "C5"}:
        raise PartnershipError(409, "C4/C5 candidates cannot enter the pilot slate")
    timestamp = _now_iso()
    conn.execute("SAVEPOINT pilot_candidate_selection")
    try:
        conn.execute(
            "DELETE FROM pilot_candidate_slots WHERE partnership_id = ? "
            "AND (slot = ? OR opportunity_id = ?)",
            (partnership_id, slot, opportunity_id),
        )
        record = {
            "id": generation.new_id("pilot_candidate_slot"),
            "tenant_id": tenant_id,
            "show_id": partnership["show_id"],
            "partnership_id": partnership_id,
            "opportunity_id": opportunity_id,
            "slot": slot,
            "selected_by": actor_id,
            "selected_role": actor_role,
            "selected_at": timestamp,
        }
        store.insert(conn, "pilot_candidate_slots", record)
        _audit(
            conn, partnership_id, tenant_id, actor_id, actor_role,
            "pilot.candidate_selected",
            {"opportunity_id": opportunity_id, "slot": PILOT_SLOT_LABELS[slot]}, timestamp,
        )
        conn.execute("RELEASE SAVEPOINT pilot_candidate_selection")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT pilot_candidate_selection")
        conn.execute("RELEASE SAVEPOINT pilot_candidate_selection")
        raise
    return {**record, "slot_label": PILOT_SLOT_LABELS[slot]}


# Compatibility alias for older direct callers. Existing keys still fail closed
# rather than being overwritten.
upsert_item = create_item
