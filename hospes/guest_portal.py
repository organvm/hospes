"""Optional isolated guest portal boundary.

The portal is disabled by default.  Its token is short-lived, one-time, and
stored only as a digest; submitted private fields are represented here by
opaque custody references rather than copied into the public artifact store.

The guest never gets an account.  A link carries a signed token in the URL
*fragment*, which a browser withholds from the Referer header and a server
never sees in a request line or an access log.  The client reads that fragment
once, exchanges it for a short server-side session, and erases it from the
address bar; the session — not the link — authorizes the submission, and it is
revoked the moment the intake lands.  A link can be exchanged exactly once,
because ``portal_sessions`` holds one row per token.

Nothing here transmits the link.  ``create_portal_token`` returns a URL for a
human to send through a channel they already have; the portal has no outbound
capability, by construction.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any, Iterable, Mapping, Sequence

from . import branding, encryption, generation, platform, store
from .paths import DASHBOARD_DIR

try:  # keep the core CLI importable without the optional API extra
    from fastapi import Request as _FastAPIRequest
except ImportError:  # pragma: no cover - exercised only without the optional extra
    _FastAPIRequest = Any  # type: ignore[misc,assignment]

#: A link is handed to a person, so it is measured in days; a session is held
#: by an open tab, so it is measured in minutes.
DEFAULT_LINK_TTL_DAYS = 7
MAX_LINK_TTL_DAYS = 7
SESSION_TTL_MINUTES = 30
PREFERRED_DATE_COUNT = 3
MAX_OFFERED_DATES = 24

#: The click-wrap.  Its identifiers are ``consent_type`` values from
#: ``spec/consent.schema.json``; its text is what the guest actually reads, and
#: the pair is checksummed into the ``consent.signed`` receipt so the exact
#: wording that was agreed to stays provable after the copy is edited.
CONSENT_VERSION = "1.0"
CONSENT_CLAUSES: tuple[tuple[str, str], ...] = (
    ("recording_release", "This conversation may be recorded and edited into an episode."),
    ("likeness_use", "My name, voice, and likeness may appear in the episode and in material promoting it."),
    ("transcript_publication", "A transcript of the recorded conversation may be published."),
    ("clip_use", "Short clips of the recording may be used to promote the episode."),
    ("care_profile_storage", "The care details I give here may be stored and used to run the recording day."),
)

#: The intake vocabulary is ``spec/care_profile.schema.json``, so what a guest
#: types lands in the field the production already reads.  Absence means "not
#: provided", never "not applicable" — so every field is optional.
CARE_PROFILE_TEXT_FIELDS = ("name_pronunciation", "pronouns", "preferred_beverage", "notes")
CARE_PROFILE_LIST_FIELDS = ("dietary_restrictions", "allergies", "accessibility_needs", "topics_off_limits")

#: A portal link is only offered once the recording is confirmed.
PORTAL_ELIGIBLE_STATES = frozenset({"BOOKED"})

PORTAL_SCRIPT_PATH = "/guest/app.js"
PORTAL_LAYOUT_STYLESHEET_PATH = "/guest/styles.css"
PORTAL_ROOT = "/guest/"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_TEXT = 500
_MAX_LIST_ENTRIES = 20
_MAX_LIST_ENTRY = 200

_RECEIPT_EVENTS = ("guest_intake.completed", "consent.signed", "availability.provided")


class PortalError(ValueError):
    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _sign(payload: str, secret: str) -> str:  # allow-secret: runtime signing material
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _token_payload(tenant_id: str, show_id: str, guest_id: str, expires: int, nonce: str) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                {"tenant_id": tenant_id, "show_id": show_id, "guest_id": guest_id, "exp": expires, "nonce": nonce},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )


def _decode(
    token: str,
    secret: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:  # allow-secret: runtime signing material
    try:
        encoded, signature = token.split(".", 1)
        if not hmac.compare_digest(_sign(encoded, secret), signature):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError, OverflowError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PortalError("portal token is invalid", 401) from exc
    if (
        not isinstance(payload, dict)
        or not all(
            isinstance(payload.get(key), str) and payload[key] for key in ("tenant_id", "show_id", "guest_id", "nonce")
        )
        or not isinstance(payload.get("exp"), int)
        or payload["exp"] <= int((now or generation.now()).timestamp())
    ):
        raise PortalError("portal token is expired", 401)
    return payload


def _timestamp(value: datetime | None = None) -> str:
    return (value or generation.now()).astimezone(timezone.utc).isoformat()


def _iso_date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _ISO_DATE.fullmatch(text):
        raise PortalError(f"{field} must be a YYYY-MM-DD date")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise PortalError(f"{field} must be a real calendar date") from exc
    return text


def _offered_dates(value: Any) -> list[str]:
    """Validate the operator's published availability (an empty list is legal).

    An operator who has not published dates yet still gets a working link; the
    guest is then asked to propose three instead of choosing from a slate.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise PortalError("offered_dates must be a list of YYYY-MM-DD dates")
    dates = [_iso_date(entry, "offered_dates") for entry in value]
    if len(set(dates)) != len(dates):
        raise PortalError("offered_dates must not repeat a date")
    if len(dates) > MAX_OFFERED_DATES:
        raise PortalError(f"offered_dates must not exceed {MAX_OFFERED_DATES} dates")
    return sorted(dates)


