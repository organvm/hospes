"""Cross-season guest CRM memory and the live do-not-contact directive.

A guest is an opaque, tenant/show-scoped identity that outlives any one season.
``guest_history`` is the durable memory of every prior interaction with that
identity — season, episode, disposition, date, and an opaque or encrypted note
reference.  ``guest_directives`` holds the *current* answer to one question:
may this show contact this guest at all?

Two rules make the memory trustworthy:

* The directive, not the history, decides.  A history row records what happened;
  the directive records the standing instruction, so an operator can both set
  and lift do-not-contact.  When no directive exists the reader falls back to
  the legacy append-only ``guest_history.do_not_contact`` marker rather than
  silently reporting "contact permitted".
* Only the relationship owner may write a directive.  Recording history is open
  to every operator role; deciding that a human may never be approached again is
  not.

Do-not-contact is enforced at both ends of the loop: an import cannot re-ask a
blocked guest, and no outbound draft can be previewed or persisted for one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from . import encryption, generation, privacy, store

#: Bounded outcome vocabulary for one recorded season interaction.
DISPOSITIONS = frozenset(
    {
        "APPROVED",
        "DECLINED",
        "SOFT_DECLINE",
        "HARD_DECLINE",
        "NO_RESPONSE",
        "REVISIT_LATER",
        "PROTECTED",
        "DO_NOT_CONTACT",
        "RECORDED",
        "PUBLISHED",
        "CANCELLED",
    }
)
AUTHORIZED_HISTORY_ROLES = frozenset({"host", "producer", "editorial_owner", "relationship_owner"})
#: Setting or lifting a do-not-contact directive is a relationship-owner act.
DIRECTIVE_ROLES = frozenset({"relationship_owner"})

#: Directive-derived history rows keep the timeline honest but never become the
#: "Previously: ..." badge, which reports the last real season interaction.
DIRECTIVE_SOURCE = "directive"
DIRECTIVE_SEASON = "current"
DIRECTIVE_EPISODE = "registry"

GUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,31}$")
_OPAQUE_REFERENCE = re.compile(r"^[a-z][a-z0-9_-]{1,31}://[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+-]{1,207}$")
_PRIVATE_REFERENCE = re.compile(
    r"^private-field://[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INPUT_KEYS = frozenset(
    {
        "guest_id",
        "opportunity_id",
        "season",
        "episode",
        "disposition",
        "date",
        "notes",
        "notes_ref",
    }
)
_DERIVED_KEYS = frozenset({"recorded_by", "recorded_by_role", "source"})


class GuestHistoryError(ValueError):
    """A safe guest-CRM validation, authorization, or custody error."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _identifier(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or GUEST_ID.fullmatch(value.strip()) is None:
        raise GuestHistoryError(f"{field_name} must be an opaque identifier")
    return value.strip()


def _label(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _LABEL.fullmatch(value.strip()) is None:
        raise GuestHistoryError(f"{field_name} must be a short opaque label")
    return value.strip()


def _disposition(value: Any) -> str:
    if not isinstance(value, str):
        raise GuestHistoryError("disposition must be text")
    normalized = value.strip().upper()
    if normalized not in DISPOSITIONS:
        raise GuestHistoryError(f"disposition must be one of {sorted(DISPOSITIONS)}")
    return normalized


def _calendar_date(value: Any, *, now: datetime | None = None) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value.strip()) is None:
        raise GuestHistoryError("date must be an ISO-8601 calendar date")
    try:
        parsed = date_type.fromisoformat(value.strip())
    except ValueError as exc:
        raise GuestHistoryError("date must be an ISO-8601 calendar date") from exc
    current = (now or generation.now()).astimezone(timezone.utc).date()
    if parsed > current:
        raise GuestHistoryError("date cannot be in the future")
    return parsed.isoformat()


