"""Publishing pipeline: RSS, video, and clip packages behind a human gate.

Migration 9 gave HOSPES a ``distributions`` row and an empty
``delivery_receipts`` table.  A row could move draft → authorized → published,
but the *artifact* being published was an unvalidated blob, a retry was
indistinguishable from a first attempt, and the delivery table nothing wrote to
stayed empty.  This module owns the missing pipeline:

* **Packages.** Three validated families — ``rss``, ``video``, and ``clip`` —
  behind :class:`DistributionPackage`.  Free text is bounded and control-char
  free, every reference is an opaque custody reference or a public ``https``
  URL, chapters are ordered, and unknown fields are refused.  Only the minimum
  that makes an artifact identifiable is required; :func:`package_completeness`
  reports the rest so an operator sees what is still missing without being
  blocked from drafting it.
* **Previews.** :func:`preview` renders the package the operator is about to
  hand a platform: a parseable RSS ``<item>``, a YouTube metadata document with
  timestamped chapters, or a clip-queue card.  Previews are pure — they read
  nothing and write nothing.
* **Adapters.** :func:`delivery_adapter` resolves the distribution capability
  through the provider registry and reports the chosen adapter, or a visible
  ``unconfigured``/``blocked`` state with its reason.  No credential is ever
  invented and no adapter is silently substituted for another.
* **Authorization and manual receipts.** Nothing leaves HOSPES.  A publication
  is *recorded*, and only after a human authorization receipt exists and both
  the rights and sponsor gates pass.  Every delivery names its
  ``delivery_mode``: today that is always ``manual_receipt``, because no live
  adapter has an authenticated smoke receipt.
* **Retry-safe transitions.** Every transition is idempotent on its own key, and
  each delivery attempt — succeeded or failed — appends one immutable
  ``delivery_receipts`` row at attempt *n+1*.  A published external reference is
  immutable; re-publishing the same reference returns the row, a different one
  is a conflict.

Every state change also appends an attributable ``distribution_receipts`` row,
the sibling of ``clearance_receipts`` and ``sponsorship_receipts``.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import clearances, configuration, generation, platform, privacy, providers, store


PLATFORMS = frozenset({"rss", "youtube", "tiktok", "reels", "shorts", "clip_queue"})
SHORT_FORM_PLATFORMS = ("tiktok", "reels", "shorts")
#: Which validated package family each platform carries.  The close condition
#: names the three families directly: RSS, video, and clip.
PACKAGE_FAMILIES = {
    "rss": "rss",
    "youtube": "video",
    "clip_queue": "clip",
    "tiktok": "clip",
    "reels": "clip",
    "shorts": "clip",
}
STATUSES = ("draft", "blocked", "authorized", "scheduled", "published", "failed")
#: A job in one of these states has an authorization receipt and may be
#: delivered.  ``failed`` is included deliberately: a retry is the whole point
#: of recording the failure.
DELIVERABLE_STATUSES = frozenset({"authorized", "scheduled", "failed"})
AUTHORIZABLE_STATUSES = frozenset({"draft", "blocked", "authorized", "failed"})
OUTBOUND_MODES = frozenset({"manual_receipt", "provider_connected"})
DELIVERY_MODES = ("manual_receipt", "provider_connected")

READ_ROLES = frozenset({"host", "network_operator", "producer", "editorial_owner", "relationship_owner"})
WRITE_ROLES = frozenset({"producer", "editorial_owner", "relationship_owner"})
PUBLISH_ROLES = frozenset({"editorial_owner", "relationship_owner"})

#: Recorded when a caller supplies no operator identity.  Only internal callers
#: reach that path — every HTTP route passes the authenticated session's actor —
#: and the receipt says so rather than borrowing somebody's name.
SYSTEM_ACTOR = "system"
SYSTEM_ROLE = "system"

#: Per-platform adapter preference.  ``manual_external_receipt`` is always the
#: last resort, which is what keeps a missing credential a visible fallback
#: instead of a failure.
_PLATFORM_ADAPTERS = {
    "rss": ("rss", "manual_external_receipt"),
    "youtube": ("youtube_metadata", "manual_external_receipt"),
    "clip_queue": ("clip_queue", "manual_external_receipt"),
    "tiktok": ("clip_queue", "manual_external_receipt"),
    "reels": ("clip_queue", "manual_external_receipt"),
    "shorts": ("clip_queue", "manual_external_receipt"),
}

#: The events an auto-publish webhook would carry once a live adapter exists.
WEBHOOK_EVENTS = (
    "distribution.scheduled",
    "distribution.published",
    "distribution.failed",
)
WEBHOOK_PAYLOAD_SCHEMA = "spec/distribution.schema.json"

_RSS_KEYS = frozenset({"title", "description", "audio_url", "duration", "chapters", "guid"})
_VIDEO_KEYS = frozenset({"title", "description", "tags", "chapters", "playlist", "thumbnail_ref"})
_CLIP_KEYS = frozenset({"start_seconds", "end_seconds", "target_platform", "caption_ref", "hashtags"})
_PACKAGE_KEYS = {"rss": _RSS_KEYS, "video": _VIDEO_KEYS, "clip": _CLIP_KEYS}
_REQUIRED_KEYS = {
    "rss": ("title",),
    "video": ("title",),
    "clip": ("start_seconds", "end_seconds", "target_platform", "caption_ref"),
}
#: Fields a complete package carries.  A package missing one of these still
#: drafts — the operator is mid-preparation — and :func:`package_completeness`
#: names what is outstanding.
_RECOMMENDED_KEYS = {
    "rss": ("description", "audio_url", "duration"),
    "video": ("description", "tags"),
    "clip": ("hashtags",),
}

MAX_TITLE = 160
MAX_DESCRIPTION = 5_000
MAX_CHAPTERS = 100
MAX_CHAPTER_TITLE = 120
MAX_TAGS = 30
MAX_TAG_BYTES = 500
MAX_HASHTAGS = 30
MAX_CLIP_SECONDS = 600
MAX_LIST_LIMIT = 500

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTTPS_URL = re.compile(
    r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
    r"(?::\d{1,5})?(?:/[^\s<>\"']{0,1800})?$"
)
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,29}$")
_HASHTAG = re.compile(r"^[A-Za-z0-9_]{1,60}$")
_PLAYLIST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _:./-]{0,95}$")
_DURATION = re.compile(r"^(?:(\d{1,3}):)?([0-5]?\d):([0-5]\d)$")


class DistributionError(platform.PlatformError):
    """A safe distribution validation, authorization, or transition error.

    It subclasses :class:`hospes.platform.PlatformError` so the HTTP adapter
    maps it to a status code through the boundary it already has, and so the
    existing callers that catch ``PlatformError`` keep working.
    """


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _now(value: datetime | None = None) -> str:
    return (value or generation.now()).astimezone(timezone.utc).isoformat()


def _require_role(actor_role: Any, allowed: frozenset[str], action: str) -> str:
    """Gate a supplied operator role; an absent role is an unattributed write."""
    if actor_role is None:
        return SYSTEM_ROLE
    if not isinstance(actor_role, str) or actor_role not in allowed:
        raise DistributionError(f"operator role cannot {action}", 403)
    return actor_role


def _actor(actor_id: Any) -> str:
    return SYSTEM_ACTOR if actor_id is None else platform._ref(actor_id, "actor_id")


def _scope(tenant_id: str, show_id: str) -> tuple[str, str]:
    return platform._scope(tenant_id, show_id)


def _text(value: Any, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DistributionError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise DistributionError(f"{field_name} is required")
    if len(normalized) > limit:
        raise DistributionError(f"{field_name} exceeds {limit} characters")
    if _CONTROL.search(normalized):
        raise DistributionError(f"{field_name} contains unsupported control characters")
    return normalized


def _media_reference(value: Any, field_name: str) -> str:
    """Accept a public ``https`` URL or an opaque custody reference, nothing else."""
    if not isinstance(value, str):
        raise DistributionError(f"{field_name} must be a public https URL or an opaque custody reference")
    normalized = value.strip()
    if _HTTPS_URL.fullmatch(normalized):
        if privacy.contact_kind(normalized) is not None:
            raise DistributionError(f"{field_name} must not carry contact data")
        return normalized
    if normalized.lower().startswith("http://"):
        # A plain-http URL matches the opaque-reference grammar by accident.
        # Accepting it would ship a cleartext media link as if it were custody.
        raise DistributionError(f"{field_name} must use https, not plain http")
    return platform._ref(normalized, field_name)


def _seconds(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DistributionError(f"{field_name} must be a whole number of seconds")
    if not 0 <= value <= 86_400:
        raise DistributionError(f"{field_name} must be between 0 and 86400 seconds")
    return value


def _duration(value: Any) -> str:
    """Normalize ``HH:MM:SS``, ``MM:SS``, or a second count to ``HH:MM:SS``."""
    if isinstance(value, bool):
        raise DistributionError("duration must be HH:MM:SS or a whole number of seconds")
    if isinstance(value, int):
        total = _seconds(value, "duration")
    else:
        if not isinstance(value, str):
            raise DistributionError("duration must be HH:MM:SS or a whole number of seconds")
        match = _DURATION.fullmatch(value.strip())
        if match is None:
            raise DistributionError("duration must be HH:MM:SS or a whole number of seconds")
        hours, minutes, seconds = match.groups()
        total = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _chapters(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DistributionError("chapters must be a list of {start_seconds, title} objects")
    if len(value) > MAX_CHAPTERS:
        raise DistributionError(f"chapters cannot exceed {MAX_CHAPTERS} entries")
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise DistributionError("each chapter must be an object")
        unknown = sorted(set(item) - {"start_seconds", "start", "title"})
        if unknown:
            raise DistributionError(f"chapter has unsupported fields: {unknown}")
        if "start_seconds" in item and "start" in item:
            raise DistributionError("declare the chapter start exactly once")
        normalized.append(
            {
                "start_seconds": _seconds(item.get("start_seconds", item.get("start")), "chapter start_seconds"),
                "title": _text(item.get("title"), "chapter title", MAX_CHAPTER_TITLE),
            }
        )
    starts = [chapter["start_seconds"] for chapter in normalized]
    if starts != sorted(set(starts)):
        raise DistributionError("chapter starts must be distinct and ascending")
    return normalized


def _tags(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DistributionError("tags must be a list of short strings")
    if len(value) > MAX_TAGS:
        raise DistributionError(f"tags cannot exceed {MAX_TAGS} entries")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or _TAG.fullmatch(item.strip()) is None:
            raise DistributionError("each tag must be 1-30 alphanumeric characters, spaces, dashes, or underscores")
        tag = item.strip()
        if tag in normalized:
            raise DistributionError(f"tag {tag!r} is declared twice")
        normalized.append(tag)
    if len(",".join(normalized)) > MAX_TAG_BYTES:
        raise DistributionError(f"tags exceed the {MAX_TAG_BYTES}-character platform budget")
    return normalized


def _hashtags(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DistributionError("hashtags must be a list of strings")
    if len(value) > MAX_HASHTAGS:
        raise DistributionError(f"hashtags cannot exceed {MAX_HASHTAGS} entries")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise DistributionError("each hashtag must be text")
        tag = item.strip().lstrip("#")
        if _HASHTAG.fullmatch(tag) is None:
            raise DistributionError("each hashtag must be 1-60 alphanumeric or underscore characters")
        if f"#{tag}" in normalized:
            raise DistributionError(f"hashtag {tag!r} is declared twice")
        normalized.append(f"#{tag}")
    return normalized


def _target_platform(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in SHORT_FORM_PLATFORMS:
        raise DistributionError(f"target_platform must be one of {list(SHORT_FORM_PLATFORMS)}")
    return value.strip()


def _future_timestamp(value: Any, field_name: str, *, now: datetime | None = None) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DistributionError(f"{field_name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DistributionError(f"{field_name} must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    if normalized <= (now or generation.now()).astimezone(timezone.utc):
        raise DistributionError(f"{field_name} must be in the future")
    return normalized.isoformat()


def _checksum(values: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _platform_name(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in PLATFORMS:
        raise DistributionError(f"platform must be one of {sorted(PLATFORMS)}")
    return value.strip()


# ---------------------------------------------------------------------------
# Packages.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistributionPackage:
    """One validated publication package and the family it belongs to."""

    platform: str
    family: str
    values: dict[str, Any]

    @property
    def checksum(self) -> str:
        return _checksum(self.values)

    @classmethod
    def from_mapping(cls, platform_name: Any, metadata: Any) -> "DistributionPackage":
        selected = _platform_name(platform_name)
        family = PACKAGE_FAMILIES[selected]
        if not isinstance(metadata, Mapping):
            raise DistributionError("package metadata must be an object")
        unknown = sorted(set(metadata) - _PACKAGE_KEYS[family])
        if unknown:
            raise DistributionError(f"{family} package has unsupported fields: {unknown}")
        missing = [key for key in _REQUIRED_KEYS[family] if metadata.get(key) is None]
        if missing:
            raise DistributionError(f"{family} package requires {missing}")
        builder = {"rss": cls._rss, "video": cls._video, "clip": cls._clip}[family]
        return cls(platform=selected, family=family, values=builder(selected, metadata))

    @staticmethod
    def _rss(_platform_name: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {"title": _text(metadata.get("title"), "title", MAX_TITLE)}
        if metadata.get("description") is not None:
            values["description"] = _text(metadata["description"], "description", MAX_DESCRIPTION)
        if metadata.get("audio_url") is not None:
            values["audio_url"] = _media_reference(metadata["audio_url"], "audio_url")
        if metadata.get("duration") is not None:
            values["duration"] = _duration(metadata["duration"])
        if metadata.get("chapters") is not None:
            values["chapters"] = _chapters(metadata["chapters"])
        if metadata.get("guid") is not None:
            values["guid"] = platform._ref(metadata["guid"], "guid")
        return values

    @staticmethod
    def _video(_platform_name: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {"title": _text(metadata.get("title"), "title", MAX_TITLE)}
        if metadata.get("description") is not None:
            values["description"] = _text(metadata["description"], "description", MAX_DESCRIPTION)
        if metadata.get("tags") is not None:
            values["tags"] = _tags(metadata["tags"])
        if metadata.get("chapters") is not None:
            values["chapters"] = _chapters(metadata["chapters"])
        if metadata.get("playlist") is not None:
            playlist = _text(metadata["playlist"], "playlist", 96)
            if _PLAYLIST.fullmatch(playlist) is None:
                raise DistributionError("playlist must be a short playlist name or reference")
            values["playlist"] = playlist
        if metadata.get("thumbnail_ref") is not None:
            values["thumbnail_ref"] = _media_reference(metadata["thumbnail_ref"], "thumbnail_ref")
        return values

    @staticmethod
    def _clip(selected_platform: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        start = _seconds(metadata.get("start_seconds"), "start_seconds")
        end = _seconds(metadata.get("end_seconds"), "end_seconds")
        if end <= start:
            raise DistributionError("end_seconds must be greater than start_seconds")
        if end - start > MAX_CLIP_SECONDS:
            raise DistributionError(f"a clip cannot exceed {MAX_CLIP_SECONDS} seconds")
        target = _target_platform(metadata.get("target_platform"))
        if selected_platform in SHORT_FORM_PLATFORMS and target != selected_platform:
            raise DistributionError("target_platform must match the short-form platform being drafted")
        values: dict[str, Any] = {
            "start_seconds": start,
            "end_seconds": end,
            "target_platform": target,
            "caption_ref": platform._ref(metadata.get("caption_ref"), "caption_ref"),
        }
        if metadata.get("hashtags") is not None:
            values["hashtags"] = _hashtags(metadata["hashtags"])
        return values


def package_completeness(platform_name: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Report which recommended fields a validated package is still missing.

    Completeness is advisory on purpose: an operator drafts a package before the
    audio is cut and the description is written, and refusing the draft would
    push that half-finished state back into a spreadsheet.  What actually blocks
    a publication is the rights gate, the sponsor gate, and the human
    authorization receipt.
    """
    family = PACKAGE_FAMILIES[_platform_name(platform_name)]
    missing = [key for key in _RECOMMENDED_KEYS[family] if metadata.get(key) in (None, "", [], {})]
    return {"family": family, "complete": not missing, "missing": missing}