def _preferred_dates(value: Any, offered: Sequence[str]) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise PortalError("preferred_dates must be a list of YYYY-MM-DD dates")
    dates = [_iso_date(entry, "preferred_dates") for entry in value]
    if len(dates) != PREFERRED_DATE_COUNT:
        raise PortalError(f"preferred_dates must contain exactly {PREFERRED_DATE_COUNT} dates")
    if len(set(dates)) != len(dates):
        raise PortalError("preferred_dates must be three different dates")
    if offered and not set(dates) <= set(offered):
        raise PortalError("preferred_dates must be chosen from the dates this production offered")
    return dates


def _clean_text(value: Any, field: str) -> str:
    text = _CONTROL.sub("", str(value or "")).strip()
    if len(text) > _MAX_TEXT:
        raise PortalError(f"{field} must be {_MAX_TEXT} characters or fewer")
    return text


def _care_profile(value: Any) -> dict[str, Any]:
    """Accept only the declared care-profile vocabulary, all of it optional."""
    if not isinstance(value, Mapping):
        raise PortalError("care_profile must be an object")
    unknown = set(value) - set(CARE_PROFILE_TEXT_FIELDS) - set(CARE_PROFILE_LIST_FIELDS)
    if unknown:
        raise PortalError(f"care_profile does not accept {sorted(unknown)[0]!r}")
    profile: dict[str, Any] = {}
    for field in CARE_PROFILE_TEXT_FIELDS:
        text = _clean_text(value.get(field), field)
        if text:
            profile[field] = text
    for field in CARE_PROFILE_LIST_FIELDS:
        raw = value.get(field)
        if raw is None:
            continue
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
            raise PortalError(f"{field} must be a list of short entries")
        entries = [_CONTROL.sub("", str(entry or "")).strip() for entry in raw]
        entries = [entry for entry in entries if entry]
        if len(entries) > _MAX_LIST_ENTRIES:
            raise PortalError(f"{field} must not exceed {_MAX_LIST_ENTRIES} entries")
        if any(len(entry) > _MAX_LIST_ENTRY for entry in entries):
            raise PortalError(f"{field} entries must be {_MAX_LIST_ENTRY} characters or fewer")
        if entries:
            profile[field] = entries
    return profile


def consent_clauses() -> list[dict[str, str]]:
    """Return the click-wrap a guest is shown, in the order they read it."""
    return [{"consent_type": key, "text": text} for key, text in CONSENT_CLAUSES]


