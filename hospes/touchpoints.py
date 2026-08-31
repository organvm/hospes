"""Encrypted, attributable, tenant/show-scoped informal touchpoints.

Touchpoints describe conversations that happened outside HOSPES.  They are
informational evidence only: recording one never advances an opportunity or a
Pilot run.  Private notes are sealed with the tenant field vault and are
revealed only to an authenticated operator in the same tenant/show scope.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from . import encryption, generation, store


CHANNELS = frozenset({"text", "email", "ig", "in-person", "phone", "hallway"})
AUTHORIZED_TOUCHPOINT_ROLES = frozenset(
    {"host", "producer", "editorial_owner", "relationship_owner"}
)
_INPUT_KEYS = frozenset(
    {"guest_id", "opportunity_id", "partnership_id", "channel", "notes", "occurred_at"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}$")
_PRIVATE_REFERENCE = re.compile(
    r"^private-field://[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class TouchpointError(ValueError):
    """A safe touchpoint validation, authorization, or custody error."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _identifier(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise TouchpointError(f"{field_name} must be an opaque identifier")
    normalized = value.strip()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise TouchpointError(f"{field_name} must be an opaque identifier")
    return normalized


def _aware_timestamp(
    value: Any,
    field_name: str,
    *,
    now: datetime | None = None,
    reject_future: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise TouchpointError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TouchpointError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TouchpointError(f"{field_name} must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    current = (now or generation.now()).astimezone(timezone.utc)
    if reject_future and normalized > current:
        raise TouchpointError(f"{field_name} cannot be in the future")
    return normalized.isoformat()


def _private_notes(value: Any) -> str:
    if not isinstance(value, str):
        raise TouchpointError("notes must be text")
    normalized = value.strip()
    if not normalized:
        raise TouchpointError("notes are required")
    if len(normalized) > 2_000:
        raise TouchpointError("notes exceed 2000 characters")
    if _CONTROL.search(normalized):
        raise TouchpointError("notes contain unsupported control characters")
    return normalized


@dataclass(frozen=True)
class TouchpointInput:
    """Strict private input accepted by the service and HTTP boundary."""

    guest_id: str | None
    opportunity_id: str | None
    partnership_id: str | None
    channel: str
    notes: str
    occurred_at: str

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, Any], *, now: datetime | None = None
    ) -> "TouchpointInput":
        if not isinstance(payload, Mapping):
            raise TouchpointError("touchpoint payload must be an object")
        unknown = set(payload) - _INPUT_KEYS
        if unknown:
            if "initiator" in unknown or "initiator_role" in unknown:
                raise TouchpointError("initiator is derived from the authenticated operator")
            raise TouchpointError("touchpoint payload contains unsupported fields")
        guest_id = _identifier(payload.get("guest_id"), "guest_id", required=False)
        opportunity_id = _identifier(
            payload.get("opportunity_id"), "opportunity_id", required=False
        )
        if guest_id is None and opportunity_id is None:
            raise TouchpointError("guest_id or opportunity_id is required")
        channel = payload.get("channel")
        if not isinstance(channel, str) or channel not in CHANNELS:
            raise TouchpointError(f"channel must be one of {sorted(CHANNELS)}")
        return cls(
            guest_id=guest_id,
            opportunity_id=opportunity_id,
            partnership_id=_identifier(
                payload.get("partnership_id"), "partnership_id", required=False
            ),
            channel=channel,
            notes=_private_notes(payload.get("notes")),
            occurred_at=_aware_timestamp(
                payload.get("occurred_at"),
                "occurred_at",
                now=now,
                reject_future=True,
            ),
        )


def _notes_scope(
    *, tenant_id: str, show_id: str, touchpoint_id: str
) -> encryption.PrivateFieldScope:
    return encryption.PrivateFieldScope(
        tenant_id=tenant_id,
        show_id=show_id,
        category="private_relationship",
        owner_table="touchpoint_receipts",
        owner_record_id=touchpoint_id,
        field_name="notes",
    )


def _opportunity(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str | None,
    opportunity_id: str | None,
) -> dict[str, Any]:
    if opportunity_id is not None:
        row = store.fetch_one(
            conn,
            "SELECT * FROM appearance_opportunities WHERE tenant_id = ? "
            "AND show_id = ? AND id = ?",
            (tenant_id, show_id, opportunity_id),
        )
    else:
        row = store.fetch_one(
            conn,
            "SELECT * FROM appearance_opportunities WHERE tenant_id = ? "
            "AND show_id = ? AND (source_key = ? OR id = ?)",
            (tenant_id, show_id, guest_id, guest_id),
        )
    if row is None:
        raise TouchpointError("guest opportunity not found in this tenant/show", 404)
    canonical_guest_id = str(row.get("source_key") or row["id"])
    if guest_id is not None and guest_id not in {row["id"], canonical_guest_id}:
        raise TouchpointError("guest_id does not match the opportunity", 409)
    return row


