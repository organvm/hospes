"""Reusable partnership command-center registry.

The registry holds bounded operating summaries and opaque owner references. It
never stores contracts, bank details, correspondence, private notes, or full
deal terms. One schema serves Ari + Anthony and any future partnership tenant.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

import yaml

from . import generation, privacy, store

CATEGORIES = (
    "engine",
    "plan",
    "role",
    "agreement",
    "deal",
    "obligation",
    "decision",
    "receipt",
    "risk",
    "unknown",
)
STATES = (
    "current",
    "planned",
    "agreed",
    "in_progress",
    "blocked",
    "done",
    "unknown",
)
READABLE_STATES = (*STATES, "superseded")
PARTNERSHIP_STATUSES = {"active", "paused", "complete"}
RESOURCE_TYPES = {
    "opportunity",
    "commitment",
    "operational_receipt",
    "issue",
    "capability",
    "external_owner",
}
REVIEW_KINDS = {
    "ari_review",
    "partnership_review",
    "technical_rehearsal",
    "pilot_scorecard",
}
PILOT_SLOT_LABELS = {1: "primary", 2: "backup_1", 3: "backup_2"}
PILOT_SLOT_INELIGIBLE_STATES = frozenset({
    "DECLINED",
    "PAUSED",
    "REVISIT_LATER",
    "DUPLICATE",
    "DO_NOT_CONTACT",
    "LEGAL_REVIEW",
    "NEEDS_HUMAN",
    "CANCELLED",
})
_BOUNDARY_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
_ITEM_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{1,119}$")
_OPAQUE_REFERENCE = re.compile(
    r"^[a-z][a-z0-9_-]{1,31}://[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+-]{1,207}$"
)
_PRIVATE_FIELDS = {
    "contract_body",
    "correspondence_body",
    "private_notes",
    "bank_details",
    "account_number",
    "routing_number",
    "tax_id",
    "signature",
}


class PartnershipError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class PartnershipItemInput:
    item_key: str
    category: str
    title: str
    summary: str
    owner: str
    state: str
    external_reference: str | None = None
    due_at: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PartnershipItemInput":
        for field_name in _PRIVATE_FIELDS:
            if value.get(field_name):
                raise PartnershipError(
                    422, f"{field_name} must remain in the external agreement or deal owner"
                )
        item_key = _bounded(value, "item_key", 2, 120)
        if not _ITEM_KEY.fullmatch(item_key):
            raise PartnershipError(422, "item_key must be a bounded lower-case identifier")
        category = _bounded(value, "category", 2, 40)
        if category not in CATEGORIES:
            raise PartnershipError(422, f"category must be one of {list(CATEGORIES)}")
        state = _bounded(value, "state", 2, 40)
        if state not in STATES:
            raise PartnershipError(422, f"state must be one of {list(STATES)}")
        external_reference = value.get("external_reference")
        if external_reference is not None:
            if not isinstance(external_reference, str) or not _OPAQUE_REFERENCE.fullmatch(
                external_reference
            ):
                raise PartnershipError(
                    422, "external_reference must be an opaque owner reference"
                )
        if category in {"agreement", "deal", "receipt"} and not external_reference:
            raise PartnershipError(
                422, f"{category} items require an opaque external owner reference"
            )
        due_at = value.get("due_at")
        if due_at is not None:
            if isinstance(due_at, datetime):
                due_at = due_at.date().isoformat()
            elif isinstance(due_at, date):
                due_at = due_at.isoformat()
            elif not isinstance(due_at, str):
                raise PartnershipError(422, "due_at must be an ISO date")
            try:
                date.fromisoformat(due_at)
            except ValueError as exc:
                raise PartnershipError(422, "due_at must be an ISO date") from exc
        title = _bounded(value, "title", 2, 160)
        summary = _bounded(value, "summary", 2, 1000)
        owner = _bounded(value, "owner", 2, 120)
        for field_name, text in (
            ("title", title),
            ("summary", summary),
            ("owner", owner),
            ("external_reference", external_reference or ""),
        ):
            private_kind = privacy.private_text_kind(text)
            if private_kind:
                raise PartnershipError(
                    422, f"{field_name} contains {private_kind} that must remain external"
                )
        return cls(
            item_key=item_key,
            category=category,
            title=title,
            summary=summary,
            owner=owner,
            state=state,
            external_reference=external_reference,
            due_at=due_at,
        )


@dataclass
class PartnershipImportResult:
    partnership_id: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    item_ids: list[str] = field(default_factory=list)
    template_digest: str = ""
    outcome: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "partnership_id": self.partnership_id,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "item_ids": self.item_ids,
            "template_digest": self.template_digest,
            "outcome": self.outcome,
        }


def _bounded(value: Mapping[str, Any], key: str, minimum: int, maximum: int) -> str:
    raw = value.get(key)
    if not isinstance(raw, str):
        raise PartnershipError(422, f"{key} must be a string")
    normalized = raw.strip()
    if not minimum <= len(normalized) <= maximum:
        raise PartnershipError(422, f"{key} must be {minimum}-{maximum} chars")
    return normalized


def _now_iso(now: datetime | None = None) -> str:
    return (now or generation.now()).astimezone(timezone.utc).isoformat()


def _partnership_id(tenant_id: str, show_id: str, partnership_key: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:partnership:{tenant_id}:{show_id}:{partnership_key}",
        )
    )


def _item_id(
    tenant_id: str, show_id: str, partnership_key: str, item_key: str
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:partnership-item:{tenant_id}:{show_id}:{partnership_key}:{item_key}",
        )
    )


def _audit(
    conn: sqlite3.Connection,
    partnership_id: str,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    event_type: str,
    details: dict[str, Any],
    timestamp: str,
) -> None:
    partnership = store.fetch_one(
        conn,
        "SELECT show_id FROM partnerships WHERE id = ? AND tenant_id = ?",
        (partnership_id, tenant_id),
    )
    show_id = str(partnership["show_id"]) if partnership else "legacy"
    store.insert(conn, "partnership_audit_events", {
        "id": generation.new_id("partnership_audit_event"),
        "tenant_id": tenant_id,
        "show_id": show_id,
        "partnership_id": partnership_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "details": details,
        "created_at": timestamp,
    })


def _validate_boundary(value: str, label: str) -> None:
    if not _BOUNDARY_ID.fullmatch(value):
        raise PartnershipError(422, f"{label} must be an opaque lower-case id")


def _item_values(payload: PartnershipItemInput) -> dict[str, Any]:
    return {
        "category": payload.category,
        "title": payload.title,
        "summary": payload.summary,
        "owner": payload.owner,
        "state": payload.state,
        "external_reference": payload.external_reference,
        "due_at": payload.due_at,
    }


def _record_revision(
    conn: sqlite3.Connection,
    item: Mapping[str, Any],
    *,
    change_kind: str,
    actor_id: str,
    actor_role: str,
    timestamp: str,
) -> None:
    store.insert(conn, "partnership_item_revisions", {
        "id": f"{item['id']}:r{item['revision']}",
        "tenant_id": item["tenant_id"],
        "show_id": item.get("show_id", "legacy"),
        "partnership_id": item["partnership_id"],
        "item_id": item["id"],
        "revision": item["revision"],
        "item_key": item["item_key"],
        "category": item["category"],
        "title": item["title"],
        "summary": item["summary"],
        "owner": item["owner"],
        "state": item["state"],
        "external_reference": item.get("external_reference"),
        "due_at": item.get("due_at"),
        "superseded_by_item_id": item.get("superseded_by_item_id"),
        "change_kind": change_kind,
        "changed_by": actor_id,
        "changed_role": actor_role,
        "created_at": timestamp,
    })


def _template_digest(raw: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: value.isoformat()
        if isinstance(value, (date, datetime))
        else str(value),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_template(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    tenant_id: str,
    show_id: str = "legacy",
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> PartnershipImportResult:
    """Import one versioned template without overwriting live decisions.

    The first import creates the registry.  An unchanged digest is a no-op.  A
    changed template returns 409 so a human must revise or supersede live items
    explicitly instead of silently replacing their history.
    """
    for value, label in (
        (tenant_id, "tenant_id"),
        (show_id, "show_id"),
        (actor_id, "actor_id"),
        (actor_role, "actor_role"),
    ):
        _validate_boundary(value, label)
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PartnershipError(422, f"partnership template is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise PartnershipError(422, "partnership template must declare version 1")
    partnership = raw.get("partnership")
    raw_items = raw.get("items")
    if not isinstance(partnership, dict) or not isinstance(raw_items, list):
        raise PartnershipError(422, "template requires partnership and items")
    partnership_key = _bounded(partnership, "key", 2, 80)
    _validate_boundary(partnership_key, "partnership key")
    status = _bounded(partnership, "status", 2, 40)
    if status not in PARTNERSHIP_STATUSES:
        raise PartnershipError(422, "partnership status must be active, paused, or complete")
    label = _bounded(partnership, "label", 2, 160)
    purpose = _bounded(partnership, "purpose", 2, 1000)
    for field_name, text in (("label", label), ("purpose", purpose)):
        private_kind = privacy.private_text_kind(text)
        if private_kind:
            raise PartnershipError(
                422, f"partnership {field_name} contains {private_kind} that must remain external"
            )
    items = [
        PartnershipItemInput.from_dict(item)
        for item in raw_items
        if isinstance(item, dict)
    ]
    if len(items) != len(raw_items):
        raise PartnershipError(422, "every partnership item must be an object")
    keys = [item.item_key for item in items]
    if len(keys) != len(set(keys)):
        raise PartnershipError(422, "partnership item keys must be unique")

    digest = _template_digest(raw)
    partnership_id = _partnership_id(tenant_id, show_id, partnership_key)
    scoped_partnership_key = f"{show_id}__{partnership_key}"
    timestamp = _now_iso(now)
    result = PartnershipImportResult(
        partnership_id=partnership_id,
        template_digest=digest,
    )
    conn.execute("SAVEPOINT partnership_import")
    try:
        existing_partnership = store.fetch_one(
            conn,
            "SELECT * FROM partnerships WHERE tenant_id = ? AND show_id = ? "
            "AND template_key = ?",
            (tenant_id, show_id, partnership_key),
        )
        partnership_values = {
            "label": label,
            "purpose": purpose,
            "status": status,
        }
        if existing_partnership is None:
            store.insert(conn, "partnerships", {
                "id": partnership_id,
                "tenant_id": tenant_id,
                "show_id": show_id,
                "partnership_key": scoped_partnership_key,
                "template_key": partnership_key,
                **partnership_values,
                "created_at": timestamp,
                "updated_at": timestamp,
            })
            _audit(
                conn, partnership_id, tenant_id, actor_id, actor_role,
                "partnership.created", {"partnership_key": partnership_key}, timestamp,
            )
            result.outcome = "created"
        else:
            partnership_id = existing_partnership["id"]
            known_import = store.fetch_one(
                conn,
                "SELECT * FROM partnership_template_imports "
                "WHERE partnership_id = ? AND template_digest = ?",
                (partnership_id, digest),
            )
            current_items = store.fetch_all(
                conn,
                "SELECT * FROM partnership_items WHERE partnership_id = ? "
                "AND state != 'superseded' ORDER BY item_key",
                (partnership_id,),
            )
            expected = {item.item_key: _item_values(item) for item in items}
            matches_live = (
                all(existing_partnership[key] == value for key, value in partnership_values.items())
                and len(current_items) == len(items)
                and all(
                    row["item_key"] in expected
                    and all(row[key] == value for key, value in expected[row["item_key"]].items())
                    for row in current_items
                )
            )
            if known_import is not None:
                result.outcome = "unchanged"
                result.unchanged = len(items)
                result.item_ids = [row["id"] for row in current_items]
                conn.execute("RELEASE SAVEPOINT partnership_import")
                return result
            prior_import = store.fetch_one(
                conn,
                "SELECT id FROM partnership_template_imports WHERE partnership_id = ? LIMIT 1",
                (partnership_id,),
            )
            if prior_import is not None or not matches_live:
                raise PartnershipError(
                    409,
                    "template differs from live partnership state; revise or supersede items explicitly",
                )
            result.outcome = "adopted"
            result.unchanged = len(items)
            result.item_ids = [row["id"] for row in current_items]

        result.partnership_id = partnership_id
        if existing_partnership is None:
            for item in items:
                item_id = _item_id(
                    tenant_id, show_id, partnership_key, item.item_key
                )
                record = {
                    "id": item_id,
                    "tenant_id": tenant_id,
                    "show_id": show_id,
                    "partnership_id": partnership_id,
                    "item_key": item.item_key,
                    **_item_values(item),
                    "revision": 1,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                store.insert(conn, "partnership_items", record)
                _record_revision(
                    conn,
                    record,
                    change_kind="template_created",
                    actor_id=actor_id,
                    actor_role=actor_role,
                    timestamp=timestamp,
                )
                result.created += 1
                _audit(
                    conn, partnership_id, tenant_id, actor_id, actor_role,
                    "partnership.item_created", {"item_key": item.item_key}, timestamp,
                )
                result.item_ids.append(item_id)
        store.insert(conn, "partnership_template_imports", {
            "id": generation.new_id("partnership_template_import"),
            "tenant_id": tenant_id,
            "show_id": show_id,
            "partnership_id": partnership_id,
            "template_version": int(raw["version"]),
            "template_digest": digest,
            "source_reference": f"template://{partnership_key}/v{raw['version']}",
            "outcome": result.outcome,
            "item_count": len(items),
            "imported_by": actor_id,
            "imported_role": actor_role,
            "created_at": timestamp,
        })
        conn.execute("RELEASE SAVEPOINT partnership_import")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT partnership_import")
        conn.execute("RELEASE SAVEPOINT partnership_import")
        raise
    return result


def list_partnerships(
    conn: sqlite3.Connection, tenant_id: str, show_id: str | None = None
) -> list[dict[str, Any]]:
    where = "tenant_id = ?"
    params: tuple[str, ...] = (tenant_id,)
    if show_id is not None:
        _validate_boundary(show_id, "show_id")
        where += " AND show_id = ?"
        params += (show_id,)
    return store.fetch_all(
        conn,
        f"SELECT * FROM partnerships WHERE {where} ORDER BY label",
        params,
    )


def _partnership(
    conn: sqlite3.Connection,
    partnership_id: str,
    tenant_id: str,
    show_id: str | None = None,
) -> dict[str, Any]:
    value = store.fetch_one(
        conn,
        "SELECT * FROM partnerships WHERE id = ? AND tenant_id = ?",
        (partnership_id, tenant_id),
    )
    if value is None:
        raise PartnershipError(404, "partnership not found in this tenant")
    if show_id is not None:
        _validate_boundary(show_id, "show_id")
        if value["show_id"] != show_id:
            raise PartnershipError(403, "partnership belongs to another show")
    return value


def assign_legacy_show(
    conn: sqlite3.Connection,
    partnership_id: str,
    *,
    tenant_id: str,
    show_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """Atomically assign an ambiguous migrated partnership and its Pilot rows."""
    for value, label in (
        (partnership_id, "partnership_id"),
        (tenant_id, "tenant_id"),
        (show_id, "show_id"),
        (actor_id, "actor_id"),
    ):
        _validate_boundary(value, label)
    if actor_role not in {"producer", "editorial_owner", "relationship_owner"}:
        raise PartnershipError(403, "actor role cannot assign partnership custody")

    conn.execute("SAVEPOINT assign_partnership_show")
    try:
        partnership = _partnership(conn, partnership_id, tenant_id)
        current_show = str(partnership["show_id"])
        if current_show != "legacy":
            if current_show == show_id:
                conn.execute("RELEASE SAVEPOINT assign_partnership_show")
                return {"partnership_id": partnership_id, "show_id": show_id, "changed": False}
            raise PartnershipError(409, "partnership already has explicit show custody")
        active = store.fetch_one(
            conn,
            "SELECT 1 FROM show_registry "
            "WHERE tenant_id = ? AND show_id = ? AND status = 'active'",
            (tenant_id, show_id),
        )
        if active is None:
            raise PartnershipError(403, "target show is not active for this tenant")
        collision = store.fetch_one(
            conn,
            "SELECT id FROM partnerships WHERE tenant_id = ? AND show_id = ? "
            "AND template_key = ? AND id <> ?",
            (tenant_id, show_id, partnership["template_key"], partnership_id),
        )
        if collision is not None:
            raise PartnershipError(409, "target show already owns this partnership template")
        foreign_slot = store.fetch_one(
            conn,
            "SELECT s.id FROM pilot_candidate_slots s "
            "JOIN appearance_opportunities o ON o.id = s.opportunity_id "
            "WHERE s.tenant_id = ? AND s.partnership_id = ? AND o.show_id <> ? LIMIT 1",
            (tenant_id, partnership_id, show_id),
        )
        if foreign_slot is not None:
            raise PartnershipError(
                409,
                "legacy partnership has candidate slots owned by another show; resolve slots first",
            )

        partnership_tables = (
            "partnership_items",
            "partnership_audit_events",
            "partnership_item_revisions",
            "partnership_template_imports",
            "partnership_resource_links",
            "partnership_reviews",
            "pilot_candidate_slots",
            "pilot_policies",
            "pilot_runs",
        )
        for table in partnership_tables:
            conn.execute(
                f"UPDATE {table} SET show_id = ? WHERE tenant_id = ? AND partnership_id = ?",
                (show_id, tenant_id, partnership_id),
            )
        conn.execute(
            "UPDATE partnerships SET show_id = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND show_id = 'legacy'",
            (show_id, _now_iso(), partnership_id, tenant_id),
        )
        for table in (
            "pilot_candidate_assignments",
            "pilot_assignment_events",
            "pilot_plan_projections",
            "pilot_decisions",
        ):
            conn.execute(
                f"UPDATE {table} SET show_id = ? WHERE tenant_id = ? "
                f"AND pilot_run_id IN (SELECT id FROM pilot_runs "
                f"WHERE tenant_id = ? AND partnership_id = ?)",
                (show_id, tenant_id, tenant_id, partnership_id),
            )
        _audit(
            conn,
            partnership_id,
            tenant_id,
            actor_id,
            actor_role,
            "legacy_show_assigned",
            {"previous_show_id": "legacy", "show_id": show_id},
            _now_iso(),
        )
        conn.execute("RELEASE SAVEPOINT assign_partnership_show")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT assign_partnership_show")
        conn.execute("RELEASE SAVEPOINT assign_partnership_show")
        raise
    return {"partnership_id": partnership_id, "show_id": show_id, "changed": True}


def command_center(
    conn: sqlite3.Connection,
    partnership_id: str,
    tenant_id: str,
    show_id: str | None = None,
) -> dict[str, Any]:
    from .partnership_projections import command_center as build_command_center

    _partnership(conn, partnership_id, tenant_id, show_id)
    return build_command_center(conn, partnership_id, tenant_id)
def create_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .partnership_records import create_item as create_record

    return create_record(*args, **kwargs)


def update_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .partnership_records import update_item as update_record

    return update_record(*args, **kwargs)


def supersede_item(
    conn,
    partnership_id: str,
    item_id: str,
    *,
    expected_revision: int,
    successor_item_id: str,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    from .partnership_records import supersede_item as supersede_record

    return supersede_record(
        conn,
        partnership_id,
        item_id,
        expected_revision=expected_revision,
        successor_item_id=successor_item_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
    )


def link_resource(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .partnership_records import link_resource as link_record

    return link_record(*args, **kwargs)


def record_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .partnership_records import record_review as create_review

    return create_review(*args, **kwargs)


def record_informal_touchpoint(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .partnership_records import record_informal_touchpoint as create_touchpoint

    return create_touchpoint(*args, **kwargs)


def select_pilot_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .partnership_records import select_pilot_candidate as select_candidate

    return select_candidate(*args, **kwargs)


upsert_item = create_item
