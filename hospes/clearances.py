"""Rights and clearance custody for episodes: music, clips, IP, footage, likeness.

A clearance is the record that one rights item on one episode is either still
outstanding (``pending``), licensed (``cleared``), or refused (``denied``).  The
module owns four boundaries the rest of the engine depends on:

* **Custody.** The rights holder and the licence terms are private counterparty
  data.  With a configured tenant field vault they are sealed as AES-256-GCM
  ciphertext and the row keeps only a ``private-field://`` reference plus a
  SHA-256 checksum.  Without a vault the caller must supply an opaque external
  custody reference instead; a private literal is refused rather than stored in
  the clear, and the record says which mode it is in (``custody_mode``).
* **Evidence.** Every status decision requires an opaque evidence reference and
  appends an immutable ``clearance_receipts`` row.  A clearance never changes
  status without attributable evidence.
* **Publication denial.** ``publication_gate`` refuses an episode whose
  clearances are in *any* status other than ``cleared`` — pending and denied
  alike.  :mod:`hospes.distribution` calls it before drafting, authorizing, and
  marking a distribution published.
* **Badges.** ``episode_clearance_summary`` projects the per-episode counters
  the dashboard renders as "2 clearances pending".

Nothing here sends, publishes, or books.  It records, gates, and reports.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from . import encryption, generation, store

CLEARANCE_TYPES = ("music", "clip", "IP", "footage", "guest_likeness")
CLEARANCE_STATUSES = ("pending", "cleared", "denied")
#: Any status other than ``cleared`` denies publication.  Kept derived so a new
#: status can never quietly become publishable.
BLOCKING_STATUSES = tuple(status for status in CLEARANCE_STATUSES if status != "cleared")
CUSTODY_MODES = ("sealed", "external_reference")
AUTHORIZED_CLEARANCE_ROLES = frozenset(
    {"host", "network_operator", "producer", "editorial_owner", "relationship_owner"}
)
CLEARANCE_DECISION_ROLES = frozenset({"producer", "editorial_owner", "relationship_owner"})
PRIVATE_FIELD_CATEGORIES = {
    "rights_holder": "contact",
    "license_terms": "financial",
}
_STATUS_EVENT = {
    "cleared": "clearance.cleared",
    "denied": "clearance.denied",
    "pending": "clearance.reopened",
}
_INPUT_KEYS = frozenset(
    {
        "episode_id",
        "type",
        "clearance_type",
        "rights_holder",
        "license_terms",
        "cost",
        "cost_minor",
        "due_date",
    }
)
_DECISION_KEYS = frozenset({"status", "evidence_ref"})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}$")
_OPAQUE_REFERENCE = re.compile(r"^[a-z][a-z0-9_-]{1,31}://[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+-]{1,207}$")
_PRIVATE_REFERENCE = re.compile(
    r"^private-field://[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_COST_MINOR = 1_000_000_000
MAX_RIGHTS_HOLDER = 500
MAX_LICENSE_TERMS = 4_000
MAX_LIST_LIMIT = 500

CSV_COLUMNS = (
    "clearance_id",
    "episode_id",
    "type",
    "status",
    "blocks_publication",
    "cost_minor",
    "due_date",
    "custody_mode",
    "rights_holder_ref",
    "license_terms_ref",
    "evidence_ref",
    "recorded_by",
    "recorded_by_role",
    "decided_by",
    "decided_by_role",
    "decided_at",
    "created_at",
    "updated_at",
)


class ClearanceError(ValueError):
    """A safe clearance validation, authorization, or custody error."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ClearanceError(f"{field_name} must be an opaque identifier")
    normalized = value.strip()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ClearanceError(f"{field_name} must be an opaque identifier")
    return normalized


