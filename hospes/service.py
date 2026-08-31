"""Guest-operations service layer — human-gated, keyless, DRAFTS-never-SENDS.

Ported from the overnight ``agent/hospes-core-api`` slice (PR #2). The original
was a FastAPI + SQLAlchemy + Pydantic app; this is its domain layer folded onto
the canonical HOSPES engine:

* **Persistence** is stdlib :mod:`sqlite3` via :mod:`hospes.store` (was
  SQLAlchemy ORM). The store file is a runtime artifact under ``out/`` with a
  ``HOSPES_DB`` override.
* **Validation** is plain dataclass payloads + explicit checks (was Pydantic).
* **Engine reuse** — this layer does not reinvent rules the engine already
  owns:
  - lifecycle statuses are validated against the canonical state machine
    (:mod:`hospes.states`); an operation that would land a non-canonical state
    fails loudly rather than inventing one.
  - the protected-relationship classes (C4/C5) come from
    :data:`hospes.drafts.PROTECTED_CLASSES`.
  - the correspondence draft carries the same DRAFT-NOT-SENT banner the
    file-based engine stamps (:data:`hospes.drafts.DRAFT_BANNER`), and there is
    deliberately **no send/deliver operation** anywhere in this module.

Every mutating operation writes an ``audit_events`` row, mirroring the engine's
append-only audit discipline.

This module is fully usable and testable **without** the optional FastAPI
extra: the HTTP surface in :mod:`hospes.api` is a thin adapter over these
functions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from . import contact_roster, drafts, generation, guest_crm, notifications, privacy, states, store

# ---------------------------------------------------------------------------
# Enumerations (ported from schemas.py StrEnum -> stdlib str Enum).
# ---------------------------------------------------------------------------


class RelationshipClass(str, Enum):
    C0 = "C0"
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"
    C5 = "C5"


class HumanRole(str, Enum):
    HOST = "host"
    NETWORK_OPERATOR = "network_operator"
    PRODUCER = "producer"
    EDITORIAL_OWNER = "editorial_owner"
    RELATIONSHIP_OWNER = "relationship_owner"
    # Post-production. The show registry has always declared this role; it
    # receives clip work and never gains a lifecycle write permission.
    EDITOR = "editor"


class DecisionAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    PROTECT = "protect"
    NOTE = "note"


class DraftKind(str, Enum):
    INITIAL = "initial"
    INVITATION = "invitation"
    FOLLOW_UP = "follow_up"
    THANK_YOU = "thank_you"


class StudioCity(str, Enum):
    LOS_ANGELES = "Los Angeles"
    NEW_YORK_CITY = "New York City"
    AUSTIN = "Austin"


class ReceiptType(str, Enum):
    OUTREACH_SENT = "outreach.sent"
    REPLY_CLASSIFIED = "reply.classified"
    BOOKING_CONFIRMED = "booking.confirmed"
    CONSENT_SIGNED = "consent.signed"
    RECORDING_READY = "recording.ready"
    RECORDING_COMPLETED = "recording.completed"
    MEDIA_INGESTED = "media.ingested"


# The preflight package proves capture readiness. Post-recording masters are
# represented only by opaque ``media.ingested`` receipt references.
REQUIRED_PREFLIGHT_ASSETS = {
    "environmental_master",
    "host_singles",
    "safety_microphone",
    "backup_recorder",
    "room_tone",
    "slate",
}

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
_ROUTE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_ASSET_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_OPAQUE_REFERENCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}://[A-Za-z0-9][A-Za-z0-9._~:/?#=&%+-]{1,207}$")

REPLY_CLASSIFICATIONS = frozenset(
    {
        "POSITIVE_INTEREST",
        "PROPOSED_TIMES",
        "NEEDS_MORE_INFORMATION",
        "CONTACT_PUBLICIST",
        "CONTACT_ASSISTANT",
        "FOLLOW_UP_LATER",
        "SOFT_DECLINE",
        "HARD_DECLINE",
        "UNSUBSCRIBE",
        "FEE_REQUEST",
        "TRAVEL_REQUEST",
        "TOPIC_CONCERN",
        "AMBIGUOUS",
        "REQUIRES_HUMAN",
    }
)

#: A terminal decision is the season's memory of this guest. The map is
#: explicit so a new DecisionAction cannot silently write no history at all.
DECISION_DISPOSITIONS = {
    "approve": "APPROVED",
    "reject": "DECLINED",
    "protect": "PROTECTED",
}

OUTREACH_DRAFTABLE_STATES = frozenset(
    {
        "APPROVED",
        "CONTACT_ROUTE_IDENTIFIED",
        "OUTREACH_DRAFTED",
        "OUTREACH_APPROVED",
    }
)

_RECEIPT_DETAIL_KEYS = {
    ReceiptType.OUTREACH_SENT: frozenset(),
    ReceiptType.REPLY_CLASSIFIED: frozenset({"classification"}),
    ReceiptType.BOOKING_CONFIRMED: frozenset({"studio_ref", "producer_ref", "recording_time"}),
    ReceiptType.CONSENT_SIGNED: frozenset({"private_pilot", "clip_scope"}),
    ReceiptType.RECORDING_READY: frozenset({"preflight_ref", "asset_package_id"}),
    ReceiptType.RECORDING_COMPLETED: frozenset({"session_kind"}),
    ReceiptType.MEDIA_INGESTED: frozenset({"master_ref", "checksum_ref"}),
}

_RECEIPT_ROLES = {
    ReceiptType.OUTREACH_SENT: {
        HumanRole.PRODUCER,
        HumanRole.EDITORIAL_OWNER,
        HumanRole.RELATIONSHIP_OWNER,
    },
    ReceiptType.REPLY_CLASSIFIED: {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
    ReceiptType.BOOKING_CONFIRMED: {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
    ReceiptType.CONSENT_SIGNED: {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
    ReceiptType.RECORDING_READY: {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
    ReceiptType.RECORDING_COMPLETED: {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
    ReceiptType.MEDIA_INGESTED: {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
}


# ---------------------------------------------------------------------------
# Errors and actor.
# ---------------------------------------------------------------------------


class DomainError(ValueError):
    """A domain-rule violation. ``status_code`` maps to an HTTP status in api.py."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class HumanActor:
    actor_id: str
    role: HumanRole
    tenant_id: str


# ---------------------------------------------------------------------------
# Request payloads (ported from Pydantic BaseModels -> validated dataclasses).
# Each ``from_dict`` performs the same validation the Pydantic model did, so the
# HTTP adapter and direct callers share one validation path.
# ---------------------------------------------------------------------------


class ValidationError(DomainError):
    def __init__(self, detail: str) -> None:
        super().__init__(422, detail)


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise ValidationError(detail)


def _str_field(data: dict[str, Any], key: str, *, min_len: int, max_len: int) -> str:
    value = data.get(key)
    _require(isinstance(value, str), f"{key} must be a string")
    value = value.strip() if key == "note" else value
    _require(min_len <= len(value) <= max_len, f"{key} must be {min_len}-{max_len} chars")
    return value


def _optional_str_field(data: dict[str, Any], key: str, *, min_len: int, max_len: int) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    _require(isinstance(value, str), f"{key} must be a string")
    value = value.strip()
    _require(min_len <= len(value) <= max_len, f"{key} must be {min_len}-{max_len} chars")
    return value


def _opaque_reference(value: Any, key: str = "external_reference") -> str:
    _require(isinstance(value, str), f"{key} must be a string")
    _require(
        bool(_OPAQUE_REFERENCE_PATTERN.fullmatch(value)),
        f"{key} must be an opaque owner reference such as owner://record/id",
    )
    _require(
        privacy.contact_kind(value) is None,
        f"{key} must not contain contact data",
    )
    return value


def _enum_field(data: dict[str, Any], key: str, enum: type[Enum]) -> Any:
    value = data.get(key)
    try:
        return enum(value)
    except (ValueError, KeyError) as exc:
        raise ValidationError(f"{key} must be one of {[e.value for e in enum]}") from exc