def _partnership(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    partnership_id: str | None,
) -> dict[str, Any] | None:
    if partnership_id is None:
        return None
    row = store.fetch_one(
        conn,
        "SELECT * FROM partnerships WHERE tenant_id = ? AND id = ?",
        (tenant_id, partnership_id),
    )
    if row is None:
        raise TouchpointError("partnership not found in this tenant", 404)
    if row["show_id"] != show_id:
        raise TouchpointError("partnership belongs to another show", 403)
    return row


def _record_id(
    tenant_id: str,
    show_id: str,
    guest_id: str,
    channel: str,
    occurred_at: str,
    initiator: str,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:touchpoint:{tenant_id}:{show_id}:{guest_id}:"
            f"{channel}:{occurred_at}:{initiator}",
        )
    )


def _rollback_savepoint(conn: store.DatabaseConnection) -> None:
    conn.execute("ROLLBACK TO SAVEPOINT touchpoint_record")
    conn.execute("RELEASE SAVEPOINT touchpoint_record")


def record_touchpoint(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault,
    payload: TouchpointInput,
    *,
    tenant_id: str,
    show_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one immutable touchpoint without changing workflow state."""
    if actor_role not in AUTHORIZED_TOUCHPOINT_ROLES:
        raise TouchpointError("operator role cannot record informal touchpoints", 403)
    tenant = _identifier(tenant_id, "tenant_id")
    show = _identifier(show_id, "show_id")
    initiator = _identifier(actor_id, "actor_id")
    assert tenant is not None and show is not None and initiator is not None
    opportunity = _opportunity(
        conn,
        tenant_id=tenant,
        show_id=show,
        guest_id=payload.guest_id,
        opportunity_id=payload.opportunity_id,
    )
    partnership = _partnership(
        conn,
        tenant_id=tenant,
        show_id=show,
        partnership_id=payload.partnership_id,
    )
    guest_id = str(opportunity.get("source_key") or opportunity["id"])
    record_id = _record_id(
        tenant, show, guest_id, payload.channel, payload.occurred_at, initiator
    )
    notes_checksum = hashlib.sha256(payload.notes.encode("utf-8")).hexdigest()
    existing = store.fetch_one(
        conn,
        "SELECT * FROM touchpoint_receipts WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, record_id),
    )
    if existing is not None:
        if (
            existing.get("notes_checksum") != notes_checksum
            or existing.get("opportunity_id") != opportunity["id"]
            or existing.get("partnership_id") != payload.partnership_id
        ):
            raise TouchpointError(
                "touchpoint identity already exists with different evidence", 409
            )
        return reveal_touchpoint(
            conn, vault, existing, actor_role=actor_role, guest_name=opportunity["guest_name"]
        )

    timestamp = (now or generation.now()).astimezone(timezone.utc).isoformat()
    conn.execute("SAVEPOINT touchpoint_record")
    try:
        notes_ref = vault.put_text(
            conn,
            _notes_scope(tenant_id=tenant, show_id=show, touchpoint_id=record_id),
            payload.notes,
            commit=False,
        )
        record = {
            "id": record_id,
            "tenant_id": tenant,
            "show_id": show,
            "guest_id": guest_id,
            "opportunity_id": opportunity["id"],
            "partnership_id": partnership["id"] if partnership else None,
            "channel": payload.channel,
            "notes_ref": notes_ref,
            "notes_checksum": notes_checksum,
            "occurred_at": payload.occurred_at,
            "initiator": initiator,
            "initiator_role": actor_role,
            "correlation_id": f"touchpoint://{record_id}",
            "created_at": timestamp,
        }
        store.insert(conn, "touchpoint_receipts", record)
        if partnership is not None:
            store.insert(
                conn,
                "partnership_audit_events",
                {
                    "id": generation.new_id("partnership_audit_event"),
                    "tenant_id": tenant,
                    "partnership_id": partnership["id"],
                    "event_type": "touchpoint.recorded",
                    "actor_id": initiator,
                    "actor_role": actor_role,
                    "details": {
                        "touchpoint_id": record_id,
                        "guest_id": guest_id,
                        "channel": payload.channel,
                        "occurred_at": payload.occurred_at,
                    },
                    "show_id": show,
                    "subject_ref": f"opportunity://{opportunity['id']}",
                    "correlation_id": f"touchpoint://{record_id}",
                    "created_at": timestamp,
                },
            )
        conn.execute("RELEASE SAVEPOINT touchpoint_record")
        conn.commit()
    except encryption.EncryptionError as exc:
        _rollback_savepoint(conn)
        raise TouchpointError("touchpoint note encryption is unavailable", 503) from exc
    except Exception as exc:
        _rollback_savepoint(conn)
        raise TouchpointError("touchpoint write was rejected", 409) from exc
    return reveal_touchpoint(
        conn, vault, record, actor_role=actor_role, guest_name=opportunity["guest_name"]
    )


def _public_row(row: Mapping[str, Any], *, guest_name: str | None = None) -> dict[str, Any]:
    return {
        "touchpoint_id": row["id"],
        "tenant_id": row["tenant_id"],
        "show_id": row["show_id"],
        "guest_id": row["guest_id"],
        "guest_name": guest_name if guest_name is not None else row.get("guest_name"),
        "opportunity_id": row.get("opportunity_id"),
        "partnership_id": row.get("partnership_id"),
        "kind": "informal_touchpoint",
        "channel": row["channel"],
        "notes_ref": row["notes_ref"],
        "occurred_at": row["occurred_at"],
        "initiator": row["initiator"],
        "initiator_role": row.get("initiator_role"),
        "created_at": row["created_at"],
    }


def reveal_touchpoint(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault,
    row: Mapping[str, Any],
    *,
    actor_role: str,
    guest_name: str | None = None,
) -> dict[str, Any]:
    if actor_role not in AUTHORIZED_TOUCHPOINT_ROLES:
        raise TouchpointError("operator role cannot view informal touchpoints", 403)
    item = _public_row(row, guest_name=guest_name)
    reference = str(row["notes_ref"])
    if _PRIVATE_REFERENCE.fullmatch(reference) is None:
        item["notes"] = None
        item["notes_available"] = False
        return item
    try:
        notes = vault.reveal_text(
            conn,
            reference,
            _notes_scope(
                tenant_id=str(row["tenant_id"]),
                show_id=str(row["show_id"]),
                touchpoint_id=str(row["id"]),
            ),
            actor_role=actor_role,
            allowed_roles=AUTHORIZED_TOUCHPOINT_ROLES,
        )
    except encryption.EncryptionError as exc:
        raise TouchpointError("touchpoint note ciphertext is unavailable", 503) from exc
    checksum = row.get("notes_checksum")
    if checksum and not hashlib.sha256(notes.encode("utf-8")).hexdigest() == checksum:
        raise TouchpointError("touchpoint note checksum is invalid", 409)
    item["notes"] = notes
    item["notes_available"] = True
    return item


def list_touchpoints(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    guest_id: str | None = None,
    opportunity_id: str | None = None,
    partnership_id: str | None = None,
    channel: str | None = None,
    initiator: str | None = None,
    from_at: str | None = None,
    to_at: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return a newest-first authorized timeline with bounded filters."""
    if actor_role not in AUTHORIZED_TOUCHPOINT_ROLES:
        raise TouchpointError("operator role cannot view informal touchpoints", 403)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise TouchpointError("limit must be an integer between 1 and 200")
    tenant = _identifier(tenant_id, "tenant_id")
    show = _identifier(show_id, "show_id")
    assert tenant is not None and show is not None
    clauses = ["t.tenant_id = ?", "t.show_id = ?"]
    params: list[Any] = [tenant, show]
    for column, value, label in (
        ("t.guest_id", guest_id, "guest_id"),
        ("t.opportunity_id", opportunity_id, "opportunity_id"),
        ("t.partnership_id", partnership_id, "partnership_id"),
        ("t.initiator", initiator, "initiator"),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(_identifier(value, label))
    if channel is not None:
        if channel not in CHANNELS:
            raise TouchpointError(f"channel must be one of {sorted(CHANNELS)}")
        clauses.append("t.channel = ?")
        params.append(channel)
    normalized_from = (
        _aware_timestamp(from_at, "from_at") if from_at is not None else None
    )
    normalized_to = _aware_timestamp(to_at, "to_at") if to_at is not None else None
    if normalized_from and normalized_to and normalized_from > normalized_to:
        raise TouchpointError("from_at cannot be after to_at")
    if normalized_from:
        clauses.append("t.occurred_at >= ?")
        params.append(normalized_from)
    if normalized_to:
        clauses.append("t.occurred_at <= ?")
        params.append(normalized_to)
    params.append(limit)
    rows = store.fetch_all(
        conn,
        "SELECT t.*, o.guest_name FROM touchpoint_receipts t "
        "LEFT JOIN appearance_opportunities o ON o.tenant_id = t.tenant_id "
        "AND o.show_id = t.show_id AND o.id = t.opportunity_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY t.occurred_at DESC, t.id DESC LIMIT ?",
        params,
    )
    return [reveal_touchpoint(conn, vault, row, actor_role=actor_role) for row in rows]


__all__ = [
    "AUTHORIZED_TOUCHPOINT_ROLES",
    "CHANNELS",
    "TouchpointError",
    "TouchpointInput",
    "list_touchpoints",
    "record_touchpoint",
    "reveal_touchpoint",
]