def _reference(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _OPAQUE_REFERENCE.fullmatch(value.strip()) is None:
        raise GuestHistoryError(f"{field_name} must be an opaque owner reference")
    normalized = value.strip()
    if privacy.contact_kind(normalized) is not None:
        raise GuestHistoryError(f"{field_name} must not contain contact data")
    return normalized


def _private_notes(value: Any) -> str:
    if not isinstance(value, str):
        raise GuestHistoryError("notes must be text")
    normalized = value.strip()
    if not normalized:
        raise GuestHistoryError("notes cannot be empty")
    if len(normalized) > 2_000:
        raise GuestHistoryError("notes exceed 2000 characters")
    if _CONTROL.search(normalized):
        raise GuestHistoryError("notes contain unsupported control characters")
    return normalized


@dataclass(frozen=True)
class GuestHistoryInput:
    """Strict private input accepted by the service and HTTP boundary."""

    guest_id: str | None
    opportunity_id: str | None
    season: str
    episode: str
    disposition: str
    date: str
    notes: str | None
    notes_ref: str | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, now: datetime | None = None) -> "GuestHistoryInput":
        if not isinstance(payload, Mapping):
            raise GuestHistoryError("guest history payload must be an object")
        unknown = set(payload) - _INPUT_KEYS
        if unknown:
            if unknown & _DERIVED_KEYS:
                raise GuestHistoryError("the recording operator is derived from the authenticated session")
            raise GuestHistoryError("guest history payload contains unsupported fields")
        guest_id = _identifier(payload.get("guest_id"), "guest_id", required=False)
        opportunity_id = _identifier(payload.get("opportunity_id"), "opportunity_id", required=False)
        if guest_id is None and opportunity_id is None:
            raise GuestHistoryError("guest_id or opportunity_id is required")
        notes = payload.get("notes")
        notes_ref = payload.get("notes_ref")
        if notes is not None and notes_ref is not None:
            raise GuestHistoryError("supply either notes or notes_ref, never both")
        return cls(
            guest_id=guest_id,
            opportunity_id=opportunity_id,
            season=_label(payload.get("season"), "season"),
            episode=_label(payload.get("episode"), "episode"),
            disposition=_disposition(payload.get("disposition")),
            date=_calendar_date(payload.get("date"), now=now),
            notes=None if notes is None else _private_notes(notes),
            notes_ref=None if notes_ref is None else _reference(notes_ref, "notes_ref"),
        )


def guest_identity(opportunity: Mapping[str, Any]) -> str:
    """Return the cross-season guest id an opportunity row belongs to."""
    return str(opportunity.get("guest_id") or opportunity.get("source_key") or opportunity["id"])


def _history_id(
    tenant_id: str,
    show_id: str,
    guest_id: str,
    season: str,
    episode: str,
    disposition: str,
    date: str,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:guest-history:{tenant_id}:{show_id}:{guest_id}:{season}:{episode}:{disposition}:{date}",
        )
    )


def _directive_id(tenant_id: str, show_id: str, guest_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:guest-directive:{tenant_id}:{show_id}:{guest_id}",
        )
    )


def _notes_scope(*, tenant_id: str, show_id: str, history_id: str) -> encryption.PrivateFieldScope:
    return encryption.PrivateFieldScope(
        tenant_id=tenant_id,
        show_id=show_id,
        category="private_relationship",
        owner_table="guest_history",
        owner_record_id=history_id,
        field_name="notes",
    )


def _scope(tenant_id: str, show_id: str) -> tuple[str, str]:
    tenant = _identifier(tenant_id, "tenant_id")
    show = _identifier(show_id, "show_id")
    assert tenant is not None and show is not None
    return tenant, show


def _opportunity(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str | None,
    opportunity_id: str | None,
) -> dict[str, Any] | None:
    """Resolve the opportunity a history entry attaches to, if one exists.

    A cross-season entry may predate HOSPES entirely, so a guest-only lookup
    that finds nothing is legal. An explicit ``opportunity_id`` is not: naming a
    record that is absent from this tenant/show is an authorization error.
    """
    if opportunity_id is not None:
        row = store.fetch_one(
            conn,
            "SELECT * FROM appearance_opportunities WHERE tenant_id = ? AND show_id = ? AND id = ?",
            (tenant_id, show_id, opportunity_id),
        )
        if row is None:
            raise GuestHistoryError("guest opportunity not found in this tenant/show", 404)
        if guest_id is not None and guest_id not in {row["id"], guest_identity(row)}:
            raise GuestHistoryError("guest_id does not match the opportunity", 409)
        return row
    return store.fetch_one(
        conn,
        "SELECT * FROM appearance_opportunities WHERE tenant_id = ? AND show_id = ? "
        "AND (guest_id = ? OR source_key = ? OR id = ?) ORDER BY created_at DESC",
        (tenant_id, show_id, guest_id, guest_id, guest_id),
    )


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "history_id": row["id"],
        "tenant_id": row["tenant_id"],
        "show_id": row["show_id"],
        "guest_id": row["guest_id"],
        "opportunity_id": row.get("opportunity_id"),
        "kind": "guest_history_entry",
        "season": row["season"],
        "episode": row["episode"],
        "disposition": row["disposition"],
        "date": row.get("date") or str(row["occurred_at"])[:10],
        "occurred_at": row["occurred_at"],
        "notes_ref": row["notes_ref"],
        "do_not_contact": bool(row.get("do_not_contact")),
        "source": row.get("source") or "legacy",
        "recorded_by": row.get("recorded_by") or "legacy",
        "recorded_by_role": row.get("recorded_by_role") or "legacy",
        "created_at": row["created_at"],
    }


