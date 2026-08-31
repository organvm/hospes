"""Private, idempotent candidate ingestion for the SQLite operator store.

This module is a library boundary suitable for a thin CLI wrapper. It accepts
runtime rows or a CSV path, validates the whole batch before writing, and
upserts each opportunity by ``(tenant_id, show_id, source_key)``. The source key
must be an opaque identifier owned by the external candidate system; guest
names are never used as identity keys.

Raw contact values are admitted only inside the versioned ``contact_roster``
JSON column and are encrypted before persistence. Top-level contact values,
correspondence bodies, signed documents, studio addresses, and private notes
remain rejected. Public projections receive only opaque references.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO
from uuid import NAMESPACE_URL, uuid4, uuid5

from . import contact_roster, encryption, guest_crm, privacy, store
from .suggest import ROUTE_LABELS, ROUTE_TYPES, SUGGESTION_FORMAT


_BOUNDARY_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
_SOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}$")
_ROUTE_TYPE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_SEASON_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,31}$")
_OPAQUE_REFERENCE = re.compile(
    r"^[a-z][a-z0-9_-]{1,31}://[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+-]{1,207}$"
)
_RELATIONSHIP_CLASSES = {"C0", "C1", "C2", "C3", "C4", "C5"}
_REQUIRED_FIELDS = (
    "source_key",
    "guest_name",
    "why_guest",
    "why_now",
    "episode_thesis",
    "proposed_artifact",
    "relationship_class",
    "relationship_owner",
    "route_type",
    "route_reference",
    "route_verified_at",
    "preferred_city",
    "social_cost_1_5",
    "ari_effort",
    "next_action",
    "source_provenance",
)
_PRIVATE_FIELDS = {
    "email",
    "email_address",
    "phone",
    "phone_number",
    "studio_address",
    "home_address",
    "correspondence",
    "correspondence_body",
    "message_body",
    "private_notes",
    "notes",
    "signed_document",
    "signed_release",
}
_TEXT_LIMITS = {
    "guest_name": (2, 160),
    "guest_id": (1, 240),
    "season": (1, 32),
    "episode": (1, 32),
    "why_guest": (10, 2000),
    "why_now": (10, 2000),
    "episode_thesis": (20, 4000),
    "proposed_artifact": (3, 1000),
    "relationship_owner": (2, 80),
    "preferred_city": (2, 120),
    "ari_effort": (2, 120),
    "next_action": (3, 2000),
    "source_provenance": (10, 2000),
    "category": (0, 120),
    "next_action_date": (0, 40),
}


@dataclass(frozen=True)
class CandidateImportIssue:
    row_index: int
    field_name: str
    message: str


class CandidateImportError(ValueError):
    """A batch validation error. No rows are written when this is raised."""

    def __init__(self, issues: Sequence[CandidateImportIssue]):
        self.issues = list(issues)
        summary = "; ".join(
            f"row {issue.row_index} {issue.field_name}: {issue.message}"
            for issue in self.issues[:5]
        )
        if len(self.issues) > 5:
            summary += f"; plus {len(self.issues) - 5} more issue(s)"
        super().__init__(summary)


@dataclass(frozen=True)
class CandidateRecord:
    source_key: str
    guest_name: str
    why_guest: str
    why_now: str
    episode_thesis: str
    proposed_artifact: str
    relationship_class: str
    relationship_owner: str
    preferred_city: str
    social_cost_1_5: int
    ari_effort: str
    next_action: str
    source_provenance: str
    route_type: str
    route_reference: str
    route_verified_at: str
    route_usable: bool = True
    guest_id: str = ""
    season: str | None = None
    episode: str | None = None
    do_not_contact: bool = False
    source_kind: str = "private_import"
    category: str | None = None
    next_action_date: str | None = None
    contact_roster_entries: tuple[contact_roster.ContactRosterEntry, ...] = field(
        default=(), repr=False
    )


@dataclass
class CandidateImportResult:
    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    unchanged_ids: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return len(self.created_ids)

    @property
    def updated(self) -> int:
        return len(self.updated_ids)

    @property
    def unchanged(self) -> int:
        return len(self.unchanged_ids)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready summary for a future CLI adapter."""
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "created_ids": list(self.created_ids),
            "updated_ids": list(self.updated_ids),
            "unchanged_ids": list(self.unchanged_ids),
        }


