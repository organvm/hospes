"""Typed, synthetic-safe source records for guest packet exports."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from urllib.parse import urlparse

from . import privacy
from .paths import DATA_DIR

PACKET_SOURCE_NAME = "packet-sources.json"
DEFAULT_MANIFEST = DATA_DIR / "pilot-packet-sources.json"
PILOT_IDS = frozenset({"pilot-a", "pilot-b", "pilot-c"})
PROTECTED_CLASSES = frozenset({"C4", "C5"})
PACKET_SOURCE_REQUIRED_FIELDS = frozenset(
    {
        "pilot_id",
        "guest_name",
        "city",
        "studio",
        "recording_window",
        "relationship_class",
        "host_bio",
        "episode_thesis",
        "invitation_note",
        "asset_items",
        "claims",
    }
)
PACKET_CLAIM_REQUIRED_FIELDS = frozenset(
    {"claim", "source_url", "verified_date", "approved_for_external_use"}
)
PRIVATE_FIELDS = frozenset(
    {
        "email",
        "phone",
        "contact",
        "contact_route",
        "private_notes",
        "correspondence",
        "address",
    }
)
_PLACEHOLDER = re.compile(
    r"(?:\b(?:todo|tbd|placeholder)\b|\[(?!synthetic\])[^]]*(?:fill|todo|tbd)[^]]*\])",
    re.IGNORECASE,
)


def _allowed_pilot_ids() -> str:
    return ", ".join(sorted(PILOT_IDS))


class PacketClaim(TypedDict):
    claim: str
    source_url: str
    verified_date: str
    approved_for_external_use: bool


class PacketSource(TypedDict):
    pilot_id: str
    guest_name: str
    city: str
    studio: str
    recording_window: str
    relationship_class: str
    host_bio: str
    episode_thesis: str
    invitation_note: str
    asset_items: list[str]
    claims: list[PacketClaim]
    protected: NotRequired[bool]


class PacketSourceError(ValueError):
    """Raised when packet input is incomplete, unsafe, or unapproved."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PacketSourceError(f"packet source file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PacketSourceError(f"packet source file is invalid JSON: {path}") from exc


def _walk_private_fields(value: Any, path: str = "source") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in PRIVATE_FIELDS and item not in (None, "", [], {}):
                raise PacketSourceError(f"private field is forbidden: {path}.{key}")
            _walk_private_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_private_fields(item, f"{path}[{index}]")


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PacketSourceError(f"{field_name} is required")
    text = value.strip()
    if _PLACEHOLDER.search(text):
        raise PacketSourceError(f"{field_name} contains a placeholder")
    if privacy.contact_kind(text):
        raise PacketSourceError(f"{field_name} contains private contact data")
    if privacy.private_text_kind(text):
        raise PacketSourceError(f"{field_name} contains private content")
    return text