def reveal_history(
    conn: store.DatabaseConnection,
    row: Mapping[str, Any],
    *,
    actor_role: str,
    field_vault: encryption.FieldVault | None = None,
) -> dict[str, Any]:
    """Project one entry, decrypting its note only for an authorized session."""
    if actor_role not in AUTHORIZED_HISTORY_ROLES:
        raise GuestHistoryError("operator role cannot view guest history", 403)
    item = _public_row(row)
    reference = str(row["notes_ref"])
    if field_vault is None or _PRIVATE_REFERENCE.fullmatch(reference) is None:
        item["notes"] = None
        item["notes_available"] = False
        return item
    try:
        notes = field_vault.reveal_text(
            conn,
            reference,
            _notes_scope(
                tenant_id=str(row["tenant_id"]),
                show_id=str(row["show_id"]),
                history_id=str(row["id"]),
            ),
            actor_role=actor_role,
            allowed_roles=AUTHORIZED_HISTORY_ROLES,
        )
    except encryption.EncryptionError as exc:
        raise GuestHistoryError("guest history ciphertext is unavailable", 503) from exc
    checksum = row.get("notes_checksum")
    if checksum and hashlib.sha256(notes.encode("utf-8")).hexdigest() != checksum:
        raise GuestHistoryError("guest history note checksum is invalid", 409)
    item["notes"] = notes
    item["notes_available"] = True
    return item


def _rollback_savepoint(conn: store.DatabaseConnection) -> None:
    conn.execute("ROLLBACK TO SAVEPOINT guest_history_record")
    conn.execute("RELEASE SAVEPOINT guest_history_record")