# ---------------------------------------------------------------------------
# Previews.  Pure renderers: they read nothing and write nothing.
# ---------------------------------------------------------------------------


def _chapter_timestamp(start_seconds: int) -> str:
    hours, remainder = divmod(int(start_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def rss_preview(metadata: Mapping[str, Any]) -> str:
    """Render one podcast RSS ``<item>`` from an episode package."""
    title = html.escape(str(metadata.get("title", "Untitled episode")))
    description = html.escape(str(metadata.get("description", "")))
    audio_url = html.escape(str(metadata.get("audio_url", "")))
    duration = html.escape(str(metadata.get("duration", "")))
    guid = html.escape(str(metadata.get("guid", "")))
    chapters = "".join(
        '<psc:chapter start="{start}" title="{title}"/>'.format(
            start=html.escape(_chapter_timestamp(chapter.get("start_seconds", 0))),
            title=html.escape(str(chapter.get("title", ""))),
        )
        for chapter in metadata.get("chapters", [])
        if isinstance(chapter, Mapping)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<item xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:psc="http://podlove.org/simple-chapters">'
        f"<title>{title}</title>"
        f"<description>{description}</description>"
        + (f'<guid isPermaLink="false">{guid}</guid>' if guid else "")
        + f'<enclosure url="{audio_url}" type="audio/mpeg"/>'
        f"<itunes:duration>{duration}</itunes:duration>"
        + (f'<psc:chapters version="1.2">{chapters}</psc:chapters>' if chapters else "")
        + "</item>"
    )


def youtube_preview(metadata: Mapping[str, Any]) -> str:
    """Render the YouTube metadata document an operator pastes into the studio."""
    lines = [f"Title: {metadata.get('title', 'Untitled episode')}"]
    if metadata.get("playlist"):
        lines.append(f"Playlist: {metadata['playlist']}")
    if metadata.get("thumbnail_ref"):
        lines.append(f"Thumbnail: {metadata['thumbnail_ref']}")
    if metadata.get("tags"):
        lines.append(f"Tags: {', '.join(metadata['tags'])}")
    lines.append("")
    lines.append(str(metadata.get("description", "")))
    chapters = [chapter for chapter in metadata.get("chapters", []) if isinstance(chapter, Mapping)]
    if chapters:
        lines.append("")
        lines.append("Chapters:")
        lines.extend(
            f"{_chapter_timestamp(chapter.get('start_seconds', 0))} {chapter.get('title', '')}" for chapter in chapters
        )
    return "\n".join(lines).rstrip() + "\n"


def clip_preview(metadata: Mapping[str, Any]) -> str:
    """Render one clip-queue card: window, target, caption custody, hashtags."""
    start = int(metadata.get("start_seconds", 0))
    end = int(metadata.get("end_seconds", 0))
    lines = [
        f"Target: {metadata.get('target_platform', 'unassigned')}",
        f"Window: {_chapter_timestamp(start)} → {_chapter_timestamp(end)} ({max(end - start, 0)}s)",
        f"Caption: {metadata.get('caption_ref', 'no caption reference')}",
    ]
    if metadata.get("hashtags"):
        lines.append(f"Hashtags: {' '.join(metadata['hashtags'])}")
    return "\n".join(lines) + "\n"


_PREVIEW_RENDERERS = {"rss": rss_preview, "video": youtube_preview, "clip": clip_preview}
_PREVIEW_FORMATS = {"rss": "xml", "video": "text", "clip": "text"}


def preview(platform_name: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Render the package for one platform and report what is still missing."""
    selected = _platform_name(platform_name)
    family = PACKAGE_FAMILIES[selected]
    package = DistributionPackage.from_mapping(selected, metadata)
    return {
        "platform": selected,
        "family": family,
        "format": _PREVIEW_FORMATS[family],
        "body": _PREVIEW_RENDERERS[family](package.values),
        "package_checksum": package.checksum,
        "completeness": package_completeness(selected, package.values),
    }


# ---------------------------------------------------------------------------
# Adapters and webhook readiness.
# ---------------------------------------------------------------------------


def delivery_adapter(
    platform_name: str,
    *,
    registry: providers.ProviderRegistry | None = None,
) -> dict[str, Any]:
    """Resolve the distribution adapter for one platform, or say why there is none.

    A missing credential is never invented and never silently swapped for a
    different vendor: the report names the adapter that was actually chosen, its
    mode, and the reason every blocked candidate was skipped.
    """
    selected = _platform_name(platform_name)
    resolved = registry if registry is not None else providers.ProviderRegistry()
    try:
        adapter = resolved.choose("distribution", _PLATFORM_ADAPTERS[selected])
    except providers.ProviderError as exc:
        return {
            "platform": selected,
            "provider": None,
            "mode": None,
            "status": "unconfigured",
            "delivery_mode": "manual_receipt",
            "reason": str(exc),
        }
    mode = getattr(adapter, "mode", "manual")
    return {
        "platform": selected,
        "provider": adapter.name,
        "mode": mode,
        "status": "ready",
        "delivery_mode": "provider_connected" if mode == "live" else "manual_receipt",
        "reason": None,
    }


def webhook_contract(
    show_id: str | None = None,
    *,
    registry: providers.ProviderRegistry | None = None,
) -> dict[str, Any]:
    """Report what an auto-publish webhook would need, and what is missing today.

    The pipeline is webhook-ready in the only sense that means anything: the
    events, the payload schema, and the authorization precondition are declared
    and stable.  Whether a webhook may actually fire is a *visible* state, not a
    silent default — it stays ``unconfigured`` until a show opts into
    ``provider_connected`` outbound mode and a live adapter verifies.
    """
    outbound_mode = "draft_only"
    if show_id:
        try:
            outbound_mode = configuration.load_show(show_id).outbound_mode
        except configuration.ConfigurationError:
            outbound_mode = "draft_only"
    adapters = {name: delivery_adapter(name, registry=registry) for name in sorted(PLATFORMS)}
    connected = sorted(name for name, state in adapters.items() if state["delivery_mode"] == "provider_connected")
    if outbound_mode != "provider_connected":
        status = "unconfigured"
        reason = f"show outbound mode is {outbound_mode}; auto-publish requires provider_connected"
    elif not connected:
        status = "unconfigured"
        reason = "no distribution adapter has an authenticated live smoke receipt"
    else:  # pragma: no cover - reachable only once a live adapter is provisioned
        status = "ready"
        reason = None
    return {
        "status": status,
        "reason": reason,
        "outbound_mode": outbound_mode,
        "events": list(WEBHOOK_EVENTS),
        "payload_schema": WEBHOOK_PAYLOAD_SCHEMA,
        "requires_authorization_receipt": True,
        "connected_platforms": connected,
        "adapters": adapters,
    }


def record_adapter_verification(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str | None = None,
    registry: providers.ProviderRegistry | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Write one provider receipt per distribution adapter's current state."""
    _require_role(actor_role, WRITE_ROLES, "verify distribution adapters")
    tenant, show = _scope(tenant_id, show_id)
    resolved = registry if registry is not None else providers.ProviderRegistry()
    return resolved.record_verification(
        conn,
        tenant_id=tenant,
        show_id=show,
        capability="distribution",
        now=now,
    )


# ---------------------------------------------------------------------------
# Receipts.
# ---------------------------------------------------------------------------


def _receipt(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    row: Mapping[str, Any],
    event_type: str,
    from_status: str | None,
    to_status: str,
    actor_id: str,
    actor_role: str,
    attempt: int = 0,
    evidence_ref: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one attributable distribution receipt (no workflow state changes)."""
    ordinal = store.fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM distribution_receipts WHERE tenant_id = ? AND show_id = ? AND distribution_id = ?",
        (tenant_id, show_id, row["id"]),
    )
    receipt = {
        "id": generation.new_id("distribution_receipt"),
        "tenant_id": tenant_id,
        "show_id": show_id,
        "distribution_id": row["id"],
        "episode_id": row["episode_id"],
        "platform": row["platform"],
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "attempt": attempt,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "evidence_ref": evidence_ref,
        "correlation_id": f"distribution://{row['id']}/{int(ordinal['count'] if ordinal else 0) + 1}",
        "created_at": _now(now),
    }
    store.insert(conn, "distribution_receipts", receipt)
    return receipt


def _next_attempt(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, distribution_id: str) -> int:
    """Derive the next delivery attempt from the immutable evidence, not a counter."""
    row = store.fetch_one(
        conn,
        "SELECT MAX(attempt) AS highest FROM delivery_receipts "
        "WHERE tenant_id = ? AND show_id = ? AND distribution_id = ?",
        (tenant_id, show_id, distribution_id),
    )
    highest = row["highest"] if row and row["highest"] is not None else 0
    return int(highest) + 1


def _job(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, distribution_id: Any) -> dict[str, Any]:
    row = store.fetch_one(
        conn,
        "SELECT * FROM distributions WHERE id = ? AND tenant_id = ? AND show_id = ?",
        (distribution_id, tenant_id, show_id),
    )
    if not row:
        raise DistributionError("distribution job not found", 404)
    return row


def _gate(conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, episode_id: str, action: str) -> None:
    """Refuse an action whose episode is blocked by rights or sponsor state."""
    gate = clearances.publication_gate(conn, tenant_id=tenant_id, show_id=show_id, episode_id=episode_id)
    if not gate["publishable"]:
        raise DistributionError(f"uncleared rights items block publishing: {gate['reason']}", 409)
    blockers = platform.publish_blockers(conn, tenant_id=tenant_id, show_id=show_id, episode_id=episode_id)
    if blockers:
        raise DistributionError(f"{len(blockers)} publication blocker(s) prevent {action}", 409)


# ---------------------------------------------------------------------------
# Transitions.
# ---------------------------------------------------------------------------


def create_draft(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    platform_name: str,
    metadata: Mapping[str, Any],
    idempotency_key: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Draft one validated publication package for one episode and platform.

    Re-drafting under the same idempotency key returns the existing row rather
    than a second job: the operator retried, the platform did not gain a
    duplicate.
    """
    role = _require_role(actor_role, WRITE_ROLES, "draft a publication")
    tenant, show = _scope(tenant_id, show_id)
    actor = _actor(actor_id)
    episode = platform._ref(episode_id, "episode_id")
    package = DistributionPackage.from_mapping(platform_name, metadata)
    key = platform._ref(idempotency_key, "idempotency_key")
    existing = store.fetch_one(
        conn,
        "SELECT * FROM distributions WHERE tenant_id = ? AND show_id = ? AND idempotency_key = ?",
        (tenant, show, key),
    )
    if existing:
        return existing
    blockers = platform.publish_blockers(conn, tenant_id=tenant, show_id=show, episode_id=episode)
    timestamp = _now(now)
    row = {
        "id": generation.new_id("distribution"),
        "tenant_id": tenant,
        "show_id": show,
        "episode_id": episode,
        "platform": package.platform,
        "target_platform": package.values.get("target_platform"),
        "status": "blocked" if blockers else "draft",
        "metadata": dict(package.values),
        "package_checksum": package.checksum,
        "authorization_receipt_ref": None,
        "idempotency_key": key,
        "external_id_ref": None,
        "scheduled_at": None,
        "published_at": None,
        "attempt_count": 0,
        "last_attempt_at": None,
        "last_error_ref": None,
        "recorded_by": actor,
        "recorded_by_role": role,
        "scheduled_by": None,
        "scheduled_by_role": None,
        "correlation_id": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    store.insert(conn, "distributions", row)
    row["correlation_id"] = f"distribution://{row['id']}"
    store.update(conn, "distributions", row["id"], {"correlation_id": row["correlation_id"]})
    _receipt(
        conn,
        tenant_id=tenant,
        show_id=show,
        row=row,
        event_type="distribution.clip_queued" if package.family == "clip" else "distribution.drafted",
        from_status=None,
        to_status=row["status"],
        actor_id=actor,
        actor_role=role,
        evidence_ref=package.checksum,
        now=now,
    )
    conn.commit()
    return row


def queue_clip(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    start_seconds: int,
    end_seconds: int,
    platform_name: str,
    caption_ref: str,
    idempotency_key: str,
    hashtags: Sequence[str] | None = None,
    actor_id: str | None = None,
    actor_role: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Queue one short-form clip against an episode's timeline window."""
    metadata: dict[str, Any] = {
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "target_platform": platform_name,
        "caption_ref": caption_ref,
    }
    if hashtags is not None:
        metadata["hashtags"] = list(hashtags)
    return create_draft(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        episode_id=episode_id,
        platform_name="clip_queue",
        metadata=metadata,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        actor_role=actor_role,
        now=now,
    )


def authorize_publish(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    distribution_id: str,
    outbound_mode: str,
    authorized_by: str,
    authorization_ref: str,
    idempotency_key: str,
    actor_role: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind one human authorization receipt to a publication job.

    Nothing is delivered here.  This records that a named human authorized a
    publication whose rights and sponsor gates were green at the moment they
    said so — and both gates are checked again before anything is marked
    published, because a clearance can be reopened afterwards.
    """
    role = _require_role(actor_role, PUBLISH_ROLES, "authorize a publication")
    row = _job(conn, tenant_id=tenant_id, show_id=show_id, distribution_id=distribution_id)
    if outbound_mode not in OUTBOUND_MODES:
        raise DistributionError("show outbound mode does not permit publish authorization", 403)
    if row["status"] == "published":
        return row
    if row["status"] not in AUTHORIZABLE_STATUSES and row["status"] != "scheduled":
        raise DistributionError("distribution job is not eligible for authorization", 409)
    _gate(conn, tenant_id=tenant_id, show_id=show_id, episode_id=row["episode_id"], action="authorization")
    receipt = providers.require_human_authorization(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        action="publish",
        subject_ref=distribution_id,
        idempotency_key=idempotency_key,
        authorized_by=authorized_by,
        authorization_ref=authorization_ref,
        now=now,
    )
    status = "scheduled" if row["status"] == "scheduled" else "authorized"
    store.update(
        conn,
        "distributions",
        distribution_id,
        {"authorization_receipt_ref": receipt["id"], "status": status, "updated_at": _now(now)},
    )
    _receipt(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        row=row,
        event_type="distribution.authorized",
        from_status=row["status"],
        to_status=status,
        actor_id=platform._ref(authorized_by, "authorized_by"),
        actor_role=role,
        evidence_ref=receipt["id"],
        now=now,
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM distributions WHERE id = ?", (distribution_id,)) or row


def schedule_publication(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    distribution_id: str,
    scheduled_at: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Schedule an already-authorized publication for a future moment.

    Scheduling deliberately requires the authorization receipt first.  A
    scheduled outbound action with no human behind it is exactly the automation
    ``AGENTS.md`` forbids, and a future timestamp is not a weaker gate — it is
    the same gate, later.
    """
    role = _require_role(actor_role, WRITE_ROLES, "schedule a publication")
    actor = _actor(actor_id)
    row = _job(conn, tenant_id=tenant_id, show_id=show_id, distribution_id=distribution_id)
    if row["status"] == "published":
        raise DistributionError("a published distribution cannot be rescheduled", 409)
    if row["status"] not in {"authorized", "scheduled"} or not row.get("authorization_receipt_ref"):
        raise DistributionError("scheduling requires a human authorization receipt", 403)
    target = _future_timestamp(scheduled_at, "scheduled_at", now=now)
    if row["status"] == "scheduled" and row.get("scheduled_at") == target:
        return row
    _gate(conn, tenant_id=tenant_id, show_id=show_id, episode_id=row["episode_id"], action="scheduling")
    store.update(
        conn,
        "distributions",
        distribution_id,
        {
            "status": "scheduled",
            "scheduled_at": target,
            "scheduled_by": actor,
            "scheduled_by_role": role,
            "updated_at": _now(now),
        },
    )
    _receipt(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        row=row,
        event_type="distribution.scheduled",
        from_status=row["status"],
        to_status="scheduled",
        actor_id=actor,
        actor_role=role,
        evidence_ref=target,
        now=now,
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM distributions WHERE id = ?", (distribution_id,)) or row


def _authorization(
    conn: store.DatabaseConnection, *, tenant_id: str, show_id: str, row: Mapping[str, Any]
) -> dict[str, Any]:
    if not row.get("authorization_receipt_ref"):
        raise DistributionError("recording a delivery requires a human authorization receipt", 403)
    authorization = store.fetch_one(
        conn,
        "SELECT * FROM authorization_receipts WHERE id = ? AND tenant_id = ? AND show_id = ? AND subject_ref = ?",
        (row["authorization_receipt_ref"], tenant_id, show_id, row["id"]),
    )
    if not authorization:
        raise DistributionError("distribution authorization receipt is invalid", 403)
    return authorization


def _delivery_receipt(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    row: Mapping[str, Any],
    authorization: Mapping[str, Any],
    adapter: Mapping[str, Any],
    result: str,
    external_reference: str,
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    """Append one immutable delivery attempt to the evidence table.

    ``delivery_receipts`` is unique on (tenant, show, distribution, provider,
    attempt) and foreign-keyed to the authorization receipt for *this* subject
    and the ``publish`` action, so an attempt can never be rewritten and can
    never borrow another job's authorization.
    """
    attempt = _next_attempt(conn, tenant_id=tenant_id, show_id=show_id, distribution_id=row["id"])
    receipt = {
        "id": generation.new_id("delivery_receipt"),
        "tenant_id": tenant_id,
        "show_id": show_id,
        "distribution_id": row["id"],
        "authorization_receipt_id": authorization["id"],
        "authorization_action": "publish",
        "provider": str(adapter.get("provider") or "unconfigured"),
        "attempt": attempt,
        "result": result,
        "delivery_mode": str(adapter.get("delivery_mode") or "manual_receipt"),
        "external_reference": external_reference,
        "payload_checksum": str(row.get("package_checksum") or _checksum(dict(row.get("metadata") or {}))),
        "started_at": started_at,
        "completed_at": completed_at,
        "created_at": completed_at,
    }
    store.insert(conn, "delivery_receipts", receipt)
    return receipt


def mark_published(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    distribution_id: str,
    external_id_ref: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    registry: providers.ProviderRegistry | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record that a human published this package, and file the evidence.

    HOSPES does not publish.  This is the manual receipt for a publication that
    already happened somewhere else: it re-checks both gates, requires the
    authorization receipt, stores the platform's external identifier as
    immutable, and appends one delivery attempt plus one provider receipt.
    """
    role = _require_role(actor_role, PUBLISH_ROLES, "record a publication")
    row = _job(conn, tenant_id=tenant_id, show_id=show_id, distribution_id=distribution_id)
    external_ref = platform._ref(external_id_ref, "external_id_ref")
    if row["status"] == "published":
        if row.get("external_id_ref") == external_ref:
            return row
        raise DistributionError("published distribution external reference is immutable", 409)
    if row["status"] not in DELIVERABLE_STATUSES:
        raise DistributionError("only an authorized distribution can be published", 409)
    _gate(conn, tenant_id=tenant_id, show_id=show_id, episode_id=row["episode_id"], action="publishing")
    authorization = _authorization(conn, tenant_id=tenant_id, show_id=show_id, row=row)
    adapter = delivery_adapter(row["platform"], registry=registry)
    started_at = str(row.get("last_attempt_at") or row["updated_at"])
    timestamp = _now(now)
    delivery = _delivery_receipt(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        row=row,
        authorization=authorization,
        adapter=adapter,
        result="delivered",
        external_reference=external_ref,
        started_at=started_at,
        completed_at=timestamp,
    )
    store.update(
        conn,
        "distributions",
        distribution_id,
        {
            "status": "published",
            "external_id_ref": external_ref,
            "published_at": timestamp,
            "attempt_count": delivery["attempt"],
            "last_attempt_at": timestamp,
            "updated_at": timestamp,
        },
    )
    store.insert(
        conn,
        "provider_receipts",
        {
            "id": generation.new_id("delivery_receipt"),
            "tenant_id": tenant_id,
            "show_id": show_id,
            "capability": "distribution",
            "provider": row["platform"],
            "status": "ready",
            "receipt_ref": external_ref,
            "details": {
                "action": "distribution.published",
                "distribution_id": distribution_id,
                "episode_id": row["episode_id"],
                "authorization_receipt_id": authorization["id"],
                "authorized_by": authorization["authorized_by"],
                "adapter": adapter["provider"],
                "delivery_mode": delivery["delivery_mode"],
                "attempt": delivery["attempt"],
            },
            "created_at": timestamp,
        },
    )
    _receipt(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        row=row,
        event_type="distribution.published",
        from_status=row["status"],
        to_status="published",
        actor_id=_actor(actor_id),
        actor_role=role,
        attempt=delivery["attempt"],
        evidence_ref=external_ref,
        now=now,
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM distributions WHERE id = ?", (distribution_id,)) or row


def record_failure(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    distribution_id: str,
    error_ref: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    registry: providers.ProviderRegistry | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one failed delivery attempt so the retry is honest about being one.

    A failure is evidence, not an erasure: the attempt keeps its own immutable
    ``delivery_receipts`` row, the job returns to a deliverable state under the
    same authorization receipt, and the next attempt is numbered *n+1*.
    """
    role = _require_role(actor_role, WRITE_ROLES, "record a delivery failure")
    row = _job(conn, tenant_id=tenant_id, show_id=show_id, distribution_id=distribution_id)
    failure_ref = platform._ref(error_ref, "error_ref")
    if row["status"] == "published":
        raise DistributionError("a published distribution cannot be marked failed", 409)
    if row["status"] not in DELIVERABLE_STATUSES:
        raise DistributionError("only an authorized distribution can record a delivery failure", 409)
    authorization = _authorization(conn, tenant_id=tenant_id, show_id=show_id, row=row)
    adapter = delivery_adapter(row["platform"], registry=registry)
    timestamp = _now(now)
    delivery = _delivery_receipt(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        row=row,
        authorization=authorization,
        adapter=adapter,
        result="failed",
        external_reference=failure_ref,
        started_at=str(row.get("last_attempt_at") or row["updated_at"]),
        completed_at=timestamp,
    )
    store.update(
        conn,
        "distributions",
        distribution_id,
        {
            "status": "failed",
            "attempt_count": delivery["attempt"],
            "last_attempt_at": timestamp,
            "last_error_ref": failure_ref,
            "updated_at": timestamp,
        },
    )
    _receipt(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        row=row,
        event_type="distribution.failed",
        from_status=row["status"],
        to_status="failed",
        actor_id=_actor(actor_id),
        actor_role=role,
        attempt=delivery["attempt"],
        evidence_ref=failure_ref,
        now=now,
    )
    conn.commit()
    return store.fetch_one(conn, "SELECT * FROM distributions WHERE id = ?", (distribution_id,)) or row


# ---------------------------------------------------------------------------
# Reads: the Publish board and its evidence.
# ---------------------------------------------------------------------------


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIST_LIMIT:
        raise DistributionError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
    return value


def list_distributions(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
    platform_name: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the distribution jobs in scope, newest first, bounded."""
    _require_role(actor_role, READ_ROLES, "read distribution jobs")
    tenant, show = _scope(tenant_id, show_id)
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(platform._ref(episode_id, "episode_id"))
    if platform_name is not None:
        clauses.append("platform = ?")
        params.append(_platform_name(platform_name))
    if status is not None:
        if status not in STATUSES:
            raise DistributionError(f"status must be one of {list(STATUSES)}")
        clauses.append("status = ?")
        params.append(status)
    params.append(_limit(limit))
    return store.fetch_all(
        conn,
        "SELECT * FROM distributions WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    )


def distribution_receipts(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    distribution_id: str,
) -> dict[str, Any]:
    """Return both evidence trails for one job: transitions and delivery attempts."""
    _require_role(actor_role, READ_ROLES, "read distribution receipts")
    tenant, show = _scope(tenant_id, show_id)
    row = _job(conn, tenant_id=tenant, show_id=show, distribution_id=platform._ref(distribution_id, "distribution_id"))
    return {
        "distribution_id": row["id"],
        "episode_id": row["episode_id"],
        "platform": row["platform"],
        "status": row["status"],
        "transitions": store.fetch_all(
            conn,
            "SELECT * FROM distribution_receipts WHERE tenant_id = ? AND show_id = ? AND distribution_id = ? "
            "ORDER BY created_at, correlation_id",
            (tenant, show, row["id"]),
        ),
        "deliveries": store.fetch_all(
            conn,
            "SELECT * FROM delivery_receipts WHERE tenant_id = ? AND show_id = ? AND distribution_id = ? "
            "ORDER BY attempt",
            (tenant, show, row["id"]),
        ),
    }


def _card(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(row.get("metadata") or {})
    return {
        "distribution_id": row["id"],
        "platform": row["platform"],
        "family": PACKAGE_FAMILIES.get(str(row["platform"]), "rss"),
        "target_platform": row.get("target_platform"),
        "status": row["status"],
        # The package itself is public copy and opaque references, never a
        # private value, so the board can carry it and the UI can re-preview
        # exactly what was drafted without a second round trip.
        "metadata": metadata,
        "title": metadata.get("title"),
        "caption_ref": metadata.get("caption_ref"),
        "authorized": bool(row.get("authorization_receipt_ref")),
        "scheduled_at": row.get("scheduled_at"),
        "published_at": row.get("published_at"),
        "external_id_ref": row.get("external_id_ref"),
        "attempt_count": int(row.get("attempt_count") or 0),
        "last_error_ref": row.get("last_error_ref"),
        "package_checksum": row.get("package_checksum"),
        "completeness": package_completeness(str(row["platform"]), metadata),
        "updated_at": row["updated_at"],
    }


def publication_board(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    actor_role: str,
    episode_id: str | None = None,
    registry: providers.ProviderRegistry | None = None,
) -> dict[str, Any]:
    """Project the Publish view: per episode, per platform cards and blockers."""
    _require_role(actor_role, READ_ROLES, "read distribution jobs")
    tenant, show = _scope(tenant_id, show_id)
    rows = list_distributions(
        conn,
        tenant_id=tenant,
        show_id=show,
        actor_role=actor_role,
        episode_id=episode_id,
        limit=MAX_LIST_LIMIT,
    )
    episodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        episode = str(row["episode_id"])
        entry = episodes.setdefault(
            episode,
            {"episode_id": episode, "platforms": [], "clips": [], "publishable": True, "blockers": []},
        )
        card = _card(row)
        (entry["clips"] if card["family"] == "clip" else entry["platforms"]).append(card)
    for episode, entry in episodes.items():
        entry["platforms"].sort(key=lambda card: (card["platform"], card["distribution_id"]))
        entry["clips"].sort(key=lambda card: (card["target_platform"] or "", card["distribution_id"]))
        blockers = platform.publish_blockers(conn, tenant_id=tenant, show_id=show, episode_id=episode)
        entry["blockers"] = blockers
        entry["publishable"] = not blockers
    board = [episodes[episode] for episode in sorted(episodes)]
    cards = [card for entry in board for card in (*entry["platforms"], *entry["clips"])]
    return {
        "tenant_id": tenant,
        "show_id": show,
        "episodes": board,
        "totals": {
            "episodes": len(board),
            "jobs": len(cards),
            "published": sum(1 for card in cards if card["status"] == "published"),
            "scheduled": sum(1 for card in cards if card["status"] == "scheduled"),
            "failed": sum(1 for card in cards if card["status"] == "failed"),
            "blocked_episodes": sum(1 for entry in board if not entry["publishable"]),
            "clips": sum(len(entry["clips"]) for entry in board),
        },
        "adapters": {name: delivery_adapter(name, registry=registry) for name in sorted(PLATFORMS)},
        "webhook": webhook_contract(show, registry=registry),
    }


__all__ = [
    "AUTHORIZABLE_STATUSES",
    "DELIVERABLE_STATUSES",
    "DELIVERY_MODES",
    "PACKAGE_FAMILIES",
    "PLATFORMS",
    "PUBLISH_ROLES",
    "READ_ROLES",
    "SHORT_FORM_PLATFORMS",
    "STATUSES",
    "WEBHOOK_EVENTS",
    "WRITE_ROLES",
    "DistributionError",
    "DistributionPackage",
    "authorize_publish",
    "clip_preview",
    "create_draft",
    "delivery_adapter",
    "distribution_receipts",
    "list_distributions",
    "mark_published",
    "package_completeness",
    "preview",
    "publication_board",
    "queue_clip",
    "record_adapter_verification",
    "record_failure",
    "rss_preview",
    "schedule_publication",
    "webhook_contract",
    "youtube_preview",
]