def consent_checksum(version: str = CONSENT_VERSION) -> str:
    """Checksum the exact wording, so an edited clause is a different consent."""
    body = json.dumps({"version": version, "clauses": consent_clauses()}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _consent(value: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Build the durable consent record from an explicit, affirmative tick.

    ``promotion_required`` is pinned false rather than read from the payload:
    a guest is never required to promote the show, and ``spec/consent.schema.json``
    makes that a validation invariant rather than a setting.
    """
    if not isinstance(value, Mapping):
        raise PortalError("consent must be an object")
    if value.get("accepted") is not True:
        raise PortalError("consent must be accepted before an intake can be recorded")
    version = str(value.get("version") or CONSENT_VERSION)
    if version != CONSENT_VERSION:
        raise PortalError("consent version is stale; reload the portal and read the current terms")
    return {
        "version": version,
        "checksum": consent_checksum(version),
        "clauses": consent_clauses(),
        "granted_consent_types": [key for key, _ in CONSENT_CLAUSES],
        "status": "granted",
        "granted_at": _timestamp(now),
        "promotion_required": False,
    }


def create_portal_token(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    secret: str,  # allow-secret: runtime signing material, not a value
    base_url: str,
    ttl_days: int = 1,
    enabled: bool = False,
    brand: dict[str, Any] | None = None,
    offered_dates: Any = None,
    created_by: str | None = None,
    created_by_role: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:  # allow-secret: runtime signing material
    if not enabled:
        raise PortalError("guest portal is disabled by show configuration", 403)
    if not secret:
        raise PortalError("guest portal signing secret is not configured", 503)
    if ttl_days < 1 or ttl_days > MAX_LINK_TTL_DAYS:
        raise PortalError(f"portal token ttl must be between 1 and {MAX_LINK_TTL_DAYS} days")
    dates = _offered_dates(offered_dates)
    current = int((now or generation.now()).timestamp())
    expires = current + ttl_days * 86400
    payload = _token_payload(
        tenant_id, show_id, platform._guest(guest_id), expires, secrets.token_urlsafe(18)
    )  # allow-secret: ephemeral nonce
    token = f"{payload}.{_sign(payload, secret)}"  # allow-secret: runtime signing material
    created_at = (now or generation.now()).astimezone(timezone.utc).isoformat()
    row = {
        "id": generation.new_id("portal_token"),
        "tenant_id": tenant_id,
        "show_id": show_id,
        "guest_id": platform._guest(guest_id),
        "token_digest": hashlib.sha256(token.encode()).hexdigest(),
        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
        "used_at": None,
        "created_at": created_at,
        "offered_dates": dates,
        "created_by": created_by,
        "created_by_role": created_by_role,
    }
    store.insert(conn, "portal_tokens", row)
    conn.commit()
    try:
        public_base = branding.portal_base_url(brand, base_url)
    except branding.BrandError as exc:
        raise PortalError(str(exc), 503) from exc
    return {
        "id": row["id"],
        "url": f"{public_base.rstrip('/')}/guest/#token={token}",  # allow-secret: runtime signing material, not a value
        "expires_at": row["expires_at"],
        "public_base_url": public_base,
        "offered_dates": dates,
    }  # allow-secret: token is one-time runtime output


def _token_for(
    conn: Any,
    token: str,
    secret: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:  # allow-secret: runtime token input
    payload = _decode(token, secret, now=now)
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = store.fetch_one(
        conn,
        "SELECT * FROM portal_tokens WHERE token_digest = ? AND tenant_id = ? AND show_id = ? AND guest_id = ?",
        (digest, payload["tenant_id"], payload["show_id"], payload["guest_id"]),
    )
    if not row or row.get("used_at"):
        raise PortalError("portal token is invalid or already used", 401)
    return row


def open_session(
    conn: Any, *, token: str, secret: str, ttl_minutes: int = SESSION_TTL_MINUTES, now: datetime | None = None  # allow-secret: runtime signing material, not a value
) -> dict[str, Any]:  # allow-secret: runtime token input
    """Exchange the one-time fragment token for a short, digest-only session.

    ``portal_sessions`` holds ``UNIQUE(portal_token_id)``, so the exchange is
    the moment the link is spent: a replayed fragment — from a shared screenshot
    or a synced browser history — finds the row already taken and is refused.
    """
    if ttl_minutes < 1 or ttl_minutes > SESSION_TTL_MINUTES:
        raise PortalError(f"portal session ttl must be between 1 and {SESSION_TTL_MINUTES} minutes")
    moment = now or generation.now()
    row = _token_for(conn, token, secret, now=moment)
    if store.fetch_one(conn, "SELECT id FROM portal_sessions WHERE portal_token_id = ?", (row["id"],)):
        raise PortalError("this one-time link has already been opened", 401)
    session = secrets.token_urlsafe(32)  # allow-secret: ephemeral session material
    expires_at = _timestamp(moment + timedelta(minutes=ttl_minutes))
    store.insert(
        conn,
        "portal_sessions",
        {
            "id": generation.new_id("portal_session"),
            "tenant_id": row["tenant_id"],
            "show_id": row["show_id"],
            "portal_token_id": row["id"],
            "session_digest": hashlib.sha256(session.encode()).hexdigest(),
            "expires_at": expires_at,
            "revoked_at": None,
            "created_at": _timestamp(moment),
        },
    )
    conn.commit()
    return {
        "session": session,
        "expires_at": expires_at,
        "guest_id": row["guest_id"],
        "offered_dates": list(row.get("offered_dates") or []),
        "consent_version": CONSENT_VERSION,
        "consent_clauses": consent_clauses(),
        "preferred_date_count": PREFERRED_DATE_COUNT,
    }  # allow-secret: session is one-time runtime output


def _session_for(
    conn: Any, session: str, *, now: datetime | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:  # allow-secret: runtime session input
    digest = hashlib.sha256(str(session or "").encode()).hexdigest()
    row = store.fetch_one(conn, "SELECT * FROM portal_sessions WHERE session_digest = ?", (digest,))
    if not row or row.get("revoked_at"):
        raise PortalError("portal session is invalid or already closed", 401)
    if str(row["expires_at"]) <= _timestamp(now):
        raise PortalError("portal session has expired; reopen your link", 401)
    token_row = store.fetch_one(
        conn,
        "SELECT * FROM portal_tokens WHERE id = ? AND tenant_id = ? AND show_id = ?",
        (row["portal_token_id"], row["tenant_id"], row["show_id"]),
    )
    if not token_row or token_row.get("used_at"):
        raise PortalError("portal session is invalid or already closed", 401)
    return row, token_row


def _receipts(
    conn: Any, intake: Mapping[str, Any], details: Mapping[str, Mapping[str, Any]], timestamp: str
) -> list[dict[str, Any]]:
    """Append the three attributable receipts one completed intake produces.

    They live in ``portal_receipts`` rather than ``operational_receipts``
    because the portal is scoped ``(tenant, show, guest)`` and is reached
    without an operator identity — the isolated boundary this module exists to
    hold.
    """
    written: list[dict[str, Any]] = []
    for event_type in _RECEIPT_EVENTS:
        row = {
            "id": generation.new_id("portal_receipt"),
            "tenant_id": intake["tenant_id"],
            "show_id": intake["show_id"],
            "guest_id": intake["guest_id"],
            "portal_intake_id": intake["id"],
            "event_type": event_type,
            "subject_ref": f"portal-intake://{intake['id']}",
            "details": dict(details.get(event_type, {})),
            "created_at": timestamp,
        }
        store.insert(conn, "portal_receipts", row)
        written.append(row)
    return written


def _record_intake(
    conn: Any,
    token_row: Mapping[str, Any],
    *,
    consent_ref: str,
    intake_ref: str,
    availability_ref: str,
    details: Mapping[str, Mapping[str, Any]],
    intake_id: str,
    preferred_dates: Sequence[str] = (),
    session_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _timestamp(now)
    intake = {
        "id": intake_id,
        "tenant_id": token_row["tenant_id"],
        "show_id": token_row["show_id"],
        "guest_id": token_row["guest_id"],
        "portal_token_id": token_row["id"],
        "consent_ref": consent_ref,
        "intake_ref": intake_ref,
        "availability_ref": availability_ref,
        "completed_at": timestamp,
    }
    store.insert(conn, "portal_intakes", intake)
    for rank, preferred in enumerate(preferred_dates, start=1):
        store.insert(
            conn,
            "portal_date_choices",
            {
                "id": generation.new_id("portal_date_choice"),
                "tenant_id": intake["tenant_id"],
                "show_id": intake["show_id"],
                "guest_id": intake["guest_id"],
                "portal_intake_id": intake_id,
                "rank": rank,
                "preferred_date": preferred,
                "created_at": timestamp,
            },
        )
    receipts = _receipts(conn, intake, details, timestamp)
    store.update(conn, "portal_tokens", token_row["id"], {"used_at": timestamp})
    if session_id is not None:
        store.update(conn, "portal_sessions", session_id, {"revoked_at": timestamp})
    conn.commit()
    return {**intake, "receipts": [row["event_type"] for row in receipts]}


def complete_intake(
    conn: Any,
    *,
    token: str,  # allow-secret: runtime signing material, not a value
    secret: str,  # allow-secret: runtime signing material, not a value
    consent_ref: str,
    intake_ref: str,
    availability_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:  # allow-secret: runtime token input
    """Record an intake whose private values already live in an external custody.

    This is the reference-only profile: the caller has already placed the
    consent, intake, and availability material somewhere it owns and passes
    opaque handles.  :func:`submit_intake` is the portal's own path, where the
    guest types the values and this module seals them.
    """
    row = _token_for(conn, token, secret, now=now)
    return _record_intake(
        conn,
        row,
        consent_ref=platform._ref(consent_ref, "consent_ref"),
        intake_ref=platform._ref(intake_ref, "intake_ref"),
        availability_ref=platform._ref(availability_ref, "availability_ref"),
        details={
            "guest_intake.completed": {"custody": "external"},
            "consent.signed": {"custody": "external"},
            "availability.provided": {"custody": "external"},
        },
        intake_id=generation.new_id("portal_intake"),
        now=now,
    )


def submit_intake(
    conn: Any,
    vault: encryption.FieldVault | None,
    *,
    session: str,
    care_profile: Any,
    consent: Any,
    preferred_dates: Any,
    now: datetime | None = None,
) -> dict[str, Any]:  # allow-secret: runtime session input
    """Seal what the guest typed, record the three dates, and emit the receipts.

    The care profile and the consent record are sealed into the tenant field
    vault and reach ``portal_intakes`` only as ``private-field://`` handles.
    The three dates are scheduling facts the production must act on rather than
    private content, so they stay legible in ``portal_date_choices`` and the
    operator can read them without unsealing anything.
    """
    if vault is None:
        raise PortalError("guest intake custody is not configured", 503)
    session_row, token_row = _session_for(conn, session, now=now)
    profile = _care_profile(care_profile)
    record = _consent(consent, now=now)
    dates = _preferred_dates(preferred_dates, list(token_row.get("offered_dates") or []))
    intake_id = generation.new_id("portal_intake")

    def scope(category: str, field_name: str) -> encryption.PrivateFieldScope:
        return encryption.PrivateFieldScope(
            tenant_id=str(token_row["tenant_id"]),
            show_id=str(token_row["show_id"]),
            category=category,
            owner_table="portal_intakes",
            owner_record_id=intake_id,
            field_name=field_name,
        )

    try:
        intake_ref = vault.put_json(conn, scope("correspondence", "intake"), profile, commit=False)
        consent_ref = vault.put_json(conn, scope("consent", "consent"), record, commit=False)
    except encryption.EncryptionError as exc:
        conn.rollback()
        raise PortalError("guest intake custody is unavailable", 503) from exc
    return _record_intake(
        conn,
        token_row,
        consent_ref=consent_ref,
        intake_ref=intake_ref,
        availability_ref=f"portal-availability://{intake_id}",
        details={
            "guest_intake.completed": {"fields": sorted(profile), "custody": "field_vault"},
            "consent.signed": {
                "version": record["version"],
                "checksum": record["checksum"],
                "consent_types": record["granted_consent_types"],
                "promotion_required": False,
            },
            "availability.provided": {
                "preferred_dates": list(dates),
                "from_offered_slate": bool(token_row.get("offered_dates")),
            },
        },
        intake_id=intake_id,
        preferred_dates=dates,
        session_id=str(session_row["id"]),
        now=now,
    )


def portal_status(
    conn: Any, *, tenant_id: str, show_id: str, guest_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """Report what the operator may see about one guest's portal link.

    Deliberately no private values: the operator learns that a link exists,
    whether it was opened, whether the intake landed, and which dates the guest
    chose — never what the guest wrote, which stays sealed.
    """
    tenant, show = platform._scope(tenant_id, show_id)
    guest = platform._guest(guest_id)
    row = store.fetch_one(
        conn,
        "SELECT * FROM portal_tokens WHERE tenant_id = ? AND show_id = ? AND guest_id = ? "
        "ORDER BY created_at DESC, id DESC",
        (tenant, show, guest),
    )
    status: dict[str, Any] = {
        "tenant_id": tenant,
        "show_id": show,
        "guest_id": guest,
        "state": "not_sent",
        "link_expires_at": None,
        "completed_at": None,
        "preferred_dates": [],
        "receipts": [],
        "offered_dates": [],
    }
    if not row:
        return status
    status.update(
        {"state": "sent", "link_expires_at": row["expires_at"], "offered_dates": list(row.get("offered_dates") or [])}
    )
    if store.fetch_one(conn, "SELECT id FROM portal_sessions WHERE portal_token_id = ?", (row["id"],)):
        status["state"] = "opened"
    intake = store.fetch_one(conn, "SELECT * FROM portal_intakes WHERE portal_token_id = ?", (row["id"],))
    if intake:
        status.update(
            {
                "state": "completed",
                "completed_at": intake["completed_at"],
                "preferred_dates": [
                    str(choice["preferred_date"])
                    for choice in store.fetch_all(
                        conn,
                        "SELECT preferred_date FROM portal_date_choices WHERE portal_intake_id = ? ORDER BY rank",
                        (intake["id"],),
                    )
                ],
                "receipts": [
                    str(receipt["event_type"])
                    for receipt in store.fetch_all(
                        conn,
                        "SELECT event_type FROM portal_receipts WHERE portal_intake_id = ? ORDER BY event_type",
                        (intake["id"],),
                    )
                ],
            }
        )
    elif str(row["expires_at"]) <= _timestamp(now):
        status["state"] = "expired"
    return status


def _opportunity_guest(opportunity: Mapping[str, Any]) -> str:
    """Resolve the opaque guest id a candidate row addresses.

    ``appearance_opportunities`` carries no ``guest_id`` column; ``source_key``
    is what the estate already joins on (``platform.suggest_guests`` matches it
    against ``guest_history.guest_id``), so it is the guest handle here too.
    """
    key = str(opportunity.get("source_key") or "").strip().lower()
    if not key:
        raise PortalError("this candidate has no source key to address a portal link to")
    try:
        return platform._guest(key)
    except platform.PlatformError as exc:
        raise PortalError("this candidate's source key is not a usable opaque guest id") from exc


def link_for_opportunity(
    conn: Any,
    opportunity: Mapping[str, Any],
    *,
    secret: str,  # allow-secret: runtime signing material, not a value
    base_url: str,
    enabled: bool = False,
    brand: dict[str, Any] | None = None,
    offered_dates: Any = None,
    ttl_days: int = DEFAULT_LINK_TTL_DAYS,
    created_by: str | None = None,
    created_by_role: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:  # allow-secret: runtime signing material
    """Mint one guest link for a booked candidate, for a human to send.

    A portal link is only offered once the recording is confirmed: before
    ``BOOKED`` there is no date to plan around and no reason to ask a guest for
    a care profile. The returned URL is handed back to the operator, never
    transmitted — the portal has no outbound capability.
    """
    status = str(opportunity.get("status") or "")
    if status not in PORTAL_ELIGIBLE_STATES:
        raise PortalError(
            f"a portal link is only offered for a {sorted(PORTAL_ELIGIBLE_STATES)[0]} candidate; this one is {status or 'unknown'}"
        )
    return create_portal_token(
        conn,
        tenant_id=str(opportunity["tenant_id"]),
        show_id=str(opportunity["show_id"]),
        guest_id=_opportunity_guest(opportunity),
        secret=secret,  # allow-secret: runtime signing material, not a value
        base_url=base_url,
        ttl_days=ttl_days,
        enabled=enabled,
        brand=brand,
        offered_dates=offered_dates,
        created_by=created_by,
        created_by_role=created_by_role,
        now=now,
    )


def show_portal_status(conn: Any, *, tenant_id: str, show_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Report every guest in one show who has been sent a portal link.

    The dashboard reads this once per refresh and keys it by guest id, so a
    slate of candidates costs one request rather than one per card.
    """
    tenant, show = platform._scope(tenant_id, show_id)
    guests = store.fetch_all(
        conn,
        "SELECT DISTINCT guest_id FROM portal_tokens WHERE tenant_id = ? AND show_id = ? ORDER BY guest_id",
        (tenant, show),
    )
    return [
        portal_status(conn, tenant_id=tenant, show_id=show, guest_id=str(row["guest_id"]), now=now) for row in guests
    ]


def _portal_asset(name: str) -> str:
    path = DASHBOARD_DIR / "guest" / name
    if not path.is_file():
        raise PortalError(f"the guest portal asset {name} is missing from this installation", 503)
    return path.read_text(encoding="utf-8")


def create_app(
    *,
    conn: Any,
    secret: str,  # allow-secret: runtime signing material, not a value
    enabled: bool = False,
    brand: dict[str, Any] | None = None,
    vault: encryption.FieldVault | None = None,
) -> Any:  # allow-secret: runtime signing material
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    except ImportError as exc:  # pragma: no cover
        raise PortalError("guest portal requires the api extra", 503) from exc
    try:
        portal_brand = branding.validate_brand(brand if brand is not None else branding.load_brand())
    except branding.BrandError as exc:
        raise PortalError(f"guest portal brand is invalid: {exc}", 503) from exc
    brand_name = str(portal_brand["show_name"])
    show_name = escape(brand_name)
    document = _portal_asset("index.html").replace("HOSPES", show_name)
    script = _portal_asset("app.js")
    layout = _portal_asset("styles.css")
    app = FastAPI(title=f"{brand_name} Guest Portal", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def noindex(request: _FastAPIRequest, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["x-robots-tag"] = "noindex, nofollow"
        response.headers["cache-control"] = "no-store"
        response.headers["content-security-policy"] = branding.PORTAL_CONTENT_SECURITY_POLICY
        return response

    def require_enabled() -> None:
        if not enabled:
            raise HTTPException(status_code=404, detail="guest portal is disabled")

    @app.get(PORTAL_ROOT, response_class=HTMLResponse)
    def landing() -> str:
        require_enabled()
        return document

    @app.get(branding.PORTAL_STYLESHEET_PATH)
    def brand_stylesheet() -> Any:
        require_enabled()
        return Response(
            content=branding.portal_stylesheet(portal_brand),
            media_type="text/css; charset=utf-8",
        )

    @app.get(PORTAL_LAYOUT_STYLESHEET_PATH)
    def layout_stylesheet() -> Any:
        require_enabled()
        return Response(content=layout, media_type="text/css; charset=utf-8")

    def brand_asset_response(kind: str) -> Any:
        require_enabled()
        try:
            asset_path, media_type = branding.brand_asset(portal_brand, kind)
        except branding.BrandError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(asset_path, media_type=media_type)

    @app.get(branding.PORTAL_LOGO_PATH)
    def brand_logo() -> Any:
        return brand_asset_response("logo")

    @app.get(branding.PORTAL_FAVICON_PATH)
    def brand_favicon() -> Any:
        return brand_asset_response("favicon")

    @app.get(PORTAL_SCRIPT_PATH)
    def client_script() -> Any:
        require_enabled()
        return Response(content=script, media_type="application/javascript")

    @app.post("/guest/session")
    async def session(request: _FastAPIRequest) -> JSONResponse:
        require_enabled()
        payload = await _json_body(request)
        try:
            opened = open_session(
                conn, token=str(payload.get("token", "")), secret=secret  # allow-secret: runtime signing material, not a value
            )  # allow-secret: runtime token relay
        except (PortalError, platform.PlatformError) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return JSONResponse(opened)

    @app.post("/guest/intake")
    async def intake(request: _FastAPIRequest) -> JSONResponse:
        require_enabled()
        payload = await _json_body(request)
        try:
            if "session" in payload:
                result = submit_intake(
                    conn,
                    vault,
                    session=str(payload.get("session", "")),
                    care_profile=payload.get("care_profile"),
                    consent=payload.get("consent"),
                    preferred_dates=payload.get("preferred_dates"),
                )  # allow-secret: runtime session relay
            else:
                result = complete_intake(
                    conn,
                    token=str(payload.get("token", "")),  # allow-secret: runtime signing material, not a value
                    secret=secret,  # allow-secret: runtime signing material, not a value
                    consent_ref=str(payload.get("consent_ref", "")),
                    intake_ref=str(payload.get("intake_ref", "")),
                    availability_ref=str(payload.get("availability_ref", "")),
                )  # allow-secret: runtime token relay
        except (PortalError, platform.PlatformError) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return JSONResponse({"ok": True, "receipt_ids": [result["id"]], "receipts": result.get("receipts", [])})

    return app


async def _json_body(request: Any) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise _body_error() from exc
    if not isinstance(payload, dict):
        raise _body_error()
    return payload


def _body_error() -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=422, detail="the portal expects a JSON object")


__all__ = [
    "CONSENT_CLAUSES",
    "CONSENT_VERSION",
    "PORTAL_ELIGIBLE_STATES",
    "PREFERRED_DATE_COUNT",
    "PortalError",
    "SESSION_TTL_MINUTES",
    "complete_intake",
    "consent_checksum",
    "consent_clauses",
    "create_app",
    "create_portal_token",
    "link_for_opportunity",
    "open_session",
    "portal_status",
    "show_portal_status",
    "submit_intake",
]