def _parse_aware_datetime(value: Any, key: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        _require(isinstance(value, str), f"{key} must be an ISO-8601 datetime string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{key} is not a valid ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{key} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class OpportunityCreate:
    tenant_id: str
    network_id: str
    show_id: str
    guest_name: str
    why_guest: str
    why_now: str
    proposed_artifact: str
    relationship_class: RelationshipClass
    relationship_owner: Optional[str]
    social_cost_1_5: Optional[int]
    ari_effort: Optional[str]
    preferred_city: Optional[StudioCity]
    next_action: Optional[str]
    source_provenance: Optional[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpportunityCreate":
        for key in ("tenant_id", "network_id", "show_id"):
            value = data.get(key)
            _require(
                isinstance(value, str) and bool(_ID_PATTERN.fullmatch(value)), f"{key} must match {_ID_PATTERN.pattern}"
            )
        social_cost = data.get("social_cost_1_5")
        if social_cost is not None:
            _require(
                isinstance(social_cost, int) and not isinstance(social_cost, bool) and 1 <= social_cost <= 5,
                "social_cost_1_5 must be an integer from 1 to 5",
            )
        preferred_city = data.get("preferred_city")
        if preferred_city is not None:
            try:
                preferred_city = StudioCity(preferred_city)
            except ValueError as exc:
                raise ValidationError(f"preferred_city must be one of {[city.value for city in StudioCity]}") from exc
        parsed = cls(
            tenant_id=data["tenant_id"],
            network_id=data["network_id"],
            show_id=data["show_id"],
            guest_name=_str_field(data, "guest_name", min_len=2, max_len=160),
            why_guest=_str_field(data, "why_guest", min_len=10, max_len=2000),
            why_now=_str_field(data, "why_now", min_len=10, max_len=2000),
            proposed_artifact=_str_field(data, "proposed_artifact", min_len=3, max_len=1000),
            relationship_class=_enum_field(data, "relationship_class", RelationshipClass),
            relationship_owner=_optional_str_field(data, "relationship_owner", min_len=2, max_len=120),
            social_cost_1_5=social_cost,
            ari_effort=_optional_str_field(data, "ari_effort", min_len=2, max_len=120),
            preferred_city=preferred_city,
            next_action=_optional_str_field(data, "next_action", min_len=2, max_len=500),
            source_provenance=_optional_str_field(data, "source_provenance", min_len=3, max_len=1000),
        )
        for field_name, value in vars(parsed).items():
            if isinstance(value, str) and privacy.contact_kind(value):
                raise ValidationError(f"{field_name} contains contact data that must remain external")
        return parsed


@dataclass(frozen=True)
class ThesisContactUpdate:
    episode_thesis: str
    route_type: str
    route_label: str
    source_provenance: str
    verified_at: datetime

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThesisContactUpdate":
        route_type = data.get("route_type")
        _require(
            isinstance(route_type, str) and bool(_ROUTE_TYPE_PATTERN.fullmatch(route_type)),
            f"route_type must match {_ROUTE_TYPE_PATTERN.pattern}",
        )
        parsed = cls(
            episode_thesis=_str_field(data, "episode_thesis", min_len=20, max_len=4000),
            route_type=route_type,
            route_label=_str_field(data, "route_label", min_len=3, max_len=160),
            source_provenance=_str_field(data, "source_provenance", min_len=10, max_len=2000),
            verified_at=_parse_aware_datetime(data.get("verified_at"), "verified_at"),
        )
        for field_name in ("episode_thesis", "route_label", "source_provenance"):
            if privacy.contact_kind(getattr(parsed, field_name)):
                raise ValidationError(f"{field_name} contains contact data that must remain external")
        return parsed


@dataclass(frozen=True)
class DecisionCreate:
    action: DecisionAction
    note: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionCreate":
        note = data.get("note")
        if note is not None:
            _require(isinstance(note, str), "note must be a string")
            _require(len(note) <= 4000, "note must be <= 4000 chars")
            note = note.strip() or None
        return cls(action=_enum_field(data, "action", DecisionAction), note=note)


@dataclass(frozen=True)
class DraftCreate:
    kind: DraftKind = DraftKind.INVITATION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftCreate":
        if "kind" not in data or data.get("kind") is None:
            return cls()
        return cls(kind=_enum_field(data, "kind", DraftKind))


@dataclass(frozen=True)
class StudioRoutingCreate:
    city: StudioCity
    studio_label: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StudioRoutingCreate":
        return cls(
            city=_enum_field(data, "city", StudioCity),
            studio_label=_opaque_reference(data.get("studio_reference", data.get("studio_label")), "studio_reference"),
        )


@dataclass(frozen=True)
class ResearchClaimInput:
    claim: str
    evidence_source: str
    verified: bool

    def as_json(self) -> dict[str, Any]:
        return {"claim": self.claim, "evidence_source": self.evidence_source, "verified": self.verified}


@dataclass(frozen=True)
class SegmentInput:
    title: str
    objective: str

    def as_json(self) -> dict[str, Any]:
        return {"title": self.title, "objective": self.objective}


@dataclass(frozen=True)
class BriefCreate:
    research_claims: list[ResearchClaimInput]
    segments: list[SegmentInput]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BriefCreate":
        raw_claims = data.get("research_claims")
        raw_segments = data.get("segments")
        _require(
            isinstance(raw_claims, list) and 1 <= len(raw_claims) <= 30, "research_claims must be a list of 1-30 items"
        )
        _require(
            isinstance(raw_segments, list) and 1 <= len(raw_segments) <= 20, "segments must be a list of 1-20 items"
        )
        claims = []
        for item in raw_claims:
            _require(isinstance(item, dict), "each research claim must be an object")
            claims.append(
                ResearchClaimInput(
                    claim=_str_field(item, "claim", min_len=5, max_len=2000),
                    evidence_source=_str_field(item, "evidence_source", min_len=5, max_len=1000),
                    verified=bool(item.get("verified")),
                )
            )
        segments = []
        for item in raw_segments:
            _require(isinstance(item, dict), "each segment must be an object")
            segments.append(
                SegmentInput(
                    title=_str_field(item, "title", min_len=2, max_len=120),
                    objective=_str_field(item, "objective", min_len=10, max_len=1000),
                )
            )
        return cls(research_claims=claims, segments=segments)


@dataclass(frozen=True)
class AssetDeclarationInput:
    kind: str
    custody_target: str

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "custody_target": self.custody_target}


@dataclass(frozen=True)
class AssetPackageCreate:
    assets: list[AssetDeclarationInput]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssetPackageCreate":
        raw = data.get("assets")
        _require(isinstance(raw, list) and 1 <= len(raw) <= 30, "assets must be a list of 1-30 items")
        assets = []
        for item in raw:
            _require(isinstance(item, dict), "each asset must be an object")
            kind = item.get("kind")
            _require(
                isinstance(kind, str) and bool(_ASSET_KIND_PATTERN.fullmatch(kind)),
                f"asset kind must match {_ASSET_KIND_PATTERN.pattern}",
            )
            assets.append(
                AssetDeclarationInput(
                    kind=kind,
                    custody_target=_str_field(item, "custody_target", min_len=3, max_len=240),
                )
            )
        return cls(assets=assets)


@dataclass(frozen=True)
class ReceiptCreate:
    receipt_type: ReceiptType
    external_reference: str
    occurred_at: datetime
    details: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReceiptCreate":
        receipt_type = _enum_field(data, "receipt_type", ReceiptType)
        raw_details = data.get("details", {})
        _require(isinstance(raw_details, dict), "details must be an object")
        expected_keys = _RECEIPT_DETAIL_KEYS[receipt_type]
        _require(
            set(raw_details) == expected_keys,
            f"{receipt_type.value} details must contain exactly {sorted(expected_keys)}",
        )
        details: dict[str, Any]
        if receipt_type == ReceiptType.OUTREACH_SENT:
            details = {}
        elif receipt_type == ReceiptType.REPLY_CLASSIFIED:
            classification = raw_details["classification"]
            _require(
                isinstance(classification, str) and classification in REPLY_CLASSIFICATIONS,
                "details.classification is not canonical",
            )
            details = {"classification": classification}
        elif receipt_type == ReceiptType.BOOKING_CONFIRMED:
            details = {
                "studio_ref": _opaque_reference(raw_details["studio_ref"], "details.studio_ref"),
                "producer_ref": _opaque_reference(raw_details["producer_ref"], "details.producer_ref"),
                "recording_time": _iso(_parse_aware_datetime(raw_details["recording_time"], "details.recording_time")),
            }
        elif receipt_type == ReceiptType.CONSENT_SIGNED:
            _require(
                raw_details["private_pilot"] is True,
                "details.private_pilot must be true",
            )
            _require(
                isinstance(raw_details["clip_scope"], str) and raw_details["clip_scope"] in {"none", "approved_clips"},
                "details.clip_scope must be none or approved_clips",
            )
            details = {
                "private_pilot": True,
                "clip_scope": raw_details["clip_scope"],
            }
        elif receipt_type == ReceiptType.RECORDING_READY:
            asset_package_id = raw_details["asset_package_id"]
            _require(
                isinstance(asset_package_id, str),
                "details.asset_package_id must be a UUID",
            )
            try:
                parsed_package_id = UUID(asset_package_id)
            except ValueError as exc:
                raise ValidationError("details.asset_package_id must be a UUID") from exc
            _require(
                str(parsed_package_id) == asset_package_id.lower(),
                "details.asset_package_id must be a canonical UUID",
            )
            details = {
                "preflight_ref": _opaque_reference(raw_details["preflight_ref"], "details.preflight_ref"),
                "asset_package_id": asset_package_id,
            }
        elif receipt_type == ReceiptType.RECORDING_COMPLETED:
            _require(
                isinstance(raw_details["session_kind"], str)
                and raw_details["session_kind"] in {"guest_pilot", "technical_rehearsal"},
                "details.session_kind must be guest_pilot or technical_rehearsal",
            )
            details = {"session_kind": raw_details["session_kind"]}
        else:
            details = {
                "master_ref": _opaque_reference(raw_details["master_ref"], "details.master_ref"),
                "checksum_ref": _opaque_reference(raw_details["checksum_ref"], "details.checksum_ref"),
            }
        return cls(
            receipt_type=receipt_type,
            external_reference=_opaque_reference(data.get("external_reference")),
            occurred_at=_parse_aware_datetime(data.get("occurred_at"), "occurred_at"),
            details=details,
        )


@dataclass(frozen=True)
class CommitmentCreate:
    summary: str
    owner_role: HumanRole
    due_at: datetime

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommitmentCreate":
        return cls(
            summary=_str_field(data, "summary", min_len=5, max_len=2000),
            owner_role=_enum_field(data, "owner_role", HumanRole),
            due_at=_parse_aware_datetime(data.get("due_at"), "due_at"),
        )


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _new_id(purpose: str) -> str:
    return generation.new_id(purpose)


def _now() -> datetime:
    return generation.now()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical_status(status: str) -> str:
    """Confirm ``status`` is a legal state on the engine's state machine.

    Reusing :func:`hospes.states.resolve_target` keeps the service in lockstep
    with the canonical lifecycle rather than inventing statuses. Raises a
    server-side DomainError if a status ever drifts off-machine.
    """
    try:
        return states.resolve_target(status)
    except states.HospesStateError as exc:  # pragma: no cover - guards drift
        raise DomainError(500, f"non-canonical status {status!r}: {exc}") from exc


def _advance_status(opportunity: dict[str, Any], *targets: str) -> str:
    """Advance through explicit canonical edges and return the resulting state."""
    state_holder = {"state": opportunity["status"]}
    try:
        for target in targets:
            if state_holder["state"] != states.resolve_target(target):
                states.advance(state_holder, target)
    except states.HospesStateError as exc:
        raise DomainError(409, str(exc)) from exc
    return str(state_holder["state"])


def _require_role(actor: HumanActor, allowed: set[HumanRole], action: str) -> None:
    if actor.role not in allowed:
        roles = ", ".join(sorted(role.value for role in allowed))
        raise DomainError(403, f"{action} requires one of these roles: {roles}")


def _required_detail(details: dict[str, Any], key: str) -> Any:
    value = details.get(key)
    if value is None:
        raise DomainError(422, f"receipt details require {key}")
    return value


def _required_reference_detail(details: dict[str, Any], key: str) -> str:
    try:
        return _opaque_reference(_required_detail(details, key), f"details.{key}")
    except ValidationError as exc:
        raise DomainError(exc.status_code, exc.detail) from exc


def _audit(
    conn, opportunity: dict[str, Any], actor: HumanActor, event_type: str, details: Optional[dict[str, Any]] = None
) -> None:
    store.insert(
        conn,
        "audit_events",
        {
            "id": _new_id("audit_event"),
            "tenant_id": opportunity["tenant_id"],
            "show_id": opportunity["show_id"],
            "opportunity_id": opportunity["id"],
            "event_type": event_type,
            "actor_id": actor.actor_id,
            "actor_role": actor.role.value,
            "details": details or {},
            "created_at": _iso(_now()),
        },
    )


def _notify_transition(
    conn,
    opportunity: dict[str, Any],
    actor: HumanActor,
    trigger: str,
    *,
    due_at: Any = None,
) -> None:
    """Report one lifecycle transition to the team-notification map.

    The notification and its queue assignment join the caller's transaction, so
    a rejected transition can never leave a bell item behind.
    """
    notifications.emit_transition(
        conn,
        trigger=trigger,
        tenant_id=opportunity["tenant_id"],
        show_id=opportunity["show_id"],
        entity_ref=f"opportunity://{opportunity['id']}",
        subject=opportunity["guest_name"],
        actor_id=actor.actor_id,
        due_at=due_at,
        commit=False,
    )


def _touch(conn, opportunity_id: str, changes: dict[str, Any]) -> None:
    changes = {**changes, "updated_at": _iso(_now())}
    store.update(conn, "appearance_opportunities", opportunity_id, changes)


def _attach_guest_memory(conn, row: dict[str, Any], tenant_id: str, *, limit: int = 20) -> dict[str, Any]:
    """Attach cross-season memory and the live directive to one opportunity row.

    Season, episode, disposition, and date are operational metadata, so the
    badge needs no custody key and no elevated role. Note plaintext never enters
    this projection; it is revealed only through the guest-history endpoints.
    """
    guest_id = guest_crm.guest_identity(row)
    entries = guest_crm.public_history(
        conn,
        tenant_id=tenant_id,
        show_id=str(row["show_id"]),
        guest_id=guest_id,
        limit=limit,
    )
    row["guest_id"] = guest_id
    row["guest_history"] = entries
    row["guest_history_badge"] = guest_crm.badge(entries)
    row["guest_directive"] = guest_crm.directive_state(
        conn, tenant_id=tenant_id, show_id=str(row["show_id"]), guest_id=guest_id
    )
    row["do_not_contact"] = bool(row["guest_directive"]["do_not_contact"])
    return row


def _opportunity_row(conn, opportunity_id: str) -> Optional[dict[str, Any]]:
    return store.fetch_one(conn, "SELECT * FROM appearance_opportunities WHERE id = ?", (opportunity_id,))


def _contact_route(conn, opportunity_id: str) -> Optional[dict[str, Any]]:
    return store.fetch_one(conn, "SELECT * FROM contact_routes WHERE opportunity_id = ?", (opportunity_id,))


def _receipts(conn, opportunity_id: str) -> list[dict[str, Any]]:
    return store.fetch_all(
        conn,
        "SELECT * FROM operational_receipts WHERE opportunity_id = ? ORDER BY occurred_at, created_at",
        (opportunity_id,),
    )


# ---------------------------------------------------------------------------
# Read operations.
# ---------------------------------------------------------------------------


def get_opportunity(conn, opportunity_id: str, actor: Optional[HumanActor] = None) -> dict[str, Any]:
    """Return the bare opportunity row, enforcing the tenant boundary."""
    opportunity = _opportunity_row(conn, opportunity_id)
    if opportunity is None:
        raise DomainError(404, "appearance opportunity not found")
    if actor is not None and opportunity["tenant_id"] != actor.tenant_id:
        raise DomainError(403, "the opportunity belongs to a different tenant boundary")
    return opportunity


def opportunity_detail(conn, opportunity_id: str, actor: Optional[HumanActor] = None) -> dict[str, Any]:
    """Return the opportunity plus all its child records (the API detail view)."""
    opportunity = get_opportunity(conn, opportunity_id, actor)
    detail = dict(opportunity)
    detail["contact_route"] = _contact_route(conn, opportunity_id)
    detail["contact_roster_refs"] = contact_roster.public_contact_roster(
        conn,
        tenant_id=opportunity["tenant_id"],
        show_id=opportunity["show_id"],
        opportunity_id=opportunity_id,
    )
    detail["decisions"] = store.fetch_all(
        conn, "SELECT * FROM decisions WHERE opportunity_id = ? ORDER BY created_at", (opportunity_id,)
    )
    detail["drafts"] = store.fetch_all(
        conn,
        "SELECT * FROM correspondence_drafts WHERE opportunity_id = ? ORDER BY created_at",
        (opportunity_id,),
    )
    detail["studio_routes"] = store.fetch_all(
        conn,
        "SELECT * FROM studio_routes WHERE opportunity_id = ? ORDER BY created_at",
        (opportunity_id,),
    )
    detail["briefs"] = store.fetch_all(
        conn,
        "SELECT * FROM episode_briefs WHERE opportunity_id = ? ORDER BY created_at",
        (opportunity_id,),
    )
    detail["asset_packages"] = store.fetch_all(
        conn,
        "SELECT * FROM asset_packages WHERE opportunity_id = ? ORDER BY created_at",
        (opportunity_id,),
    )
    _attach_guest_memory(conn, detail, opportunity["tenant_id"], limit=50)
    detail["receipts"] = _receipts(conn, opportunity_id)
    detail["commitments"] = store.fetch_all(
        conn, "SELECT * FROM commitments WHERE opportunity_id = ? ORDER BY due_at", (opportunity_id,)
    )
    detail["audit_events"] = store.fetch_all(
        conn,
        "SELECT * FROM audit_events WHERE opportunity_id = ? ORDER BY created_at",
        (opportunity_id,),
    )
    return detail


def list_opportunities(
    conn,
    tenant_id: str,
    *,
    state: Optional[str] = None,
    owner: Optional[str] = None,
    show_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    clauses = ["tenant_id = ?"]
    params: list[Any] = [tenant_id]
    if show_id is not None:
        normalized_show = show_id.strip()
        _require(bool(_ID_PATTERN.fullmatch(normalized_show)), "show_id is invalid")
        clauses.append("show_id = ?")
        params.append(normalized_show)
    if state is not None:
        if not states.is_valid_state(state):
            raise ValidationError("state must be a canonical appearance state")
        clauses.append("status = ?")
        params.append(state)
    if owner is not None:
        normalized_owner = owner.strip()
        _require(2 <= len(normalized_owner) <= 120, "owner must be 2-120 chars")
        clauses.append("relationship_owner = ?")
        params.append(normalized_owner)
    rows = store.fetch_all(
        conn,
        "SELECT * FROM appearance_opportunities WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC",
        params,
    )
    for row in rows:
        row["contact_roster_refs"] = contact_roster.public_contact_roster(
            conn,
            tenant_id=tenant_id,
            show_id=row["show_id"],
            opportunity_id=row["id"],
        )
        _attach_guest_memory(conn, row, tenant_id)
    return rows


def approval_queue(
    conn,
    tenant_id: str,
    *,
    owner: Optional[str] = None,
    show_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    clauses = [
        "o.tenant_id = ?",
        "o.disposition IS NULL",
        "o.status = 'EDITORIAL_REVIEW'",
    ]
    params: list[Any] = [tenant_id]
    if show_id is not None:
        normalized_show = show_id.strip()
        _require(bool(_ID_PATTERN.fullmatch(normalized_show)), "show_id is invalid")
        clauses.append("o.show_id = ?")
        params.append(normalized_show)
    if owner is not None:
        normalized_owner = owner.strip()
        _require(2 <= len(normalized_owner) <= 120, "owner must be 2-120 chars")
        clauses.append("o.relationship_owner = ?")
        params.append(normalized_owner)
    rows = store.fetch_all(
        conn,
        "SELECT o.id, o.show_id, o.guest_id, o.source_key, o.season, o.episode, "
        "o.guest_name, o.status, o.episode_thesis AS thesis, "
        "o.proposed_artifact, o.relationship_class, o.relationship_owner, "
        "o.social_cost_1_5 AS social_cost, o.ari_effort, "
        "o.preferred_city AS city, o.next_action, o.source_provenance, "
        "r.route_type, r.route_label, r.source_provenance AS route_provenance, "
        "r.verified_at AS route_verified_at, r.usable AS route_usable "
        "FROM appearance_opportunities o "
        "LEFT JOIN contact_routes r ON r.opportunity_id = o.id "
        "WHERE " + " AND ".join(clauses) + " "
        "ORDER BY COALESCE(o.social_cost_1_5, 99), o.created_at",
        params,
    )
    for row in rows:
        row["contact_roster_refs"] = contact_roster.public_contact_roster(
            conn,
            tenant_id=tenant_id,
            show_id=row["show_id"],
            opportunity_id=row["id"],
        )
        _attach_guest_memory(conn, row, tenant_id)
    return rows


# ---------------------------------------------------------------------------
# Write operations (each mirrors PR #2's services.py, human-gated).
# ---------------------------------------------------------------------------


def create_opportunity(conn, payload: OpportunityCreate, actor: HumanActor) -> dict[str, Any]:
    if payload.tenant_id != actor.tenant_id:
        raise DomainError(403, "the authenticated tenant must own the opportunity")
    now = _iso(_now())
    opportunity_id = _new_id("appearance_opportunity")
    opportunity = {
        "id": opportunity_id,
        "guest_id": opportunity_id,
        "tenant_id": payload.tenant_id,
        "network_id": payload.network_id,
        "show_id": payload.show_id,
        "guest_name": payload.guest_name,
        "why_guest": payload.why_guest,
        "why_now": payload.why_now,
        "proposed_artifact": payload.proposed_artifact,
        "relationship_class": payload.relationship_class.value,
        "relationship_owner": payload.relationship_owner,
        "social_cost_1_5": payload.social_cost_1_5,
        "ari_effort": payload.ari_effort,
        "preferred_city": payload.preferred_city.value if payload.preferred_city else None,
        "next_action": payload.next_action,
        "source_provenance": payload.source_provenance,
        "status": _canonical_status("DISCOVERED"),
        "disposition": None,
        "episode_thesis": None,
        "created_at": now,
        "updated_at": now,
    }
    store.insert(conn, "appearance_opportunities", opportunity)
    _audit(conn, opportunity, actor, "appearance.candidate_created")
    conn.commit()
    return opportunity_detail(conn, opportunity["id"], actor)


def attach_thesis_contact(conn, opportunity_id: str, payload: ThesisContactUpdate, actor: HumanActor) -> dict[str, Any]:
    opportunity = get_opportunity(conn, opportunity_id, actor)
    _require_role(
        actor,
        {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER, HumanRole.RELATIONSHIP_OWNER},
        "thesis/contact preparation",
    )
    if opportunity["disposition"] == "PROTECTED":
        raise DomainError(409, "a protected relationship's contact route cannot be changed")
    if payload.verified_at > _now():
        raise DomainError(422, "contact-route verification cannot be in the future")

    next_status = opportunity["status"]
    if next_status == "DISCOVERED":
        next_status = _advance_status(opportunity, "RESEARCHING", "QUALIFIED", "EDITORIAL_REVIEW")
    elif next_status not in {"EDITORIAL_REVIEW", "APPROVED", "CONTACT_ROUTE_IDENTIFIED"}:
        raise DomainError(409, "thesis/contact preparation is not valid in the current state")
    _touch(
        conn,
        opportunity_id,
        {"episode_thesis": payload.episode_thesis, "status": next_status},
    )

    existing = _contact_route(conn, opportunity_id)
    route_fields = {
        "route_type": payload.route_type,
        "route_label": payload.route_label,
        "source_provenance": payload.source_provenance,
        "verified_at": _iso(payload.verified_at),
        "usable": True,
    }
    if existing is None:
        store.insert(
            conn,
            "contact_routes",
            {
                "id": _new_id("contact_route"),
                "tenant_id": opportunity["tenant_id"],
                "opportunity_id": opportunity_id,
                "created_at": _iso(_now()),
                **route_fields,
            },
        )
    else:
        store.update(conn, "contact_routes", existing["id"], route_fields)

    opportunity = _opportunity_row(conn, opportunity_id)
    _audit(conn, opportunity, actor, "episode.thesis_locked")
    _audit(
        conn,
        opportunity,
        actor,
        "contact_route.verified",
        {"route_type": payload.route_type, "verified_at": _iso(payload.verified_at)},
    )
    # Pilot plans store only the opaque route id. Reproject if this opportunity
    # already belongs to an active run; the adapter cannot send correspondence.
    from . import pilot_service

    try:
        pilot_service.apply_opportunity_evidence(
            conn,
            opportunity_id,
            evidence_kind="contact_route.changed",
            evidence_reference=None,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            occurred_at=_now(),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return opportunity_detail(conn, opportunity_id, actor)


def record_decision(conn, opportunity_id: str, payload: DecisionCreate, actor: HumanActor) -> dict[str, Any]:
    opportunity = get_opportunity(conn, opportunity_id, actor)
    if opportunity["disposition"] == "PROTECTED" and payload.action != DecisionAction.NOTE:
        raise DomainError(409, "a protected relationship cannot be changed through this endpoint")
    if payload.action == DecisionAction.NOTE and not payload.note:
        raise DomainError(422, "a note action requires note text")
    if payload.action == DecisionAction.PROTECT and actor.role != HumanRole.RELATIONSHIP_OWNER:
        raise DomainError(403, "only a relationship owner may protect a relationship")
    if payload.action in {DecisionAction.APPROVE, DecisionAction.REJECT} and actor.role not in {
        HumanRole.HOST,
        HumanRole.EDITORIAL_OWNER,
        HumanRole.RELATIONSHIP_OWNER,
    }:
        raise DomainError(403, "this decision requires a host or explicit owner")
    # The protected relationship classes are owned by the engine (drafts.py).
    if (
        payload.action == DecisionAction.APPROVE
        and opportunity["relationship_class"] in drafts.PROTECTED_CLASSES
        and actor.role != HumanRole.RELATIONSHIP_OWNER
    ):
        raise DomainError(403, "C4/C5 approval requires the relationship owner")

    event_type = "correction.appended"
    changes: dict[str, Any] = {}
    if payload.action == DecisionAction.APPROVE:
        changes = {
            "disposition": "APPROVED",
            "status": _advance_status(opportunity, "APPROVED"),
        }
        event_type = "appearance.approved"
    elif payload.action == DecisionAction.REJECT:
        changes = {
            "disposition": "REJECTED",
            "status": _advance_status(opportunity, "DECLINED"),
        }
    elif payload.action == DecisionAction.PROTECT:
        # PROTECTED_RELATIONSHIP is a disposition, not a canonical lifecycle
        # state; hold the opportunity in DO_NOT_CONTACT and flag disposition.
        changes = {
            "disposition": "PROTECTED",
            "status": _advance_status(opportunity, "DO_NOT_CONTACT"),
        }
        event_type = "relationship.protected"

    store.insert(
        conn,
        "decisions",
        {
            "id": _new_id("decision"),
            "tenant_id": opportunity["tenant_id"],
            "opportunity_id": opportunity_id,
            "action": payload.action.value,
            "note": payload.note,
            "actor_id": actor.actor_id,
            "actor_role": actor.role.value,
            "created_at": _iso(_now()),
        },
    )
    if payload.action == DecisionAction.PROTECT:
        route = _contact_route(conn, opportunity_id)
        if route is not None:
            store.update(conn, "contact_routes", route["id"], {"usable": False})
    if changes:
        _touch(conn, opportunity_id, changes)

    opportunity = _opportunity_row(conn, opportunity_id)
    _audit(conn, opportunity, actor, event_type, {"decision": payload.action.value})
    _record_decision_memory(conn, opportunity, payload, actor)
    if payload.action == DecisionAction.APPROVE:
        _notify_transition(conn, opportunity, actor, "appearance.approved")
    if payload.action in {DecisionAction.REJECT, DecisionAction.PROTECT}:
        from . import pilot_service

        try:
            pilot_service.apply_opportunity_evidence(
                conn,
                opportunity_id,
                evidence_kind=(
                    "candidate.rejected" if payload.action == DecisionAction.REJECT else "contact_route.changed"
                ),
                evidence_reference=None,
                actor_id=actor.actor_id,
                actor_role=actor.role.value,
                occurred_at=_now(),
            )
        except Exception:
            conn.rollback()
            raise
    conn.commit()
    return opportunity_detail(conn, opportunity_id, actor)


def _record_decision_memory(conn, opportunity: dict[str, Any], payload: DecisionCreate, actor: HumanActor) -> None:
    """Append this season's outcome to the guest's cross-season memory.

    A decision only becomes memory when the opportunity declares which season
    and episode it belongs to; without that the entry could not be rendered as
    "Previously: S2E3", so no half-formed row is written.
    """
    disposition = DECISION_DISPOSITIONS.get(payload.action.value)
    if disposition is None or not opportunity.get("season") or not opportunity.get("episode"):
        return
    entry = guest_crm.GuestHistoryInput(
        guest_id=guest_crm.guest_identity(opportunity),
        opportunity_id=opportunity["id"],
        season=str(opportunity["season"]),
        episode=str(opportunity["episode"]),
        disposition=disposition,
        date=_now().astimezone(timezone.utc).date().isoformat(),
        notes=None,
        notes_ref=None,
    )
    try:
        guest_crm.record_history(
            conn,
            entry,
            tenant_id=opportunity["tenant_id"],
            show_id=opportunity["show_id"],
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            source="decision",
            now=_now(),
            commit=False,
        )
    except guest_crm.GuestHistoryError as exc:
        conn.rollback()
        raise DomainError(exc.status_code, exc.detail) from exc


def _require_not_do_not_contact(conn, opportunity: dict[str, Any], action: str) -> None:
    """Refuse any outbound act aimed at a guest under a do-not-contact directive."""
    if guest_crm.is_do_not_contact(
        conn,
        tenant_id=opportunity["tenant_id"],
        show_id=opportunity["show_id"],
        guest_id=guest_crm.guest_identity(opportunity),
    ):
        raise DomainError(403, f"{action} is refused: the guest is marked do not contact")


def _require_approved(opportunity: dict[str, Any]) -> None:
    if opportunity["disposition"] != "APPROVED":
        raise DomainError(409, "the opportunity requires explicit human approval")


def _render_draft_preview(
    conn, opportunity_id: str, payload: DraftCreate, actor: HumanActor
) -> tuple[dict[str, Any], str, str]:
    opportunity = get_opportunity(conn, opportunity_id, actor)
    _require_role(
        actor,
        {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER, HumanRole.RELATIONSHIP_OWNER},
        "draft generation",
    )
    if opportunity["relationship_class"] in drafts.PROTECTED_CLASSES:
        raise DomainError(403, "C4/C5 opportunities cannot be drafted in HOSPES")
    _require_not_do_not_contact(conn, opportunity, "outreach drafting")
    _require_approved(opportunity)
    if payload.kind == DraftKind.THANK_YOU:
        raise DomainError(422, "thank-you drafting is outside the Pilot outreach interface")
    if payload.kind == DraftKind.FOLLOW_UP:
        if opportunity["status"] != "FOLLOW_UP_DUE":
            raise DomainError(409, "follow-up draft preview requires FOLLOW_UP_DUE")
    elif opportunity["status"] not in OUTREACH_DRAFTABLE_STATES:
        raise DomainError(409, "initial draft preview is only valid in an approved pre-outreach state")
    route = _contact_route(conn, opportunity_id)
    if route is None or not route["usable"]:
        raise DomainError(409, "a usable, verified contact route is required")
    if not opportunity["episode_thesis"]:
        raise DomainError(409, "an episode thesis is required")

    thesis = opportunity["episode_thesis"]
    subject = (
        f"Follow-up: {thesis[:80]}"
        if payload.kind == DraftKind.FOLLOW_UP
        else f"Conversation invitation: {thesis[:80]}"
    )
    _template_key, configured_body = drafts.render_outreach(
        {
            "guest_name": opportunity["guest_name"],
            "episode_thesis": thesis,
            "why_guest": opportunity["why_guest"],
            "why_now": opportunity["why_now"],
            "proposed_artifact": opportunity["proposed_artifact"],
            "relationship_class": opportunity["relationship_class"],
        },
        show_id=opportunity["show_id"],
    )
    # Return the configured draft for immediate human review, but persist only
    # its template metadata. Correspondence bodies live in the external owner.
    if payload.kind == DraftKind.FOLLOW_UP:
        configured_body = f"A brief human-reviewed follow-up to the prior invitation.\n\n{configured_body}"
    body = f"{drafts.DRAFT_BANNER}\n\n{configured_body}"
    return opportunity, subject, body


def preview_draft(conn, opportunity_id: str, payload: DraftCreate, actor: HumanActor) -> dict[str, Any]:
    """Render an invitation for immediate review without persisting or advancing state."""
    _opportunity, subject, body = _render_draft_preview(conn, opportunity_id, payload, actor)
    return {
        "kind": payload.kind.value,
        "subject": subject,
        "body": body,
        "persisted": False,
        "persisted_body": False,
    }


def create_draft(conn, opportunity_id: str, payload: DraftCreate, actor: HumanActor) -> dict[str, Any]:
    """Persist safe human-review metadata and return the unpersisted draft body once."""
    opportunity, subject, body = _render_draft_preview(conn, opportunity_id, payload, actor)
    status = opportunity["status"]
    if payload.kind != DraftKind.FOLLOW_UP:
        if status == "APPROVED":
            status = _advance_status(opportunity, "CONTACT_ROUTE_IDENTIFIED", "OUTREACH_DRAFTED")
        elif status == "CONTACT_ROUTE_IDENTIFIED":
            status = _advance_status(opportunity, "OUTREACH_DRAFTED")
        elif status == "OUTREACH_APPROVED":
            status = _advance_status(opportunity, "OUTREACH_DRAFTED")
        elif status != "OUTREACH_DRAFTED":
            raise DomainError(409, "draft generation is not valid in the current state")

    persisted_draft = {
        "id": _new_id("correspondence_draft"),
        "tenant_id": opportunity["tenant_id"],
        "opportunity_id": opportunity_id,
        "kind": payload.kind.value,
        "subject": ("template:follow_up" if payload.kind == DraftKind.FOLLOW_UP else f"template:{payload.kind.value}"),
        "body": "[CORRESPONDENCE BODY NOT PERSISTED — EXTERNAL OWNER REQUIRED]",
        "status": "REVIEWED",
        "created_by": actor.actor_id,
        "created_at": _iso(_now()),
    }
    store.insert(conn, "correspondence_drafts", persisted_draft)
    _touch(conn, opportunity_id, {"status": status})
    opportunity = _opportunity_row(conn, opportunity_id)
    _audit(
        conn,
        opportunity,
        actor,
        "outreach.draft_ready",
        {"kind": payload.kind.value, "human_reviewed": True},
    )
    conn.commit()
    return {**persisted_draft, "subject": subject, "body": body, "persisted_body": False}


def route_to_studio(conn, opportunity_id: str, payload: StudioRoutingCreate, actor: HumanActor) -> dict[str, Any]:
    opportunity = get_opportunity(conn, opportunity_id, actor)
    _require_role(
        actor,
        {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
        "studio routing",
    )
    _require_approved(opportunity)
    route = {
        "id": _new_id("studio_route"),
        "tenant_id": opportunity["tenant_id"],
        "opportunity_id": opportunity_id,
        "city": payload.city.value,
        "studio_label": payload.studio_label,
        "routed_by": actor.actor_id,
        "created_at": _iso(_now()),
    }
    store.insert(conn, "studio_routes", route)
    if opportunity["status"] in {"INTERESTED", "NEGOTIATING"}:
        _touch(
            conn,
            opportunity_id,
            {"status": _advance_status(opportunity, "SCHEDULING")},
        )
    opportunity = _opportunity_row(conn, opportunity_id)
    _audit(conn, opportunity, actor, "booking.proposed", {"city": payload.city.value})
    conn.commit()
    return route


def create_brief(conn, opportunity_id: str, payload: BriefCreate, actor: HumanActor) -> dict[str, Any]:
    opportunity = get_opportunity(conn, opportunity_id, actor)
    _require_role(
        actor,
        {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
        "brief preparation",
    )
    _require_approved(opportunity)
    if not opportunity["episode_thesis"]:
        raise DomainError(409, "an episode thesis is required")
    if any(not claim.verified for claim in payload.research_claims):
        raise DomainError(422, "all research claims require evidence and verification")
    brief = {
        "id": _new_id("episode_brief"),
        "tenant_id": opportunity["tenant_id"],
        "opportunity_id": opportunity_id,
        "thesis": opportunity["episode_thesis"],
        "research_claims": [c.as_json() for c in payload.research_claims],
        "segments": [s.as_json() for s in payload.segments],
        "created_by": actor.actor_id,
        "created_at": _iso(_now()),
    }
    store.insert(conn, "episode_briefs", brief)
    if opportunity["status"] in {
        "BOOKED",
        "INTAKE_PENDING",
        "PREINTERVIEW_PENDING",
        "RELEASE_PENDING",
    } and _has_receipt(conn, opportunity_id, ReceiptType.CONSENT_SIGNED):
        _touch(
            conn,
            opportunity_id,
            {"status": _advance_status(opportunity, "PREP_IN_PROGRESS")},
        )
    opportunity = _opportunity_row(conn, opportunity_id)
    for claim in payload.research_claims:
        _audit(conn, opportunity, actor, "research.claim_verified", {"evidence_source": claim.evidence_source})
    for segment in payload.segments:
        _audit(conn, opportunity, actor, "segment.identified", {"title": segment.title})
    _notify_transition(conn, opportunity, actor, "brief.ready")
    conn.commit()
    return brief


def declare_assets(conn, opportunity_id: str, payload: AssetPackageCreate, actor: HumanActor) -> dict[str, Any]:
    opportunity = get_opportunity(conn, opportunity_id, actor)
    _require_role(
        actor,
        {HumanRole.PRODUCER, HumanRole.EDITORIAL_OWNER},
        "preflight asset declaration",
    )
    _require_approved(opportunity)
    briefs_present = store.fetch_one(
        conn, "SELECT id FROM episode_briefs WHERE opportunity_id = ? LIMIT 1", (opportunity_id,)
    )
    if briefs_present is None:
        raise DomainError(409, "an episode brief is required before declaring assets")
    declared_kinds = {asset.kind for asset in payload.assets}
    missing = REQUIRED_PREFLIGHT_ASSETS - declared_kinds
    if missing:
        raise DomainError(422, f"recording package is missing: {', '.join(sorted(missing))}")
    package = {
        "id": _new_id("asset_package"),
        "tenant_id": opportunity["tenant_id"],
        "opportunity_id": opportunity_id,
        "assets": [a.as_json() for a in payload.assets],
        "status": "DECLARED",
        "declared_by": actor.actor_id,
        "created_at": _iso(_now()),
    }
    store.insert(conn, "asset_packages", package)
    _audit(
        conn,
        opportunity,
        actor,
        "recording.preflight_assets_declared",
        {"declared_asset_count": len(payload.assets), "asset_package_id": package["id"]},
    )
    conn.commit()
    return package


def _receipt_by_identity(
    conn,
    opportunity_id: str,
    receipt_type: ReceiptType,
    external_reference: str,
) -> Optional[dict[str, Any]]:
    return store.fetch_one(
        conn,
        "SELECT * FROM operational_receipts WHERE opportunity_id = ? AND receipt_type = ? AND external_reference = ?",
        (opportunity_id, receipt_type.value, external_reference),
    )


def _has_receipt(conn, opportunity_id: str, receipt_type: ReceiptType) -> bool:
    return (
        store.fetch_one(
            conn,
            "SELECT id FROM operational_receipts WHERE opportunity_id = ? AND receipt_type = ? LIMIT 1",
            (opportunity_id, receipt_type.value),
        )
        is not None
    )


def record_receipt(conn, opportunity_id: str, payload: ReceiptCreate, actor: HumanActor) -> dict[str, Any]:
    """Record one external human/producer action and apply its canonical transition.

    The endpoint is an evidence intake, never an action executor. Duplicate
    owner references return the original row without another transition or
    audit event.
    """
    opportunity = get_opportunity(conn, opportunity_id, actor)
    _require_role(actor, _RECEIPT_ROLES[payload.receipt_type], payload.receipt_type.value)
    if payload.receipt_type is ReceiptType.OUTREACH_SENT:
        _require_not_do_not_contact(conn, opportunity, "an outreach receipt")
    if payload.occurred_at > _now():
        raise DomainError(422, "receipt occurred_at cannot be in the future")

    duplicate = _receipt_by_identity(conn, opportunity_id, payload.receipt_type, payload.external_reference)
    if duplicate is not None:
        return duplicate

    receipt_type = payload.receipt_type
    details = payload.details
    current_status = opportunity["status"]
    next_status = current_status

    if receipt_type == ReceiptType.OUTREACH_SENT:
        _require_approved(opportunity)
        if opportunity["relationship_class"] in drafts.PROTECTED_CLASSES and actor.role != HumanRole.RELATIONSHIP_OWNER:
            raise DomainError(403, "C4/C5 outreach receipts require the relationship owner")
        route = _contact_route(conn, opportunity_id)
        if route is None or not route["usable"] or not route["source_provenance"] or not route["verified_at"]:
            raise DomainError(409, "a usable route with provenance and verification is required")
        required_kinds = (
            (DraftKind.FOLLOW_UP.value,)
            if current_status == "FOLLOW_UP_DUE"
            else (DraftKind.INITIAL.value, DraftKind.INVITATION.value)
        )
        placeholders = ",".join("?" for _ in required_kinds)
        draft = store.fetch_one(
            conn,
            "SELECT id FROM correspondence_drafts "
            f"WHERE opportunity_id = ? AND status = 'REVIEWED' AND kind IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (opportunity_id, *required_kinds),
        )
        if draft is None:
            raise DomainError(409, "a matching human-reviewed draft must exist before a send receipt")
        next_status = (
            _advance_status(opportunity, ReceiptType.OUTREACH_SENT.value)
            if current_status == "FOLLOW_UP_DUE"
            else _advance_status(opportunity, "OUTREACH_APPROVED", ReceiptType.OUTREACH_SENT.value)
        )

    elif receipt_type == ReceiptType.REPLY_CLASSIFIED:
        if current_status not in {"OUTREACH_SENT", "FOLLOW_UP_DUE"}:
            raise DomainError(409, "reply classification requires recorded outreach")
        classification = _required_detail(details, "classification")
        if classification not in REPLY_CLASSIFICATIONS:
            raise DomainError(422, "reply classification is not canonical")
        if classification in {"POSITIVE_INTEREST", "PROPOSED_TIMES"}:
            studio_route = store.fetch_one(
                conn,
                "SELECT id FROM studio_routes WHERE opportunity_id = ? LIMIT 1",
                (opportunity_id,),
            )
            targets = ("INTERESTED", "SCHEDULING") if studio_route else ("INTERESTED",)
            next_status = _advance_status(opportunity, *targets)
        elif classification in {"SOFT_DECLINE", "HARD_DECLINE"}:
            next_status = _advance_status(opportunity, "DECLINED")
        elif classification == "UNSUBSCRIBE":
            next_status = _advance_status(opportunity, "DO_NOT_CONTACT")
        elif classification == "FOLLOW_UP_LATER":
            next_status = _advance_status(opportunity, "REVISIT_LATER")
        elif classification in {
            "NEEDS_MORE_INFORMATION",
            "CONTACT_PUBLICIST",
            "CONTACT_ASSISTANT",
            "FEE_REQUEST",
            "TRAVEL_REQUEST",
            "TOPIC_CONCERN",
            "AMBIGUOUS",
            "REQUIRES_HUMAN",
        }:
            next_status = _advance_status(opportunity, "NEEDS_HUMAN")

    elif receipt_type == ReceiptType.BOOKING_CONFIRMED:
        if current_status != "SCHEDULING":
            raise DomainError(409, "booking confirmation requires the SCHEDULING state")
        _required_reference_detail(details, "studio_ref")
        _required_reference_detail(details, "producer_ref")
        recording_time = _parse_aware_datetime(_required_detail(details, "recording_time"), "details.recording_time")
        if recording_time <= payload.occurred_at:
            raise DomainError(422, "recording_time must be after the booking receipt")
        routed = store.fetch_one(
            conn,
            "SELECT id FROM studio_routes WHERE opportunity_id = ? LIMIT 1",
            (opportunity_id,),
        )
        if routed is None:
            raise DomainError(409, "a studio route is required before booking confirmation")
        next_status = _advance_status(opportunity, ReceiptType.BOOKING_CONFIRMED.value)

    elif receipt_type == ReceiptType.CONSENT_SIGNED:
        if current_status != "BOOKED":
            raise DomainError(409, "signed consent requires the BOOKED state")
        if details.get("private_pilot") is not True:
            raise DomainError(422, "consent must explicitly cover the private pilot")
        if details.get("clip_scope") not in {"none", "approved_clips"}:
            raise DomainError(422, "consent clip_scope must be none or approved_clips")
        targets = ["RELEASE_PENDING"]
        brief = store.fetch_one(
            conn,
            "SELECT id FROM episode_briefs WHERE opportunity_id = ? LIMIT 1",
            (opportunity_id,),
        )
        if brief is not None:
            targets.append("PREP_IN_PROGRESS")
        next_status = _advance_status(opportunity, *targets)

    elif receipt_type == ReceiptType.RECORDING_READY:
        if current_status != "PREP_IN_PROGRESS":
            raise DomainError(409, "recording readiness requires PREP_IN_PROGRESS")
        route = _contact_route(conn, opportunity_id)
        if route is None or not route["usable"] or not route["source_provenance"] or not route["verified_at"]:
            raise DomainError(409, "recording readiness requires verified route provenance")
        if not _has_receipt(conn, opportunity_id, ReceiptType.BOOKING_CONFIRMED):
            raise DomainError(409, "recording readiness requires a booking receipt")
        if not _has_receipt(conn, opportunity_id, ReceiptType.CONSENT_SIGNED):
            raise DomainError(409, "recording readiness requires a consent receipt")
        brief = store.fetch_one(
            conn,
            "SELECT id FROM episode_briefs WHERE opportunity_id = ? LIMIT 1",
            (opportunity_id,),
        )
        if brief is None:
            raise DomainError(409, "recording readiness requires a verified brief and segment plan")
        _required_reference_detail(details, "preflight_ref")
        asset_package_id = _required_detail(details, "asset_package_id")
        package = store.fetch_one(
            conn,
            "SELECT * FROM asset_packages WHERE id = ? AND opportunity_id = ?",
            (asset_package_id, opportunity_id),
        )
        if package is None:
            raise DomainError(409, "recording readiness requires the declared asset package")
        asset_kinds = {item.get("kind") for item in package["assets"]}
        missing = REQUIRED_PREFLIGHT_ASSETS - asset_kinds
        if missing:
            raise DomainError(409, f"preflight asset package is missing: {', '.join(sorted(missing))}")
        next_status = _advance_status(opportunity, ReceiptType.RECORDING_READY.value)

    elif receipt_type == ReceiptType.RECORDING_COMPLETED:
        if current_status != "RECORDING_READY":
            raise DomainError(409, "recording completion requires RECORDING_READY")
        session_kind = _required_detail(details, "session_kind")
        if session_kind not in {"guest_pilot", "technical_rehearsal"}:
            raise DomainError(422, "session_kind must be guest_pilot or technical_rehearsal")
        if session_kind == "guest_pilot":
            next_status = _advance_status(opportunity, receipt_type.value)

    elif receipt_type == ReceiptType.MEDIA_INGESTED:
        if current_status != "RECORDED":
            raise DomainError(409, "media ingestion requires a completed guest recording")
        _required_reference_detail(details, "master_ref")
        _required_reference_detail(details, "checksum_ref")

    receipt = {
        "id": _new_id("operational_receipt"),
        "tenant_id": opportunity["tenant_id"],
        "opportunity_id": opportunity_id,
        "receipt_type": receipt_type.value,
        "external_reference": payload.external_reference,
        "occurred_at": _iso(payload.occurred_at),
        "actor_id": actor.actor_id,
        "actor_role": actor.role.value,
        "details": details,
        "created_at": _iso(_now()),
    }
    store.insert(conn, "operational_receipts", receipt)
    if next_status != current_status:
        _touch(conn, opportunity_id, {"status": next_status})
        opportunity = _opportunity_row(conn, opportunity_id)
    _audit(
        conn,
        opportunity,
        actor,
        receipt_type.value,
        {"receipt_id": receipt["id"], "external_reference": payload.external_reference},
    )
    if receipt_type == ReceiptType.BOOKING_CONFIRMED:
        _notify_transition(
            conn,
            opportunity,
            actor,
            "booking.confirmed",
            due_at=details.get("recording_time"),
        )
    elif receipt_type == ReceiptType.RECORDING_COMPLETED and details.get("session_kind") == "guest_pilot":
        _notify_transition(conn, opportunity, actor, "recording.completed")
    from . import pilot_service

    try:
        pilot_service.apply_opportunity_evidence(
            conn,
            opportunity_id,
            evidence_kind=receipt_type.value,
            evidence_reference=payload.external_reference,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            occurred_at=payload.occurred_at,
            classification=(details.get("classification") if receipt_type == ReceiptType.REPLY_CLASSIFIED else None),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return receipt


def create_commitment(conn, opportunity_id: str, payload: CommitmentCreate, actor: HumanActor) -> dict[str, Any]:
    opportunity = get_opportunity(conn, opportunity_id, actor)
    commitment = {
        "id": _new_id("commitment"),
        "tenant_id": opportunity["tenant_id"],
        "opportunity_id": opportunity_id,
        "summary": payload.summary,
        "owner_role": payload.owner_role.value,
        "due_at": _iso(payload.due_at),
        "status": "OPEN",
        "completed_at": None,
        "created_by": actor.actor_id,
        "created_at": _iso(_now()),
    }
    store.insert(conn, "commitments", commitment)
    _audit(
        conn,
        opportunity,
        actor,
        "commitment.created",
        {"owner_role": payload.owner_role.value, "due_at": _iso(payload.due_at)},
    )
    conn.commit()
    return commitment


def list_followups(
    conn,
    tenant_id: str,
    due_before: datetime,
    *,
    show_id: str | None = None,
) -> list[dict[str, Any]]:
    show_clause = " AND o.show_id = ?" if show_id is not None else ""
    params: tuple[Any, ...] = (tenant_id, _iso(due_before))
    if show_id is not None:
        normalized_show = show_id.strip()
        _require(bool(_ID_PATTERN.fullmatch(normalized_show)), "show_id is invalid")
        params += (normalized_show,)
    return store.fetch_all(
        conn,
        "SELECT c.* FROM commitments c "
        "JOIN appearance_opportunities o ON o.id = c.opportunity_id "
        "WHERE c.tenant_id = ? AND c.status = 'OPEN' AND c.due_at <= ?" + show_clause + " ORDER BY c.due_at",
        params,
    )


def complete_commitment(conn, commitment_id: str, actor: HumanActor) -> dict[str, Any]:
    commitment = store.fetch_one(conn, "SELECT * FROM commitments WHERE id = ?", (commitment_id,))
    if commitment is None:
        raise DomainError(404, "commitment not found")
    opportunity = get_opportunity(conn, commitment["opportunity_id"], actor)
    store.update(conn, "commitments", commitment_id, {"status": "COMPLETED", "completed_at": _iso(_now())})
    _audit(conn, opportunity, actor, "commitment.completed")
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM commitments WHERE id = ?", (commitment_id,))


# ---------------------------------------------------------------------------
# Guest CRM — cross-season memory and the do-not-contact directive.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoNotContactUpdate:
    """The relationship owner's standing instruction for one guest identity."""

    do_not_contact: bool
    reason_ref: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DoNotContactUpdate":
        _require(isinstance(data, dict), "do-not-contact payload must be an object")
        unknown = set(data) - {"do_not_contact", "reason_ref"}
        _require(not unknown, "do-not-contact payload contains unsupported fields")
        value = data.get("do_not_contact")
        _require(isinstance(value, bool), "do_not_contact must be a boolean")
        reference = data.get("reason_ref")
        if reference is not None:
            reference = _opaque_reference(reference, "reason_ref")
        return cls(do_not_contact=bool(value), reason_ref=reference)


def set_guest_do_not_contact(
    conn, opportunity_id: str, payload: DoNotContactUpdate, actor: HumanActor
) -> dict[str, Any]:
    """Set or lift the guest's do-not-contact directive from an approval card."""
    opportunity = get_opportunity(conn, opportunity_id, actor)
    guest_crm.set_directive(
        conn,
        tenant_id=opportunity["tenant_id"],
        show_id=opportunity["show_id"],
        guest_id=guest_crm.guest_identity(opportunity),
        do_not_contact=payload.do_not_contact,
        actor_id=actor.actor_id,
        actor_role=actor.role.value,
        reason_ref=payload.reason_ref,
        opportunity_id=opportunity_id,
    )
    return opportunity_detail(conn, opportunity_id, actor)


def opportunity_guest_history(
    conn,
    opportunity_id: str,
    actor: HumanActor,
    *,
    season: Optional[str] = None,
    limit: int = 100,
    field_vault: Any = None,
) -> list[dict[str, Any]]:
    """Return every cross-season interaction recorded for this guest identity."""
    opportunity = get_opportunity(conn, opportunity_id, actor)
    return guest_crm.list_history(
        conn,
        tenant_id=opportunity["tenant_id"],
        show_id=opportunity["show_id"],
        actor_role=actor.role.value,
        guest_id=guest_crm.guest_identity(opportunity),
        season=season,
        limit=limit,
        field_vault=field_vault,
    )


def show_guest_history(
    conn,
    show_id: str,
    actor: HumanActor,
    *,
    guest_id: Optional[str] = None,
    opportunity_id: Optional[str] = None,
    season: Optional[str] = None,
    limit: int = 100,
    field_vault: Any = None,
) -> list[dict[str, Any]]:
    """Return the show-wide guest register timeline, newest interaction first."""
    return guest_crm.list_history(
        conn,
        tenant_id=actor.tenant_id,
        show_id=show_id,
        actor_role=actor.role.value,
        guest_id=guest_id,
        opportunity_id=opportunity_id,
        season=season,
        limit=limit,
        field_vault=field_vault,
    )


def record_guest_history(
    conn,
    show_id: str,
    payload: dict[str, Any],
    actor: HumanActor,
    *,
    field_vault: Any = None,
) -> dict[str, Any]:
    """Record one prior-season interaction that HOSPES itself never observed."""
    parsed = guest_crm.GuestHistoryInput.from_mapping(payload)
    return guest_crm.record_history(
        conn,
        parsed,
        tenant_id=actor.tenant_id,
        show_id=show_id,
        actor_id=actor.actor_id,
        actor_role=actor.role.value,
        field_vault=field_vault,
    )