def candidate_id(tenant_id: str, show_id: str, source_key: str) -> str:
    """Return the stable opportunity id for a tenant/show-scoped external key."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"hospes:candidate:{tenant_id}:{show_id}:{source_key}",
        )
    )


def _text(row: Mapping[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if value is None:
        return ""
    return str(value).strip()


def _validate_boundary(value: str, field_name: str) -> None:
    if not _BOUNDARY_ID.fullmatch(value):
        raise CandidateImportError([
            CandidateImportIssue(-1, field_name, f"must match {_BOUNDARY_ID.pattern}")
        ])


def _validate_rows(
    rows: Sequence[Mapping[str, Any]], *, now: datetime
) -> list[CandidateRecord]:
    issues: list[CandidateImportIssue] = []
    records: list[CandidateRecord] = []
    seen_keys: set[str] = set()

    for row_index, raw_row in enumerate(rows):
        row = dict(raw_row)
        suggestion_format = _text(row, "suggestion_format")
        source_kind = "private_import"
        if suggestion_format:
            if suggestion_format != SUGGESTION_FORMAT:
                issues.append(CandidateImportIssue(
                    row_index,
                    "suggestion_format",
                    "is not a supported suggestion envelope",
                ))
            else:
                source_kind = "public_suggestion"
                # ``notes`` is required in the public display contract but is
                # not an admitted persistence field.  Discard it before the
                # ordinary private-content guard runs.
                row["notes"] = ""

        for field_name in _PRIVATE_FIELDS:
            if _text(row, field_name):
                issues.append(CandidateImportIssue(
                    row_index,
                    field_name,
                    "private content must remain in its external owner",
                ))

        for field_name in (
            *_REQUIRED_FIELDS,
            "category",
            "next_action_date",
            "guest_id",
            "season",
            "episode",
        ):
            value = _text(row, field_name)
            contact_kind = privacy.contact_kind(value) if value else None
            if contact_kind:
                issues.append(CandidateImportIssue(
                    row_index,
                    field_name,
                    f"{contact_kind} must remain in its external owner",
                ))

        for field_name in _REQUIRED_FIELDS:
            if not _text(row, field_name):
                issues.append(CandidateImportIssue(row_index, field_name, "is required"))

        source_key = _text(row, "source_key")
        if source_key and not _SOURCE_KEY.fullmatch(source_key):
            issues.append(CandidateImportIssue(
                row_index,
                "source_key",
                "must be an opaque key containing only letters, digits, '.', '_', ':', '/', or '-'",
            ))
        elif source_key in seen_keys:
            issues.append(CandidateImportIssue(
                row_index, "source_key", "is duplicated within this import batch"
            ))
        elif source_key:
            seen_keys.add(source_key)

        try:
            roster_entries = contact_roster.parse_contact_roster(
                row.get("contact_roster"), source_key=source_key, now=now
            )
        except contact_roster.ContactRosterError as exc:
            roster_entries = ()
            issues.append(
                CandidateImportIssue(row_index, "contact_roster", str(exc))
            )

        guest_id = _text(row, "guest_id")
        if guest_id and not _SOURCE_KEY.fullmatch(guest_id):
            issues.append(CandidateImportIssue(
                row_index,
                "guest_id",
                "must be an opaque cross-season key owned by the external system",
            ))
        for field_name in ("season", "episode"):
            value = _text(row, field_name)
            if value and not _SEASON_LABEL.fullmatch(value):
                issues.append(CandidateImportIssue(
                    row_index, field_name, "must be a short opaque label"
                ))

        do_not_contact_raw = row.get("do_not_contact")
        if do_not_contact_raw is None or _text(row, "do_not_contact") == "":
            do_not_contact = False
        elif isinstance(do_not_contact_raw, bool):
            do_not_contact = do_not_contact_raw
        elif isinstance(do_not_contact_raw, str) and do_not_contact_raw.casefold() in {
            "true",
            "false",
        }:
            do_not_contact = do_not_contact_raw.casefold() == "true"
        else:
            do_not_contact = False
            issues.append(CandidateImportIssue(
                row_index,
                "do_not_contact",
                "must be the explicit boolean true or false",
            ))

        relationship_class = _text(row, "relationship_class").upper()
        if relationship_class and relationship_class not in _RELATIONSHIP_CLASSES:
            issues.append(CandidateImportIssue(
                row_index,
                "relationship_class",
                f"must be one of {sorted(_RELATIONSHIP_CLASSES)}",
            ))

        route_usable_raw = row.get("route_usable")
        if route_usable_raw is None or _text(row, "route_usable") == "":
            route_usable = True
        elif isinstance(route_usable_raw, bool):
            route_usable = route_usable_raw
        elif isinstance(route_usable_raw, str) and route_usable_raw.casefold() in {
            "true",
            "false",
        }:
            route_usable = route_usable_raw.casefold() == "true"
        else:
            route_usable = True
            issues.append(CandidateImportIssue(
                row_index,
                "route_usable",
                "must be the explicit boolean true or false",
            ))

        if suggestion_format == SUGGESTION_FORMAT:
            expected_route = ROUTE_LABELS.get(relationship_class)
            expected_type = ROUTE_TYPES.get(relationship_class)
            if route_usable:
                issues.append(CandidateImportIssue(
                    row_index,
                    "route_usable",
                    "public suggestions must remain unusable until human verification",
                ))
            if _text(row, "name") != _text(row, "guest_name"):
                issues.append(CandidateImportIssue(
                    row_index,
                    "name",
                    "must match guest_name in a suggestion envelope",
                ))
            if _text(row, "estimated_social_cost") != _text(
                row, "social_cost_1_5"
            ):
                issues.append(CandidateImportIssue(
                    row_index,
                    "estimated_social_cost",
                    "must match social_cost_1_5 in a suggestion envelope",
                ))
            if expected_route is None or _text(row, "suggested_route") != expected_route:
                issues.append(CandidateImportIssue(
                    row_index,
                    "suggested_route",
                    "does not match the relationship-class route policy",
                ))
            if expected_type is None or _text(row, "route_type") != expected_type:
                issues.append(CandidateImportIssue(
                    row_index,
                    "route_type",
                    "does not match the relationship-class route policy",
                ))

        owner = _text(row, "relationship_owner")
        if owner and not _BOUNDARY_ID.fullmatch(owner):
            issues.append(CandidateImportIssue(
                row_index, "relationship_owner", "must be an opaque lower-case owner id"
            ))

        route_type = _text(row, "route_type")
        if route_type and not _ROUTE_TYPE.fullmatch(route_type):
            issues.append(CandidateImportIssue(
                row_index, "route_type", "must be a bounded lower-case route kind"
            ))
        route_reference = _text(row, "route_reference")
        if route_reference and not _OPAQUE_REFERENCE.fullmatch(route_reference):
            issues.append(CandidateImportIssue(
                row_index,
                "route_reference",
                "must be an opaque owner reference, never an address",
            ))
        route_verified_at = _text(row, "route_verified_at")
        normalized_route_time = route_verified_at
        if route_verified_at:
            try:
                parsed_route_time = datetime.fromisoformat(route_verified_at)
                if parsed_route_time.tzinfo is None or parsed_route_time.utcoffset() is None:
                    raise ValueError
                parsed_route_time = parsed_route_time.astimezone(timezone.utc)
                if parsed_route_time > now:
                    issues.append(CandidateImportIssue(
                        row_index,
                        "route_verified_at",
                        "cannot be in the future",
                    ))
                normalized_route_time = parsed_route_time.isoformat()
            except ValueError:
                issues.append(CandidateImportIssue(
                    row_index,
                    "route_verified_at",
                    "must be an ISO-8601 datetime with timezone",
                ))

        social_cost_raw = _text(row, "social_cost_1_5")
        try:
            social_cost = int(social_cost_raw)
        except ValueError:
            social_cost = 0
        if social_cost_raw and social_cost not in range(1, 6):
            issues.append(CandidateImportIssue(
                row_index, "social_cost_1_5", "must be an integer from 1 through 5"
            ))

        for field_name, (minimum, maximum) in _TEXT_LIMITS.items():
            value = _text(row, field_name)
            if value and not minimum <= len(value) <= maximum:
                issues.append(CandidateImportIssue(
                    row_index,
                    field_name,
                    f"must be {minimum}-{maximum} characters",
                ))

        if any(issue.row_index == row_index for issue in issues):
            continue
        records.append(CandidateRecord(
            source_key=source_key,
            guest_name=_text(row, "guest_name"),
            why_guest=_text(row, "why_guest"),
            why_now=_text(row, "why_now"),
            episode_thesis=_text(row, "episode_thesis"),
            proposed_artifact=_text(row, "proposed_artifact"),
            relationship_class=relationship_class,
            relationship_owner=owner,
            preferred_city=_text(row, "preferred_city"),
            social_cost_1_5=social_cost,
            ari_effort=_text(row, "ari_effort"),
            next_action=_text(row, "next_action"),
            source_provenance=_text(row, "source_provenance"),
            route_type=route_type,
            route_reference=route_reference,
            route_verified_at=normalized_route_time,
            route_usable=route_usable,
            guest_id=guest_id or source_key,
            season=_text(row, "season") or None,
            episode=_text(row, "episode") or None,
            do_not_contact=do_not_contact,
            source_kind=source_kind,
            category=_text(row, "category") or None,
            next_action_date=_text(row, "next_action_date") or None,
            contact_roster_entries=roster_entries,
        ))

    if issues:
        raise CandidateImportError(issues)
    return records


def _import_values(
    record: CandidateRecord,
    *,
    tenant_id: str,
    network_id: str,
    show_id: str,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "network_id": network_id,
        "show_id": show_id,
        "source_key": record.source_key,
        "guest_id": record.guest_id,
        "season": record.season,
        "episode": record.episode,
        "guest_name": record.guest_name,
        "category": record.category,
        "why_guest": record.why_guest,
        "why_now": record.why_now,
        "episode_thesis": record.episode_thesis,
        "proposed_artifact": record.proposed_artifact,
        "relationship_class": record.relationship_class,
        "relationship_owner": record.relationship_owner,
        "preferred_city": record.preferred_city,
        "social_cost_1_5": record.social_cost_1_5,
        "ari_effort": record.ari_effort,
        "next_action": record.next_action,
        "next_action_date": record.next_action_date,
        "source_provenance": record.source_provenance,
    }


def _route_values(
    record: CandidateRecord,
    *,
    tenant_id: str,
    opportunity_id: str,
    usable: bool = True,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "opportunity_id": opportunity_id,
        "route_type": record.route_type,
        "route_label": record.route_reference,
        "source_provenance": record.source_provenance,
        "verified_at": record.route_verified_at,
        "usable": record.route_usable and usable,
    }


def _ensure_show_registry(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    timestamp: str,
) -> None:
    existing = store.fetch_one(
        conn,
        "SELECT id FROM show_registry WHERE tenant_id = ? AND show_id = ?",
        (tenant_id, show_id),
    )
    if existing is not None:
        return
    store.insert(
        conn,
        "show_registry",
        {
            "id": str(uuid5(NAMESPACE_URL, f"hospes:show:{tenant_id}:{show_id}")),
            "tenant_id": tenant_id,
            "show_id": show_id,
            "label": show_id,
            "config_ref": f"candidate-import://shows/{show_id}",
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )


def _apply_directive(
    conn: store.DatabaseConnection,
    record: CandidateRecord,
    *,
    tenant_id: str,
    show_id: str,
    opportunity_id: str,
    actor_id: str,
    actor_role: str,
    now: datetime,
) -> bool:
    """Honor an import row that declares the guest is never to be contacted."""
    directive = guest_crm.set_directive(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        guest_id=record.guest_id,
        do_not_contact=True,
        actor_id=actor_id,
        actor_role=actor_role,
        opportunity_id=opportunity_id,
        source="import",
        now=now,
        commit=False,
    )
    return bool(directive["changed"])


def import_candidates(
    conn: store.DatabaseConnection,
    rows: Iterable[Mapping[str, Any]],
    *,
    tenant_id: str,
    network_id: str,
    show_id: str,
    actor_id: str,
    actor_role: str = "producer",
    now: datetime | None = None,
    field_vault: encryption.FieldVault | None = None,
) -> CandidateImportResult:
    """Atomically create/update candidate opportunities from private rows.

    An unchanged re-import performs no write and appends no audit event. Updates
    preserve lifecycle ``status`` and ``disposition``; ingestion cannot silently
    advance or reopen an opportunity.
    """
    for value, field_name in (
        (tenant_id, "tenant_id"),
        (network_id, "network_id"),
        (show_id, "show_id"),
        (actor_id, "actor_id"),
        (actor_role, "actor_role"),
    ):
        _validate_boundary(value, field_name)

    materialized_rows = list(rows)
    boundary_issues: list[CandidateImportIssue] = []
    for row_index, row in enumerate(materialized_rows):
        for field_name, expected in (
            ("tenant_id", tenant_id),
            ("network_id", network_id),
            ("show_id", show_id),
        ):
            declared = _text(row, field_name)
            if declared and declared != expected:
                boundary_issues.append(CandidateImportIssue(
                    row_index,
                    field_name,
                    "does not match the authenticated import boundary",
                ))
    if boundary_issues:
        raise CandidateImportError(boundary_issues)

    effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records = _validate_rows(materialized_rows, now=effective_now)
    missing_vault_issues = [
        CandidateImportIssue(
            index,
            "contact_roster",
            "encrypted field vault is required for contact roster imports",
        )
        for index, record in enumerate(records)
        if record.contact_roster_entries and field_vault is None
    ]
    if missing_vault_issues:
        raise CandidateImportError(missing_vault_issues)
    row_index_by_key = {
        _text(row, "source_key"): index for index, row in enumerate(materialized_rows)
    }
    directive_issues: list[CandidateImportIssue] = []
    for record in records:
        row_index = row_index_by_key[record.source_key]
        if record.do_not_contact:
            if actor_role not in guest_crm.DIRECTIVE_ROLES:
                directive_issues.append(CandidateImportIssue(
                    row_index,
                    "do_not_contact",
                    "may be declared only by the relationship owner",
                ))
            continue
        if guest_crm.is_do_not_contact(
            conn, tenant_id=tenant_id, show_id=show_id, guest_id=record.guest_id
        ):
            directive_issues.append(CandidateImportIssue(
                row_index,
                "guest_id",
                "is marked do-not-contact and cannot be re-imported",
            ))
    if directive_issues:
        raise CandidateImportError(directive_issues)
    timestamp = effective_now.isoformat()
    result = CandidateImportResult()

    conn.execute("SAVEPOINT candidate_import")
    try:
        if records:
            _ensure_show_registry(
                conn,
                tenant_id=tenant_id,
                show_id=show_id,
                timestamp=timestamp,
            )
        for record in records:
            values = _import_values(
                record, tenant_id=tenant_id, network_id=network_id, show_id=show_id
            )
            existing = store.fetch_one(
                conn,
                "SELECT * FROM appearance_opportunities "
                "WHERE tenant_id = ? AND show_id = ? AND source_key = ?",
                (tenant_id, show_id, record.source_key),
            )
            opportunity_id = (
                existing["id"]
                if existing is not None
                else candidate_id(tenant_id, show_id, record.source_key)
            )
            route_values = _route_values(
                record,
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                usable=existing is None or existing.get("disposition") != "PROTECTED",
            )

            if existing is None:
                store.insert(conn, "appearance_opportunities", {
                    "id": opportunity_id,
                    **values,
                    "status": "EDITORIAL_REVIEW",
                    "disposition": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                })
                store.insert(conn, "contact_routes", {
                    "id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"hospes:route:{tenant_id}:{show_id}:{record.source_key}",
                        )
                    ),
                    **route_values,
                    "created_at": timestamp,
                })
                roster_result = contact_roster.RosterUpsertResult()
                if record.contact_roster_entries:
                    assert field_vault is not None
                    try:
                        roster_result = contact_roster.upsert_contact_roster(
                            conn,
                            field_vault,
                            record.contact_roster_entries,
                            tenant_id=tenant_id,
                            show_id=show_id,
                            opportunity_id=opportunity_id,
                            actor_role=actor_role,
                            timestamp=timestamp,
                        )
                    except (contact_roster.ContactRosterError, encryption.EncryptionError) as exc:
                        raise CandidateImportError([
                            CandidateImportIssue(
                                row_index_by_key[record.source_key],
                                "contact_roster",
                                str(exc),
                            )
                        ]) from exc
                store.insert(conn, "audit_events", {
                    "id": str(uuid4()),
                    "tenant_id": tenant_id,
                    "show_id": show_id,
                    "opportunity_id": opportunity_id,
                    "event_type": "appearance.candidate_created",
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "details": {
                        "source_key": record.source_key,
                        "source": record.source_kind,
                    },
                    "created_at": timestamp,
                })
                store.insert(conn, "audit_events", {
                    "id": str(uuid4()),
                    "tenant_id": tenant_id,
                    "show_id": show_id,
                    "opportunity_id": opportunity_id,
                    "event_type": (
                        "contact_route.verified"
                        if record.route_usable
                        else "contact_route.proposed"
                    ),
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "details": {
                        "route_type": record.route_type,
                        "source": record.source_kind,
                    },
                    "created_at": timestamp,
                })
                if record.do_not_contact:
                    _apply_directive(
                        conn,
                        record,
                        tenant_id=tenant_id,
                        show_id=show_id,
                        opportunity_id=opportunity_id,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        now=effective_now,
                    )
                if roster_result.changed:
                    store.insert(conn, "audit_events", {
                        "id": str(uuid4()),
                        "tenant_id": tenant_id,
                        "show_id": show_id,
                        "opportunity_id": opportunity_id,
                        "event_type": "contact_roster.imported",
                        "actor_id": actor_id,
                        "actor_role": actor_role,
                        "details": {
                            "roles": sorted(roster_result.created_roles),
                            "source": record.source_kind,
                        },
                        "created_at": timestamp,
                    })
                result.created_ids.append(opportunity_id)
                continue

            if existing["network_id"] != network_id or existing["show_id"] != show_id:
                raise CandidateImportError([CandidateImportIssue(
                    row_index_by_key[record.source_key],
                    "source_key",
                    "already belongs to another network/show within this tenant",
                )])

            opportunity_changes = {
                key: value for key, value in values.items()
                if key not in {"tenant_id", "network_id", "show_id"}
                and existing.get(key) != value
            }
            existing_route = store.fetch_one(
                conn,
                "SELECT * FROM contact_routes WHERE opportunity_id = ?",
                (existing["id"],),
            )
            route_changes = {
                key: value for key, value in route_values.items()
                if key not in {"tenant_id", "opportunity_id"}
                and (existing_route or {}).get(key) != value
            }
            roster_result = contact_roster.RosterUpsertResult()
            if record.contact_roster_entries:
                assert field_vault is not None
                try:
                    roster_result = contact_roster.upsert_contact_roster(
                        conn,
                        field_vault,
                        record.contact_roster_entries,
                        tenant_id=tenant_id,
                        show_id=show_id,
                        opportunity_id=existing["id"],
                        actor_role=actor_role,
                        timestamp=timestamp,
                    )
                except (contact_roster.ContactRosterError, encryption.EncryptionError) as exc:
                    raise CandidateImportError([
                        CandidateImportIssue(
                            row_index_by_key[record.source_key],
                            "contact_roster",
                            str(exc),
                        )
                    ]) from exc
            directive_changed = False
            if record.do_not_contact:
                directive_changed = _apply_directive(
                    conn,
                    record,
                    tenant_id=tenant_id,
                    show_id=show_id,
                    opportunity_id=existing["id"],
                    actor_id=actor_id,
                    actor_role=actor_role,
                    now=effective_now,
                )
            if (
                not opportunity_changes
                and not route_changes
                and not roster_result.changed
                and not directive_changed
            ):
                result.unchanged_ids.append(existing["id"])
                continue

            if opportunity_changes:
                opportunity_changes["updated_at"] = timestamp
                store.update(
                    conn, "appearance_opportunities", existing["id"], opportunity_changes
                )
            if existing_route is None:
                store.insert(conn, "contact_routes", {
                    "id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"hospes:route:{tenant_id}:{show_id}:{record.source_key}",
                        )
                    ),
                    **route_values,
                    "created_at": timestamp,
                })
            elif route_changes:
                store.update(conn, "contact_routes", existing_route["id"], route_changes)
            store.insert(conn, "audit_events", {
                "id": str(uuid4()),
                "tenant_id": tenant_id,
                "show_id": show_id,
                "opportunity_id": existing["id"],
                "event_type": "correction.appended",
                "actor_id": actor_id,
                "actor_role": actor_role,
                "details": {
                    "source_key": record.source_key,
                    "source": record.source_kind,
                    "changed_fields": sorted(
                        [key for key in opportunity_changes if key != "updated_at"]
                        + [f"route.{key}" for key in route_changes]
                        + roster_result.changed_fields
                        + (["do_not_contact"] if directive_changed else [])
                    ),
                },
                "created_at": timestamp,
            })
            result.updated_ids.append(existing["id"])

        conn.execute("RELEASE SAVEPOINT candidate_import")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT candidate_import")
        conn.execute("RELEASE SAVEPOINT candidate_import")
        raise

    return result


def import_candidate_file(
    conn: store.DatabaseConnection,
    csv_path: str | Path,
    **kwargs: Any,
) -> CandidateImportResult:
    """Load a UTF-8 CSV and pass it to :func:`import_candidates`."""
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return import_candidate_stream(conn, handle, **kwargs)


def import_candidate_stream(
    conn: store.DatabaseConnection,
    stream: TextIO,
    **kwargs: Any,
) -> CandidateImportResult:
    """Read a CSV stream, including CLI stdin, without retaining its content."""
    try:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise CandidateImportError([
                CandidateImportIssue(-1, "csv", "must contain a header row")
            ])
        fields = [field.strip() for field in reader.fieldnames]
        if any(not field for field in fields) or len(fields) != len(set(fields)):
            raise CandidateImportError([
                CandidateImportIssue(
                    -1, "csv", "headers must be non-empty and unique"
                )
            ])
        reader.fieldnames = fields
        rows = [dict(row) for row in reader]
    except csv.Error as exc:
        raise CandidateImportError([
            CandidateImportIssue(-1, "csv", f"cannot parse CSV: {exc}")
        ]) from exc
    if any(None in row for row in rows):
        raise CandidateImportError([
            CandidateImportIssue(-1, "csv", "rows cannot contain extra columns")
        ])
    return import_candidates(conn, rows, **kwargs)