def validate_packet_source(pilot_id: str, value: Any) -> PacketSource:
    """Validate and normalize one packet source without mutating it."""
    if pilot_id not in PILOT_IDS:
        raise PacketSourceError(f"unknown pilot id: {pilot_id}")
    if not isinstance(value, dict):
        raise PacketSourceError(f"packet source for {pilot_id} must be an object")
    _walk_private_fields(value)
    missing = sorted(PACKET_SOURCE_REQUIRED_FIELDS - set(value))
    if missing:
        raise PacketSourceError(f"{pilot_id} is missing fields: {', '.join(missing)}")
    if value.get("pilot_id") != pilot_id:
        raise PacketSourceError(f"{pilot_id} source carries a mismatched pilot_id")
    relationship_class = _text(
        value.get("relationship_class"), "relationship_class"
    ).upper()
    if "protected" in value and not isinstance(value["protected"], bool):
        raise PacketSourceError(f"{pilot_id} protected marker must be boolean")
    if relationship_class in PROTECTED_CLASSES or value.get("protected") is True:
        raise PacketSourceError(f"{pilot_id} is protected and cannot be exported")
    if relationship_class not in {"C0", "C1", "C2", "C3"}:
        raise PacketSourceError(f"{pilot_id} has an invalid relationship class")
    assets = value.get("asset_items")
    if not isinstance(assets, list) or not assets:
        raise PacketSourceError(f"{pilot_id} requires at least one asset item")
    normalized_assets = [_text(item, "asset_items") for item in assets]
    claims = value.get("claims")
    if not isinstance(claims, list) or not claims:
        raise PacketSourceError(f"{pilot_id} requires at least one cited claim")
    normalized_claims: list[PacketClaim] = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise PacketSourceError(f"{pilot_id} claim {index} must be an object")
        claim_text = _text(claim.get("claim"), f"claims[{index}].claim")
        source_url = _text(claim.get("source_url"), f"claims[{index}].source_url")
        parsed = urlparse(source_url)
        if parsed.scheme not in {"fixture", "https"} or not parsed.netloc:
            raise PacketSourceError(
                f"{pilot_id} claim {index} has an invalid source URL"
            )
        verified = _text(claim.get("verified_date"), f"claims[{index}].verified_date")
        try:
            date.fromisoformat(verified)
        except ValueError as exc:
            raise PacketSourceError(
                f"{pilot_id} claim {index} has an invalid verified date"
            ) from exc
        if claim.get("approved_for_external_use") is not True:
            raise PacketSourceError(f"{pilot_id} claim {index} is not approved")
        normalized_claims.append(
            {
                "claim": claim_text,
                "source_url": source_url,
                "verified_date": verified,
                "approved_for_external_use": True,
            }
        )
    cited_claims = {claim["claim"] for claim in normalized_claims}
    host_bio = _text(value.get("host_bio"), "host_bio")
    if host_bio not in cited_claims:
        raise PacketSourceError(f"{pilot_id} host bio is not backed by a cited claim")
    episode_thesis = _text(value.get("episode_thesis"), "episode_thesis")
    if episode_thesis not in cited_claims:
        raise PacketSourceError(
            f"{pilot_id} episode thesis is not backed by a cited claim"
        )
    return {
        "pilot_id": pilot_id,
        "guest_name": _text(value.get("guest_name"), "guest_name"),
        "city": _text(value.get("city"), "city"),
        "studio": _text(value.get("studio"), "studio"),
        "recording_window": _text(value.get("recording_window"), "recording_window"),
        "relationship_class": relationship_class,
        "host_bio": host_bio,
        "episode_thesis": episode_thesis,
        "invitation_note": _text(value.get("invitation_note"), "invitation_note"),
        "asset_items": normalized_assets,
        "claims": normalized_claims,
    }


def load_packet_sources(source_dir: str | Path) -> dict[str, PacketSource]:
    """Load a generated packet-source document from ``source_dir``."""
    path = Path(source_dir) / PACKET_SOURCE_NAME
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("version") != 1:
        raise PacketSourceError("packet source document must declare version 1")
    pilots = document.get("pilots")
    if not isinstance(pilots, dict):
        raise PacketSourceError("packet source document requires a pilots object")
    if set(pilots) != PILOT_IDS:
        raise PacketSourceError(
            f"packet source document must contain exactly {_allowed_pilot_ids()}"
        )
    return {
        pilot_id: validate_packet_source(pilot_id, pilots.get(pilot_id))
        for pilot_id in sorted(PILOT_IDS)
    }


def write_packet_sources(
    out_dir: str | Path,
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> Path:
    """Validate the tracked manifest and atomically materialize demo sources."""
    manifest = _read_json(Path(manifest_path))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise PacketSourceError("packet source manifest must declare version 1")
    pilots = manifest.get("pilots")
    if not isinstance(pilots, dict) or set(pilots) != PILOT_IDS:
        raise PacketSourceError(
            f"packet source manifest must contain exactly {_allowed_pilot_ids()}"
        )
    normalized = {
        pilot_id: validate_packet_source(pilot_id, pilots[pilot_id])
        for pilot_id in sorted(PILOT_IDS)
    }
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / PACKET_SOURCE_NAME
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_dir,
            prefix=f".{PACKET_SOURCE_NAME}.",
            delete=False,
        ) as handle:
            json.dump(
                {"version": 1, "pilots": normalized},
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, target)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "DEFAULT_MANIFEST",
    "PACKET_CLAIM_REQUIRED_FIELDS",
    "PACKET_SOURCE_NAME",
    "PACKET_SOURCE_REQUIRED_FIELDS",
    "PILOT_IDS",
    "PacketClaim",
    "PacketSource",
    "PacketSourceError",
    "load_packet_sources",
    "validate_packet_source",
    "write_packet_sources",
]