def _reference(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _OPAQUE_REFERENCE.fullmatch(value.strip()) is None:
        raise ClearanceError(f"{field_name} must be an opaque custody reference")
    return value.strip()


def _clearance_type(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in CLEARANCE_TYPES:
        raise ClearanceError(f"type must be one of {', '.join(CLEARANCE_TYPES)}")
    return value.strip()


def _status(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in CLEARANCE_STATUSES:
        raise ClearanceError(f"status must be one of {', '.join(CLEARANCE_STATUSES)}")
    return value.strip()


def _cost_minor(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClearanceError("cost_minor must be a whole number of minor units")
    if not 0 <= value <= MAX_COST_MINOR:
        raise ClearanceError(f"cost_minor must be between 0 and {MAX_COST_MINOR}")
    return value


def _due_date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value.strip()) is None:
        raise ClearanceError("due_date must be an ISO 8601 calendar date")
    normalized = value.strip()
    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise ClearanceError("due_date must be an ISO 8601 calendar date") from exc
    return normalized


def _private_value(value: Any, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ClearanceError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ClearanceError(f"{field_name} is required")
    if len(normalized) > limit:
        raise ClearanceError(f"{field_name} exceeds {limit} characters")
    if _CONTROL.search(normalized):
        raise ClearanceError(f"{field_name} contains unsupported control characters")
    return normalized


def _timestamp(now: datetime | None = None) -> str:
    return (now or generation.now()).astimezone(timezone.utc).isoformat()


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClearanceInput:
    """Strict clearance intake accepted by the service and HTTP boundary."""

    episode_id: str
    clearance_type: str
    rights_holder: str
    license_terms: str
    cost_minor: int
    due_date: str | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ClearanceInput":
        if not isinstance(payload, Mapping):
            raise ClearanceError("clearance payload must be an object")
        unknown = set(payload) - _INPUT_KEYS
        if unknown:
            raise ClearanceError("clearance payload contains unsupported fields")
        if "type" in payload and "clearance_type" in payload:
            raise ClearanceError("declare the clearance type exactly once")
        if "cost" in payload and "cost_minor" in payload:
            raise ClearanceError("declare the clearance cost exactly once")
        episode_id = _identifier(payload.get("episode_id"), "episode_id")
        return cls(
            episode_id=episode_id,
            clearance_type=_clearance_type(payload.get("type", payload.get("clearance_type"))),
            rights_holder=_private_value(payload.get("rights_holder"), "rights_holder", MAX_RIGHTS_HOLDER),
            license_terms=_private_value(payload.get("license_terms"), "license_terms", MAX_LICENSE_TERMS),
            cost_minor=_cost_minor(payload.get("cost", payload.get("cost_minor"))),
            due_date=_due_date(payload.get("due_date")),
        )


@dataclass(frozen=True)
class ClearanceDecision:
    """Strict decision intake: a target status plus its custody evidence."""

    status: str
    evidence_ref: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ClearanceDecision":
        if not isinstance(payload, Mapping):
            raise ClearanceError("clearance decision must be an object")
        unknown = set(payload) - _DECISION_KEYS
        if unknown:
            raise ClearanceError("clearance decision contains unsupported fields")
        return cls(
            status=_status(payload.get("status")),
            evidence_ref=_reference(payload.get("evidence_ref"), "evidence_ref"),
        )


def _field_scope(*, tenant_id: str, show_id: str, clearance_id: str, field_name: str) -> encryption.PrivateFieldScope:
    return encryption.PrivateFieldScope(
        tenant_id=tenant_id,
        show_id=show_id,
        category=PRIVATE_FIELD_CATEGORIES[field_name],
        owner_table="clearances",
        owner_record_id=clearance_id,
        field_name=field_name,
    )


def _record_id(
    tenant_id: str,
    show_id: str,
    episode_id: str,
    clearance_type: str,
    rights_holder_checksum: str,
    license_terms_checksum: str,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:clearance:{tenant_id}:{show_id}:{episode_id}:"
            f"{clearance_type}:{rights_holder_checksum}:{license_terms_checksum}",
        )
    )


def _require_view_role(actor_role: str) -> None:
    if actor_role not in AUTHORIZED_CLEARANCE_ROLES:
        raise ClearanceError("operator role cannot view rights clearances", 403)


def _require_decision_role(actor_role: str) -> None:
    if actor_role not in CLEARANCE_DECISION_ROLES:
        raise ClearanceError("operator role cannot decide rights clearances", 403)


def _scope(tenant_id: str, show_id: str) -> tuple[str, str]:
    tenant = _identifier(tenant_id, "tenant_id")
    show = _identifier(show_id, "show_id")
    return tenant, show


def _rollback_savepoint(conn: store.DatabaseConnection, name: str) -> None:
    conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _next_correlation(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, clearance_id: str) -> str:
    row = store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM clearance_receipts WHERE tenant_id = ? AND show_id = ? AND clearance_id = ?",
        (tenant_id, show_id, clearance_id),
    )
    ordinal = (int(row["count"]) if row else 0) + 1
    return f"clearance://{clearance_id}/{ordinal}"


def _append_receipt(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    clearance_id: str,
    episode_id: str,
    event_type: str,
    from_status: str | None,
    to_status: str,
    actor_id: str,
    actor_role: str,
    evidence_ref: str | None,
    timestamp: str,
) -> dict[str, Any]:
    receipt = {
        "id": generation.new_id("clearance_receipt"),
        "tenant_id": tenant_id,
        "show_id": show_id,
        "clearance_id": clearance_id,
        "episode_id": episode_id,
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "evidence_ref": evidence_ref,
        "correlation_id": _next_correlation(conn, tenant_id=tenant_id, show_id=show_id, clearance_id=clearance_id),
        "created_at": timestamp,
    }
    store.insert(conn, "clearance_receipts", receipt)
    return receipt


def record_clearance(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault | None,
    payload: ClearanceInput,
    *,
    tenant_id: str,
    show_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one clearance in ``pending`` state with custody-bound evidence.

    With a field vault the private values are sealed; without one they must
    already be opaque external custody references.  Re-recording the same
    identity returns the existing record instead of duplicating it.
    """
    _require_decision_role(actor_role)
    tenant, show = _scope(tenant_id, show_id)
    recorded_by = _identifier(actor_id, "actor_id")

    sealed = vault is not None
    if not sealed:
        for value, field_name in (
            (payload.rights_holder, "rights_holder"),
            (payload.license_terms, "license_terms"),
        ):
            if _OPAQUE_REFERENCE.fullmatch(value) is None:
                raise ClearanceError(
                    "clearance private-field custody is not configured; supply an "
                    f"opaque custody reference for {field_name}",
                    503,
                )
    rights_checksum = _checksum(payload.rights_holder)
    terms_checksum = _checksum(payload.license_terms)
    clearance_id = _record_id(
        tenant,
        show,
        payload.episode_id,
        payload.clearance_type,
        rights_checksum,
        terms_checksum,
    )
    existing = store.fetch_one(
        conn,
        "SELECT * FROM clearances WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, clearance_id),
    )
    if existing is not None:
        if int(existing["cost_minor"]) != payload.cost_minor or existing.get("due_date") != payload.due_date:
            raise ClearanceError("clearance identity already exists with different terms", 409)
        return reveal_clearance(conn, vault, existing, actor_role=actor_role)

    timestamp = _timestamp(now)
    conn.execute("SAVEPOINT clearance_record")
    try:
        if sealed:
            assert vault is not None
            rights_ref = vault.put_text(
                conn,
                _field_scope(
                    tenant_id=tenant,
                    show_id=show,
                    clearance_id=clearance_id,
                    field_name="rights_holder",
                ),
                payload.rights_holder,
                commit=False,
            )
            terms_ref = vault.put_text(
                conn,
                _field_scope(
                    tenant_id=tenant,
                    show_id=show,
                    clearance_id=clearance_id,
                    field_name="license_terms",
                ),
                payload.license_terms,
                commit=False,
            )
        else:
            rights_ref = payload.rights_holder
            terms_ref = payload.license_terms
        record = {
            "id": clearance_id,
            "tenant_id": tenant,
            "show_id": show,
            "episode_id": payload.episode_id,
            "clearance_type": payload.clearance_type,
            "status": "pending",
            "rights_holder_ref": rights_ref,
            "license_terms_ref": terms_ref,
            "cost_minor": payload.cost_minor,
            "due_date": payload.due_date,
            "custody_mode": "sealed" if sealed else "external_reference",
            "rights_holder_checksum": rights_checksum,
            "license_terms_checksum": terms_checksum,
            "recorded_by": recorded_by,
            "recorded_by_role": actor_role,
            "decided_by": None,
            "decided_by_role": None,
            "decided_at": None,
            "evidence_ref": None,
            "correlation_id": f"clearance://{clearance_id}",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        store.insert(conn, "clearances", record)
        _append_receipt(
            conn,
            tenant_id=tenant,
            show_id=show,
            clearance_id=clearance_id,
            episode_id=payload.episode_id,
            event_type="clearance.recorded",
            from_status=None,
            to_status="pending",
            actor_id=recorded_by,
            actor_role=actor_role,
            evidence_ref=None,
            timestamp=timestamp,
        )
        conn.execute("RELEASE SAVEPOINT clearance_record")
        conn.commit()
    except encryption.EncryptionError as exc:
        _rollback_savepoint(conn, "clearance_record")
        raise ClearanceError("clearance private-field custody is unavailable", 503) from exc
    except Exception as exc:
        _rollback_savepoint(conn, "clearance_record")
        raise ClearanceError("clearance write was rejected", 409) from exc
    return reveal_clearance(conn, vault, record, actor_role=actor_role)


def decide_clearance(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault | None,
    decision: ClearanceDecision,
    *,
    tenant_id: str,
    show_id: str,
    clearance_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move one clearance to a new status against an opaque evidence reference."""
    _require_decision_role(actor_role)
    tenant, show = _scope(tenant_id, show_id)
    decided_by = _identifier(actor_id, "actor_id")
    record_id = _identifier(clearance_id, "clearance_id")
    row = store.fetch_one(
        conn,
        "SELECT * FROM clearances WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, record_id),
    )
    if row is None:
        raise ClearanceError("clearance not found in this tenant/show", 404)
    current = str(row["status"])
    if current == decision.status:
        if row.get("evidence_ref") == decision.evidence_ref:
            return reveal_clearance(conn, vault, row, actor_role=actor_role)
        raise ClearanceError(f"clearance is already {current} under different evidence", 409)

    timestamp = _timestamp(now)
    conn.execute("SAVEPOINT clearance_decide")
    try:
        store.update(
            conn,
            "clearances",
            record_id,
            {
                "status": decision.status,
                "evidence_ref": decision.evidence_ref,
                "decided_by": decided_by,
                "decided_by_role": actor_role,
                "decided_at": timestamp,
                "updated_at": timestamp,
            },
        )
        _append_receipt(
            conn,
            tenant_id=tenant,
            show_id=show,
            clearance_id=record_id,
            episode_id=str(row["episode_id"]),
            event_type=_STATUS_EVENT[decision.status],
            from_status=current,
            to_status=decision.status,
            actor_id=decided_by,
            actor_role=actor_role,
            evidence_ref=decision.evidence_ref,
            timestamp=timestamp,
        )
        conn.execute("RELEASE SAVEPOINT clearance_decide")
        conn.commit()
    except Exception as exc:
        _rollback_savepoint(conn, "clearance_decide")
        raise ClearanceError("clearance decision was rejected", 409) from exc
    updated = store.fetch_one(conn, "SELECT * FROM clearances WHERE id = ?", (record_id,))
    return reveal_clearance(conn, vault, updated or row, actor_role=actor_role)


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    status = str(row["status"])
    return {
        "clearance_id": row["id"],
        "tenant_id": row["tenant_id"],
        "show_id": row["show_id"],
        "episode_id": row["episode_id"],
        "type": row["clearance_type"],
        "status": status,
        "blocks_publication": status in BLOCKING_STATUSES,
        "cost_minor": int(row["cost_minor"]),
        "due_date": row.get("due_date"),
        "custody_mode": row.get("custody_mode") or "external_reference",
        "rights_holder_ref": row["rights_holder_ref"],
        "license_terms_ref": row["license_terms_ref"],
        "evidence_ref": row.get("evidence_ref"),
        "recorded_by": row.get("recorded_by"),
        "recorded_by_role": row.get("recorded_by_role"),
        "decided_by": row.get("decided_by"),
        "decided_by_role": row.get("decided_by_role"),
        "decided_at": row.get("decided_at"),
        "correlation_id": row.get("correlation_id"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def reveal_clearance(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault | None,
    row: Mapping[str, Any],
    *,
    actor_role: str,
) -> dict[str, Any]:
    """Project one clearance, decrypting private values only for an authorized role."""
    _require_view_role(actor_role)
    item = _public_row(row)
    for field_name, checksum_column in (
        ("rights_holder", "rights_holder_checksum"),
        ("license_terms", "license_terms_checksum"),
    ):
        reference = str(row[f"{field_name}_ref"])
        if vault is None or _PRIVATE_REFERENCE.fullmatch(reference) is None:
            item[field_name] = None
            item[f"{field_name}_available"] = False
            continue
        try:
            value = vault.reveal_text(
                conn,
                reference,
                _field_scope(
                    tenant_id=str(row["tenant_id"]),
                    show_id=str(row["show_id"]),
                    clearance_id=str(row["id"]),
                    field_name=field_name,
                ),
                actor_role=actor_role,
                allowed_roles=AUTHORIZED_CLEARANCE_ROLES,
            )
        except encryption.EncryptionError as exc:
            raise ClearanceError("clearance private-field ciphertext is unavailable", 503) from exc
        checksum = row.get(checksum_column)
        if checksum and _checksum(value) != checksum:
            raise ClearanceError("clearance private-field checksum is invalid", 409)
        item[field_name] = value
        item[f"{field_name}_available"] = True
    return item


def list_clearances(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault | None,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
    status: str | None = None,
    clearance_type: str | None = None,
    due_before: str | None = None,
    blocking_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the scoped clearance list under bounded, validated filters."""
    _require_view_role(actor_role)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ClearanceError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
    if not isinstance(blocking_only, bool):
        raise ClearanceError("blocking_only must be a boolean")
    tenant, show = _scope(tenant_id, show_id)
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(_identifier(episode_id, "episode_id"))
    if status is not None:
        clauses.append("status = ?")
        params.append(_status(status))
    if clearance_type is not None:
        clauses.append("clearance_type = ?")
        params.append(_clearance_type(clearance_type))
    if due_before is not None:
        clauses.append("due_date IS NOT NULL AND due_date <= ?")
        params.append(_due_date(due_before))
    if blocking_only:
        placeholders = ",".join("?" for _ in BLOCKING_STATUSES)
        clauses.append(f"status IN ({placeholders})")
        params.extend(BLOCKING_STATUSES)
    params.append(limit)
    rows = store.fetch_all(
        conn,
        "SELECT * FROM clearances WHERE "
        + " AND ".join(clauses)
        + " ORDER BY episode_id, COALESCE(due_date, '9999-12-31'), created_at LIMIT ?",
        params,
    )
    return [reveal_clearance(conn, vault, row, actor_role=actor_role) for row in rows]


def clearance_receipts(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    clearance_id: str,
) -> list[dict[str, Any]]:
    """Return the immutable decision receipts recorded for one clearance."""
    _require_view_role(actor_role)
    tenant, show = _scope(tenant_id, show_id)
    return store.fetch_all(
        conn,
        "SELECT * FROM clearance_receipts WHERE tenant_id = ? AND show_id = ? "
        "AND clearance_id = ? ORDER BY created_at, correlation_id",
        (tenant, show, _identifier(clearance_id, "clearance_id")),
    )


def _badge(pending: int, denied: int, cleared: int) -> str:
    if pending and denied:
        return f"⚠ {pending} pending · {denied} denied"
    if pending:
        noun = "clearance" if pending == 1 else "clearances"
        return f"⚠ {pending} {noun} pending"
    if denied:
        noun = "clearance" if denied == 1 else "clearances"
        return f"⚠ {denied} {noun} denied"
    if cleared:
        noun = "clearance" if cleared == 1 else "clearances"
        return f"✓ {cleared} {noun} cleared"
    return "No clearances recorded"


def episode_clearance_summary(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
) -> list[dict[str, Any]]:
    """Project the per-episode clearance badge counters the dashboard renders."""
    _require_view_role(actor_role)
    tenant, show = _scope(tenant_id, show_id)
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(_identifier(episode_id, "episode_id"))
    rows = store.fetch_all(
        conn,
        "SELECT episode_id, status, COUNT(*) AS count, MIN(due_date) AS next_due "
        "FROM clearances WHERE " + " AND ".join(clauses) + " GROUP BY episode_id, status ORDER BY episode_id",
        params,
    )
    counters: dict[str, dict[str, Any]] = {}
    for row in rows:
        episode = str(row["episode_id"])
        bucket = counters.setdefault(
            episode,
            {
                "episode_id": episode,
                "pending": 0,
                "cleared": 0,
                "denied": 0,
                "total": 0,
                "next_due_date": None,
            },
        )
        count = int(row["count"])
        bucket[str(row["status"])] = count
        bucket["total"] += count
        due = row.get("next_due")
        if due and str(row["status"]) in BLOCKING_STATUSES:
            current = bucket["next_due_date"]
            bucket["next_due_date"] = due if current is None else min(current, due)
    summary = []
    for bucket in counters.values():
        blocking = bucket["pending"] + bucket["denied"]
        bucket["blocking"] = blocking
        bucket["publishable"] = blocking == 0 and bucket["total"] > 0
        bucket["badge"] = _badge(bucket["pending"], bucket["denied"], bucket["cleared"])
        summary.append(bucket)
    return summary


def publication_gate(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
) -> dict[str, Any]:
    """Return the publication verdict for one episode.

    Every status other than ``cleared`` denies publication, and the verdict
    names each blocking item so the denial is explainable rather than opaque.
    """
    tenant, show = _scope(tenant_id, show_id)
    episode = _identifier(episode_id, "episode_id")
    placeholders = ",".join("?" for _ in BLOCKING_STATUSES)
    rows = store.fetch_all(
        conn,
        "SELECT * FROM clearances WHERE tenant_id = ? AND show_id = ? "
        f"AND episode_id = ? AND status IN ({placeholders}) "
        "ORDER BY COALESCE(due_date, '9999-12-31'), created_at",
        (tenant, show, episode, *BLOCKING_STATUSES),
    )
    blocking = [
        {
            "clearance_id": row["id"],
            "type": row["clearance_type"],
            "status": row["status"],
            "due_date": row.get("due_date"),
        }
        for row in rows
    ]
    pending = sum(1 for item in blocking if item["status"] == "pending")
    denied = sum(1 for item in blocking if item["status"] == "denied")
    return {
        "episode_id": episode,
        "publishable": not blocking,
        "blocking": blocking,
        "pending": pending,
        "denied": denied,
        "reason": None
        if not blocking
        else f"{len(blocking)} rights item(s) are not cleared: {pending} pending, {denied} denied",
    }


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def clearance_report_csv(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
    status: str | None = None,
) -> str:
    """Render the scoped clearance report as CSV containing no private plaintext."""
    _require_view_role(actor_role)
    rows = list_clearances(
        conn,
        None,
        tenant_id=tenant_id,
        show_id=show_id,
        actor_role=actor_role,
        episode_id=episode_id,
        status=status,
        limit=MAX_LIST_LIMIT,
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_cell(row.get(column)) for column in CSV_COLUMNS})
    return output.getvalue()


__all__ = [
    "AUTHORIZED_CLEARANCE_ROLES",
    "BLOCKING_STATUSES",
    "CLEARANCE_DECISION_ROLES",
    "CLEARANCE_STATUSES",
    "CLEARANCE_TYPES",
    "CSV_COLUMNS",
    "CUSTODY_MODES",
    "ClearanceDecision",
    "ClearanceError",
    "ClearanceInput",
    "clearance_receipts",
    "clearance_report_csv",
    "decide_clearance",
    "episode_clearance_summary",
    "list_clearances",
    "publication_gate",
    "record_clearance",
    "reveal_clearance",
]
