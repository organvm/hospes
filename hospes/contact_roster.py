"""Encrypted, tenant/show-scoped contact rosters for candidate opportunities.

Raw names, email addresses, phone numbers, and notes enter only through the
private import boundary.  Each value is sealed independently with the tenant
field vault.  Database projections and public API contracts expose only opaque
``private-field://`` references; authorized operators may reveal values
transiently for review and invitation-route prefill.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from . import encryption, store


ROSTER_ROLES = ("publicist", "manager", "agent", "direct")
ROSTER_ROLE_SET = frozenset(ROSTER_ROLES)
ROSTER_FIELD_NAMES = ("name", "email", "phone", "notes")
PERMISSION_STATUSES = frozenset(
    {"pending_verification", "permitted", "do_not_use", "opted_out"}
)
AUTHORIZED_CONTACT_ROLES = frozenset(
    {"host", "producer", "editorial_owner", "relationship_owner"}
)
_ENTRY_KEYS = frozenset(
    {
        *ROSTER_FIELD_NAMES,
        "provenance_ref",
        "verified_at",
        "usable",
        "preferred",
        "permission_status",
    }
)
_OPAQUE_REFERENCE = re.compile(
    r"^[a-z][a-z0-9_-]{1,31}://[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+-]{1,207}$"
)
_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)
_PHONE_CHARS = re.compile(r"^[0-9+(). xXtTeE-]{7,40}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PRIVATE_REF = re.compile(
    r"^private-field://[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ContactRosterError(ValueError):
    """A safe roster validation or custody failure."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


@dataclass(frozen=True)
class ContactRosterEntry:
    role: str
    name: str = field(repr=False)
    email: str | None = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)
    notes: str | None = field(default=None, repr=False)
    provenance_ref: str = ""
    verified_at: str | None = None
    usable: bool = False
    preferred: bool = False
    permission_status: str = "pending_verification"


@dataclass(frozen=True)
class RosterUpsertResult:
    created_roles: tuple[str, ...] = ()
    updated_roles: tuple[str, ...] = ()
    unchanged_roles: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created_roles or self.updated_roles)

    @property
    def changed_fields(self) -> list[str]:
        return [
            *(f"contact_roster.{role}.created" for role in self.created_roles),
            *(f"contact_roster.{role}.updated" for role in self.updated_roles),
        ]


def _pairs_without_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ContactRosterError("contact_roster JSON keys must be unique")
        output[key] = value
    return output