def record_history(
    conn: store.DatabaseConnection,
    payload: GuestHistoryInput,
    *,
    tenant_id: str,
    show_id: str,
    actor_id: str,
    actor_role: str,
    field_vault: encryption.FieldVault | None = None,
    source: str = "operator",
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Append one immutable cross-season memory entry, idempotently."""
    if actor_role not in AUTHORIZED_HISTORY_ROLES:
        raise GuestHistoryError("operator role cannot record guest history", 403)
    tenant, show = _scope(tenant_id, show_id)
    recorded_by = _identifier(actor_id, "actor_id")
    assert recorded_by is not None
    opportunity = _opportunity(
        conn,
        tenant_id=tenant,
        show_id=show,
        guest_id=payload.guest_id,
        opportunity_id=payload.opportunity_id,
    )
    guest_id = payload.guest_id or guest_identity(opportunity or {})
    if not guest_id:  # pragma: no cover - unreachable via GuestHistoryInput
        raise GuestHistoryError("guest_id is required")
    record_id = _history_id(
        tenant,
        show,
        guest_id,
        payload.season,
        payload.episode,
        payload.disposition,
        payload.date,
    )
    checksum = hashlib.sha256(payload.notes.encode("utf-8")).hexdigest() if payload.notes is not None else None
    existing = store.fetch_one(
        conn,
        "SELECT * FROM guest_history WHERE tenant_id = ? AND show_id = ? AND id = ?",
        (tenant, show, record_id),
    )
    if existing is not None:
        if existing.get("notes_checksum") != checksum:
            raise GuestHistoryError("guest history entry already exists with different evidence", 409)
        return reveal_history(conn, existing, actor_role=actor_role, field_vault=field_vault)
    if payload.notes is not None and field_vault is None:
        raise GuestHistoryError("guest history note custody is not configured", 503)

    timestamp = (now or generation.now()).astimezone(timezone.utc)
    conn.execute("SAVEPOINT guest_history_record")
    try:
        if payload.notes is not None:
            assert field_vault is not None
            notes_ref = field_vault.put_text(
                conn,
                _notes_scope(tenant_id=tenant, show_id=show, history_id=record_id),
                payload.notes,
                commit=False,
            )
        else:
            notes_ref = payload.notes_ref or f"guest-history://{record_id}"
        record = {
            "id": record_id,
            "tenant_id": tenant,
            "show_id": show,
            "guest_id": guest_id,
            "opportunity_id": opportunity["id"] if opportunity is not None else None,
            "season": payload.season,
            "episode": payload.episode,
            "disposition": payload.disposition,
            "occurred_at": f"{payload.date}T00:00:00+00:00",
            "date": payload.date,
            "notes_ref": notes_ref,
            "notes_checksum": checksum,
            "do_not_contact": payload.disposition == "DO_NOT_CONTACT",
            "source": source,
            "recorded_by": recorded_by,
            "recorded_by_role": actor_role,
            "created_at": timestamp.isoformat(),
        }
        store.insert(conn, "guest_history", record)
        if opportunity is not None:
            store.insert(
                conn,
                "audit_events",
                {
                    "id": generation.new_id("audit_event"),
                    "tenant_id": tenant,
                    "show_id": show,
                    "opportunity_id": opportunity["id"],
                    "event_type": "guest_history.recorded",
                    "actor_id": recorded_by,
                    "actor_role": actor_role,
                    "details": {
                        "history_id": record_id,
                        "guest_id": guest_id,
                        "season": payload.season,
                        "episode": payload.episode,
                        "disposition": payload.disposition,
                        "date": payload.date,
                        "source": source,
                    },
                    "created_at": timestamp.isoformat(),
                },
            )
        conn.execute("RELEASE SAVEPOINT guest_history_record")
        if commit:
            conn.commit()
    except encryption.EncryptionError as exc:
        _rollback_savepoint(conn)
        raise GuestHistoryError("guest history note encryption is unavailable", 503) from exc
    except Exception as exc:
        _rollback_savepoint(conn)
        raise GuestHistoryError("guest history write was rejected", 409) from exc
    return reveal_history(conn, record, actor_role=actor_role, field_vault=field_vault)


def list_history(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    guest_id: str | None = None,
    opportunity_id: str | None = None,
    season: str | None = None,
    limit: int = 100,
    field_vault: encryption.FieldVault | None = None,
) -> list[dict[str, Any]]:
    """Return the newest-first authorized cross-season timeline for one show."""
    if actor_role not in AUTHORIZED_HISTORY_ROLES:
        raise GuestHistoryError("operator role cannot view guest history", 403)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise GuestHistoryError("limit must be an integer between 1 and 200")
    tenant, show = _scope(tenant_id, show_id)
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    for column, value, label in (
        ("guest_id", guest_id, "guest_id"),
        ("opportunity_id", opportunity_id, "opportunity_id"),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(_identifier(value, label))
    if season is not None:
        clauses.append("season = ?")
        params.append(_label(season, "season"))
    params.append(limit)
    rows = store.fetch_all(
        conn,
        "SELECT * FROM guest_history WHERE "
        + " AND ".join(clauses)
        + " ORDER BY date DESC, created_at DESC, id DESC LIMIT ?",
        params,
    )
    return [reveal_history(conn, row, actor_role=actor_role, field_vault=field_vault) for row in rows]


def public_history(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return newest-first season memory without any note plaintext."""
    tenant, show = _scope(tenant_id, show_id)
    rows = store.fetch_all(
        conn,
        "SELECT * FROM guest_history WHERE tenant_id = ? AND show_id = ? "
        "AND guest_id = ? ORDER BY date DESC, created_at DESC, id DESC LIMIT ?",
        (tenant, show, guest_id, max(1, min(int(limit), 200))),
    )
    return [_public_row(row) for row in rows]


def badge(entries: list[dict[str, Any]]) -> str | None:
    """Render the approval-card badge from the last real season interaction."""
    for entry in entries:
        if entry.get("source") == DIRECTIVE_SOURCE:
            continue
        return f"Previously: {entry['season']}{entry['episode']} — {entry['disposition']} ({entry['date']})"
    return None


def directive_state(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, guest_id: str) -> dict[str, Any]:
    """Return the live directive, falling back to the legacy history marker."""
    tenant, show = _scope(tenant_id, show_id)
    row = store.fetch_one(
        conn,
        "SELECT * FROM guest_directives WHERE tenant_id = ? AND show_id = ? AND guest_id = ?",
        (tenant, show, guest_id),
    )
    if row is not None:
        return {
            "guest_id": guest_id,
            "do_not_contact": bool(row["do_not_contact"]),
            "reason_ref": row.get("reason_ref"),
            "source": row.get("source") or "operator",
            "set_by": row["set_by"],
            "set_by_role": row["set_by_role"],
            "updated_at": row["updated_at"],
            "authority": "directive",
        }
    legacy = store.fetch_one(
        conn,
        "SELECT MAX(do_not_contact) AS blocked FROM guest_history WHERE tenant_id = ? AND show_id = ? AND guest_id = ?",
        (tenant, show, guest_id),
    )
    return {
        "guest_id": guest_id,
        "do_not_contact": bool((legacy or {}).get("blocked")),
        "reason_ref": None,
        "source": "legacy",
        "set_by": None,
        "set_by_role": None,
        "updated_at": None,
        "authority": "legacy_history",
    }


def is_do_not_contact(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, guest_id: str) -> bool:
    return bool(directive_state(conn, tenant_id=tenant_id, show_id=show_id, guest_id=guest_id)["do_not_contact"])


def blocked_guests(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    guest_ids: list[str],
) -> set[str]:
    """Return every guest in ``guest_ids`` the show may not contact."""
    return {
        guest_id
        for guest_id in dict.fromkeys(guest_ids)
        if is_do_not_contact(conn, tenant_id=tenant_id, show_id=show_id, guest_id=guest_id)
    }


def set_directive(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    do_not_contact: bool,
    actor_id: str,
    actor_role: str,
    reason_ref: str | None = None,
    opportunity_id: str | None = None,
    source: str = "operator",
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Set or lift the do-not-contact directive and append its history entry."""
    if actor_role not in DIRECTIVE_ROLES:
        raise GuestHistoryError("only a relationship owner may change a do-not-contact directive", 403)
    if not isinstance(do_not_contact, bool):
        raise GuestHistoryError("do_not_contact must be a boolean")
    tenant, show = _scope(tenant_id, show_id)
    guest = _identifier(guest_id, "guest_id")
    set_by = _identifier(actor_id, "actor_id")
    assert guest is not None and set_by is not None
    normalized_reason = None if reason_ref is None else _reference(reason_ref, "reason_ref")
    timestamp = (now or generation.now()).astimezone(timezone.utc).isoformat()
    directive_id = _directive_id(tenant, show, guest)
    existing = store.fetch_one(conn, "SELECT * FROM guest_directives WHERE id = ?", (directive_id,))
    declared = {
        "do_not_contact": do_not_contact,
        "reason_ref": normalized_reason,
        "source": source,
        "set_by": set_by,
        "set_by_role": actor_role,
    }
    # An unchanged re-declaration must write nothing at all; an import that
    # repeats yesterday's row is not a new instruction.
    flipped = existing is None or bool(existing["do_not_contact"]) != do_not_contact
    drift = (
        declared
        if existing is None
        else {key: value for key, value in declared.items() if existing.get(key) != value}
    )
    changed = bool(drift)
    if existing is None:
        store.insert(
            conn,
            "guest_directives",
            {
                "id": directive_id,
                "tenant_id": tenant,
                "show_id": show,
                "guest_id": guest,
                **declared,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
    elif drift:
        store.update(
            conn,
            "guest_directives",
            directive_id,
            {**drift, "updated_at": timestamp},
        )
    if flipped:
        history = GuestHistoryInput(
            guest_id=guest,
            opportunity_id=opportunity_id,
            season=DIRECTIVE_SEASON,
            episode=DIRECTIVE_EPISODE,
            disposition="DO_NOT_CONTACT" if do_not_contact else "REVISIT_LATER",
            date=(now or generation.now()).astimezone(timezone.utc).date().isoformat(),
            notes=None,
            notes_ref=normalized_reason,
        )
        record_history(
            conn,
            history,
            tenant_id=tenant,
            show_id=show,
            actor_id=set_by,
            actor_role=actor_role,
            source=DIRECTIVE_SOURCE,
            now=now,
            commit=False,
        )
        opportunity = _opportunity(
            conn,
            tenant_id=tenant,
            show_id=show,
            guest_id=guest,
            opportunity_id=opportunity_id,
        )
        if opportunity is not None:
            store.insert(
                conn,
                "audit_events",
                {
                    "id": generation.new_id("audit_event"),
                    "tenant_id": tenant,
                    "show_id": show,
                    "opportunity_id": opportunity["id"],
                    "event_type": ("guest.do_not_contact_set" if do_not_contact else "guest.contact_permitted"),
                    "actor_id": set_by,
                    "actor_role": actor_role,
                    "details": {"guest_id": guest, "source": source},
                    "created_at": timestamp,
                },
            )
    if commit:
        conn.commit()
    state = directive_state(conn, tenant_id=tenant, show_id=show, guest_id=guest)
    state["changed"] = changed
    return state


__all__ = [
    "AUTHORIZED_HISTORY_ROLES",
    "DIRECTIVE_ROLES",
    "DISPOSITIONS",
    "GuestHistoryError",
    "GuestHistoryInput",
    "badge",
    "blocked_guests",
    "directive_state",
    "guest_identity",
    "is_do_not_contact",
    "list_history",
    "public_history",
    "record_history",
    "reveal_history",
    "set_directive",
]