def _load_mapping(raw: Any) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        try:
            encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ContactRosterError("contact_roster must be a JSON object") from exc
        if len(encoded.encode("utf-8")) > 32768:
            raise ContactRosterError("contact_roster exceeds 32 KiB")
        return raw
    if not isinstance(raw, str):
        raise ContactRosterError("contact_roster must be a JSON object")
    if not raw.strip():
        return {}
    if len(raw.encode("utf-8")) > 32768:
        raise ContactRosterError("contact_roster exceeds 32 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs_without_duplicates)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ContactRosterError("contact_roster must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ContactRosterError("contact_roster must be a JSON object")
    return value


def _private_text(value: Any, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContactRosterError(f"contact_roster {field_name} must be text")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ContactRosterError(
            f"contact_roster {field_name} exceeds {maximum} characters"
        )
    if _CONTROL.search(normalized):
        raise ContactRosterError(
            f"contact_roster {field_name} contains unsupported control characters"
        )
    return normalized


def _boolean(value: Any, field_name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ContactRosterError(
            f"contact_roster {field_name} must be a JSON boolean"
        )
    return value


def _timestamp(value: Any, field_name: str, *, now: datetime) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ContactRosterError(
            f"contact_roster {field_name} must be an ISO-8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContactRosterError(
            f"contact_roster {field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContactRosterError(
            f"contact_roster {field_name} must include a timezone"
        )
    normalized = parsed.astimezone(timezone.utc)
    if normalized > now:
        raise ContactRosterError(
            f"contact_roster {field_name} cannot be in the future"
        )
    return normalized.isoformat()


def parse_contact_roster(
    raw: Any,
    *,
    source_key: str,
    now: datetime,
) -> tuple[ContactRosterEntry, ...]:
    """Parse one private JSON roster without echoing rejected values."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContactRosterError("contact_roster validation clock must include a timezone")
    now = now.astimezone(timezone.utc)
    roster = _load_mapping(raw)
    if any(not isinstance(role, str) for role in roster):
        raise ContactRosterError(
            "contact_roster supports only publicist, manager, agent, and direct"
        )
    unknown_roles = sorted(set(roster) - ROSTER_ROLE_SET)
    if unknown_roles:
        raise ContactRosterError(
            "contact_roster supports only publicist, manager, agent, and direct"
        )
    entries: list[ContactRosterEntry] = []
    for role in ROSTER_ROLES:
        raw_entry = roster.get(role)
        if raw_entry is None:
            continue
        if not isinstance(raw_entry, Mapping):
            raise ContactRosterError(f"contact_roster {role} must be an object")
        if any(not isinstance(field_name, str) for field_name in raw_entry):
            raise ContactRosterError(
                f"contact_roster {role} contains unsupported fields"
            )
        unknown_fields = sorted(set(raw_entry) - _ENTRY_KEYS)
        if unknown_fields:
            raise ContactRosterError(
                f"contact_roster {role} contains unsupported fields"
            )
        name = _private_text(raw_entry.get("name"), f"{role}.name", 160)
        if name is None:
            raise ContactRosterError(f"contact_roster {role}.name is required")
        email = _private_text(raw_entry.get("email"), f"{role}.email", 254)
        if email is not None and _EMAIL.fullmatch(email) is None:
            raise ContactRosterError(
                f"contact_roster {role}.email is not a valid email address"
            )
        phone = _private_text(raw_entry.get("phone"), f"{role}.phone", 40)
        if phone is not None:
            digits = sum(character.isdigit() for character in phone)
            if _PHONE_CHARS.fullmatch(phone) is None or not 7 <= digits <= 15:
                raise ContactRosterError(
                    f"contact_roster {role}.phone is not a valid phone number"
                )
        notes = _private_text(raw_entry.get("notes"), f"{role}.notes", 1000)
        verified_at = _timestamp(
            raw_entry.get("verified_at"), f"{role}.verified_at", now=now
        )
        usable = _boolean(raw_entry.get("usable"), f"{role}.usable", default=False)
        preferred = _boolean(
            raw_entry.get("preferred"), f"{role}.preferred", default=False
        )
        permission_value = raw_entry.get("permission_status")
        if permission_value is not None and not isinstance(permission_value, str):
            raise ContactRosterError(
                f"contact_roster {role}.permission_status is unsupported"
            )
        permission_status = permission_value or (
            "permitted" if usable else "pending_verification"
        )
        if permission_status not in PERMISSION_STATUSES:
            raise ContactRosterError(
                f"contact_roster {role}.permission_status is unsupported"
            )
        provenance_value = raw_entry.get("provenance_ref")
        if provenance_value is not None and not isinstance(provenance_value, str):
            raise ContactRosterError(
                f"contact_roster {role}.provenance_ref must be opaque"
            )
        provenance_ref = (
            provenance_value or f"candidate-import://{source_key}/{role}"
        ).strip()
        if _OPAQUE_REFERENCE.fullmatch(provenance_ref) is None:
            raise ContactRosterError(
                f"contact_roster {role}.provenance_ref must be opaque"
            )
        if usable and (
            verified_at is None
            or (email is None and phone is None)
            or permission_status != "permitted"
        ):
            raise ContactRosterError(
                f"contact_roster {role} can be usable only with a verified, "
                "permitted route"
            )
        if permission_status in {"do_not_use", "opted_out"}:
            usable = False
            preferred = False
        entries.append(
            ContactRosterEntry(
                role=role,
                name=name,
                email=email,
                phone=phone,
                notes=notes,
                provenance_ref=provenance_ref,
                verified_at=verified_at,
                usable=usable,
                preferred=preferred,
                permission_status=permission_status,
            )
        )
    return tuple(entries)


def roster_id(
    tenant_id: str, show_id: str, opportunity_id: str, role: str
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:contact-roster:{tenant_id}:{show_id}:{opportunity_id}:{role}",
        )
    )


def _scope(
    *, tenant_id: str, show_id: str, roster_record_id: str, field_name: str
) -> encryption.PrivateFieldScope:
    return encryption.PrivateFieldScope(
        tenant_id=tenant_id,
        show_id=show_id,
        category="contact",
        owner_table="contact_rosters",
        owner_record_id=roster_record_id,
        field_name=field_name,
    )


def _put_if_changed(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault,
    *,
    reference: str | None,
    value: str | None,
    scope: encryption.PrivateFieldScope,
    actor_role: str,
) -> tuple[str | None, bool]:
    if value is None:
        return reference, False
    if reference:
        try:
            existing = vault.reveal_text(
                conn,
                reference,
                scope,
                actor_role=actor_role,
                allowed_roles=AUTHORIZED_CONTACT_ROLES,
            )
        except encryption.EncryptionError as exc:
            raise ContactRosterError(
                "existing contact roster ciphertext is unavailable", 409
            ) from exc
        if existing == value:
            return reference, False
    try:
        return vault.put_text(conn, scope, value, commit=False), True
    except encryption.EncryptionError as exc:
        raise ContactRosterError("contact roster encryption is unavailable", 503) from exc


def upsert_contact_roster(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault,
    entries: Sequence[ContactRosterEntry],
    *,
    tenant_id: str,
    show_id: str,
    opportunity_id: str,
    actor_role: str,
    timestamp: str,
) -> RosterUpsertResult:
    """Encrypt and idempotently upsert a validated roster in the caller transaction."""
    if actor_role not in AUTHORIZED_CONTACT_ROLES:
        raise ContactRosterError(
            "operator role cannot import contact roster values", 403
        )
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    for entry in entries:
        record_id = roster_id(tenant_id, show_id, opportunity_id, entry.role)
        existing = store.fetch_one(
            conn,
            "SELECT * FROM contact_rosters WHERE tenant_id = ? AND show_id = ? "
            "AND opportunity_id = ? AND role_kind = ?",
            (tenant_id, show_id, opportunity_id, entry.role),
        )
        if (
            existing is not None
            and existing["permission_status"] == "opted_out"
            and entry.permission_status != "opted_out"
        ):
            raise ContactRosterError(
                "an opted-out roster route cannot be reactivated", 409
            )

        private_refs: dict[str, str | None] = {}
        private_changed: list[str] = []
        for field_name in ROSTER_FIELD_NAMES:
            reference, changed = _put_if_changed(
                conn,
                vault,
                reference=(existing or {}).get(f"{field_name}_ref"),
                value=getattr(entry, field_name),
                scope=_scope(
                    tenant_id=tenant_id,
                    show_id=show_id,
                    roster_record_id=record_id,
                    field_name=field_name,
                ),
                actor_role=actor_role,
            )
            private_refs[f"{field_name}_ref"] = reference
            if changed:
                private_changed.append(field_name)

        values: dict[str, Any] = {
            "provenance_ref": entry.provenance_ref,
            "verified_at": entry.verified_at,
            "usable": entry.usable,
            "preferred": entry.preferred,
            "permission_status": entry.permission_status,
            **private_refs,
        }
        if existing is None:
            store.insert(
                conn,
                "contact_rosters",
                {
                    "id": record_id,
                    "tenant_id": tenant_id,
                    "show_id": show_id,
                    "opportunity_id": opportunity_id,
                    "role_kind": entry.role,
                    **values,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            created.append(entry.role)
            continue

        metadata_changes = {
            key: value for key, value in values.items() if existing.get(key) != value
        }
        if not metadata_changes and not private_changed:
            unchanged.append(entry.role)
            continue
        metadata_changes["updated_at"] = timestamp
        store.update(conn, "contact_rosters", existing["id"], metadata_changes)
        updated.append(entry.role)

    return RosterUpsertResult(tuple(created), tuple(updated), tuple(unchanged))


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    route_ref = row.get("email_ref") or row.get("phone_ref")
    return {
        "roster_id": row["id"],
        "role": row["role_kind"],
        "name_ref": row["name_ref"],
        "route_ref": route_ref,
        "notes_ref": row.get("notes_ref"),
        "provenance_ref": row["provenance_ref"],
        "verified_at": row.get("verified_at"),
        "usable": bool(row["usable"]),
        "preferred": bool(row["preferred"]),
        "permission_status": row["permission_status"],
    }


def public_contact_roster(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    opportunity_id: str,
) -> list[dict[str, Any]]:
    rows = store.fetch_all(
        conn,
        "SELECT * FROM contact_rosters WHERE tenant_id = ? AND show_id = ? "
        "AND opportunity_id = ? ORDER BY preferred DESC, role_kind",
        (tenant_id, show_id, opportunity_id),
    )
    return [_public_row(row) for row in rows]


def reveal_contact_roster(
    conn: store.DatabaseConnection,
    vault: encryption.FieldVault,
    *,
    tenant_id: str,
    show_id: str,
    opportunity_id: str,
    actor_role: str,
) -> list[dict[str, Any]]:
    """Reveal roster fields only to an authorized operator in exact scope."""
    if actor_role not in AUTHORIZED_CONTACT_ROLES:
        raise ContactRosterError(
            "operator role cannot view contact roster values", 403
        )
    rows = store.fetch_all(
        conn,
        "SELECT * FROM contact_rosters WHERE tenant_id = ? AND show_id = ? "
        "AND opportunity_id = ? ORDER BY preferred DESC, role_kind",
        (tenant_id, show_id, opportunity_id),
    )
    revealed: list[dict[str, Any]] = []
    for row in rows:
        item = _public_row(row)
        for field_name in ROSTER_FIELD_NAMES:
            reference = row.get(f"{field_name}_ref")
            if not reference:
                item[field_name] = None
                continue
            if _PRIVATE_REF.fullmatch(str(reference)) is None:
                raise ContactRosterError("contact roster reference is invalid", 409)
            try:
                item[field_name] = vault.reveal_text(
                    conn,
                    str(reference),
                    _scope(
                        tenant_id=tenant_id,
                        show_id=show_id,
                        roster_record_id=str(row["id"]),
                        field_name=field_name,
                    ),
                    actor_role=actor_role,
                    allowed_roles=AUTHORIZED_CONTACT_ROLES,
                )
            except encryption.EncryptionError as exc:
                raise ContactRosterError(
                    "contact roster ciphertext is unavailable", 503
                ) from exc
        revealed.append(item)
    return revealed


def invitation_prefill(roster: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Choose one verified, permitted route without sending or persisting it."""
    rank = {"direct": 0, "publicist": 1, "manager": 2, "agent": 3}
    eligible = [
        row
        for row in roster
        if row.get("usable") is True
        and row.get("permission_status") == "permitted"
        and row.get("verified_at")
        and (row.get("email") or row.get("phone"))
    ]
    if not eligible:
        return None
    selected = min(
        eligible,
        key=lambda row: (
            not bool(row.get("preferred")),
            rank.get(str(row.get("role")), 99),
        ),
    )
    route_kind = "email" if selected.get("email") else "phone"
    return {
        "role": selected["role"],
        "name": selected["name"],
        "route_kind": route_kind,
        "route_value": selected[route_kind],
        "route_ref": selected["route_ref"],
        "provenance_ref": selected["provenance_ref"],
        "verified_at": selected["verified_at"],
    }


__all__ = [
    "AUTHORIZED_CONTACT_ROLES",
    "ContactRosterEntry",
    "ContactRosterError",
    "ROSTER_ROLES",
    "RosterUpsertResult",
    "invitation_prefill",
    "parse_contact_roster",
    "public_contact_roster",
    "reveal_contact_roster",
    "roster_id",
    "upsert_contact_roster",
]
