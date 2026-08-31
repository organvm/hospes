"""Public-provenance guest suggestions for editorial review.

The suggestion engine reads a public episode archive and optional public tour
feeds, then emits an import-compatible CSV.  It never discovers or persists a
contact value.  Its route is only a routing hypothesis and is therefore marked
unusable until an attributable operator verifies it in the private system.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO
from urllib.parse import urlsplit, urlunsplit

from . import privacy
from .paths import DATA_DIR


SUGGESTION_FORMAT = "hospes-suggestions-v1"
DEFAULT_ARCHIVE = DATA_DIR / "unlicensed-therapy" / "archive.json"
RELATIONSHIP_CLASSES = frozenset({"C1", "C2", "C3"})
ROUTE_LABELS = {
    "C1": "Ari direct",
    "C2": "Anthony warm intro",
    "C3": "Producer cold",
}
ROUTE_TYPES = {
    "C1": "ari_direct",
    "C2": "anthony_warm_intro",
    "C3": "producer_cold",
}
DISPLAY_FIELDS = (
    "source_key",
    "name",
    "relationship_class",
    "last_appearance",
    "episodes_since",
    "estimated_social_cost",
    "suggested_route",
    "notes",
)
IMPORT_FIELDS = (
    "guest_name",
    "why_guest",
    "why_now",
    "episode_thesis",
    "proposed_artifact",
    "relationship_owner",
    "route_type",
    "route_reference",
    "route_verified_at",
    "route_usable",
    "preferred_city",
    "social_cost_1_5",
    "ari_effort",
    "next_action",
    "source_provenance",
    "category",
    "next_action_date",
    "suggestion_format",
)
CSV_FIELDS = DISPLAY_FIELDS + IMPORT_FIELDS

_ARCHIVE_KEYS = frozenset(
    {
        "episode_no",
        "title",
        "date",
        "duration",
        "type",
        "guest",
        "guest_raw",
        "confidence",
        "description_head",
        "notes",
        "relationship_class",
    }
)
_TOUR_KEYS = frozenset(
    {
        "guest",
        "name",
        "date",
        "city",
        "title",
        "venue",
        "source",
        "source_url",
    }
)
_PRIVATE_KEY_PARTS = (
    "address",
    "email",
    "phone",
    "contact",
    "message",
    "correspondence",
    "private",
    "token",
    "secret",
)
_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")
_SLUG = re.compile(r"[^a-z0-9]+")
_BOUNDARY_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
_FORMULA_PREFIX = frozenset({"=", "+", "-", "@"})


class SuggestionError(ValueError):
    """Raised when public suggestion input violates the bounded contract."""


@dataclass(frozen=True)
class ArchiveAppearance:
    guest_name: str
    appeared_on: date
    episode_key: str
    title: str
    role: str
    relationship_class: str


@dataclass(frozen=True)
class TourEvent:
    guest_name: str
    event_date: date
    city: str
    source: str


@dataclass(frozen=True)
class GuestSuggestion:
    """One public-provenance editorial suggestion."""

    source_key: str
    name: str
    relationship_class: str
    last_appearance: date
    episodes_since: int
    estimated_social_cost: int
    suggested_route: str
    notes: str
    why_guest: str
    why_now: str
    episode_thesis: str
    proposed_artifact: str
    relationship_owner: str
    route_type: str
    route_reference: str
    preferred_city: str
    source_provenance: str

    def as_csv_row(self) -> dict[str, str]:
        """Return the versioned display-plus-import row."""
        appeared_at = datetime.combine(
            self.last_appearance,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).isoformat()
        return {
            "source_key": self.source_key,
            "name": self.name,
            "relationship_class": self.relationship_class,
            "last_appearance": self.last_appearance.isoformat(),
            "episodes_since": str(self.episodes_since),
            "estimated_social_cost": str(self.estimated_social_cost),
            "suggested_route": self.suggested_route,
            "notes": self.notes,
            "guest_name": self.name,
            "why_guest": self.why_guest,
            "why_now": self.why_now,
            "episode_thesis": self.episode_thesis,
            "proposed_artifact": self.proposed_artifact,
            "relationship_owner": self.relationship_owner,
            "route_type": self.route_type,
            "route_reference": self.route_reference,
            "route_verified_at": appeared_at,
            "route_usable": "false",
            "preferred_city": self.preferred_city,
            "social_cost_1_5": str(self.estimated_social_cost),
            "ari_effort": "review_only",
            "next_action": (
                "Review the public provenance and verify the suggested route "
                "before any outreach."
            ),
            "source_provenance": self.source_provenance,
            "category": "archive_alumni",
            "next_action_date": "",
            "suggestion_format": SUGGESTION_FORMAT,
        }


def _clean_public_text(value: Any, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise SuggestionError(f"{field} must be text")
    text = unicodedata.normalize("NFKC", html.unescape(_TAG.sub(" ", value)))
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        raise SuggestionError(f"{field} is required")
    if len(text) > maximum:
        raise SuggestionError(f"{field} exceeds {maximum} characters")
    if text[0] in _FORMULA_PREFIX:
        raise SuggestionError(f"{field} begins with an unsafe spreadsheet prefix")
    private_kind = privacy.private_text_kind(text)
    if private_kind:
        raise SuggestionError(f"{field} contains {private_kind}")
    return text


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise SuggestionError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise SuggestionError(f"{field} must be an ISO date") from exc
    return parsed


def _validate_keys(
    row: Mapping[str, Any], allowed: frozenset[str], *, source: str, row_index: int
) -> None:
    keys = {str(key) for key in row}
    forbidden = sorted(
        key
        for key in keys
        if any(part in key.casefold() for part in _PRIVATE_KEY_PARTS)
    )
    if forbidden:
        raise SuggestionError(
            f"{source} row {row_index} contains private-field names: "
            + ", ".join(forbidden)
        )
    unknown = sorted(keys - allowed)
    if unknown:
        raise SuggestionError(
            f"{source} row {row_index} contains unsupported fields: "
            + ", ".join(unknown)
        )


def _read_json_rows(path: Path, *, source: str) -> list[Mapping[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SuggestionError(f"cannot read {source} JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise SuggestionError(f"{source} JSON must contain a list of rows")
    if not all(isinstance(row, Mapping) for row in payload):
        raise SuggestionError(f"{source} JSON rows must be objects")
    return list(payload)


def load_archive(path: str | Path = DEFAULT_ARCHIVE) -> list[ArchiveAppearance]:
    """Load the bounded public episode-archive contract."""
    selected = Path(path)
    rows = _read_json_rows(selected, source="archive")
    appearances: list[ArchiveAppearance] = []
    for index, row in enumerate(rows, start=1):
        _validate_keys(row, _ARCHIVE_KEYS, source="archive", row_index=index)
        relationship_class = str(row.get("relationship_class") or "C2").upper()
        if relationship_class not in RELATIONSHIP_CLASSES:
            raise SuggestionError(
                f"archive row {index} relationship_class must be C1, C2, or C3"
            )
        title = _clean_public_text(
            row.get("title"), f"archive row {index} title", maximum=300
        )
        episode_no = row.get("episode_no")
        if episode_no is not None and not isinstance(episode_no, (int, str)):
            raise SuggestionError(
                f"archive row {index} episode_no must be text, an integer, or null"
            )
        episode_identity = str(episode_no).strip() if episode_no is not None else ""
        if len(episode_identity) > 40:
            raise SuggestionError(
                f"archive row {index} episode_no exceeds 40 characters"
            )
        appearances.append(
            ArchiveAppearance(
                guest_name=_clean_public_text(
                    row.get("guest"), f"archive row {index} guest", maximum=160
                ),
                appeared_on=_parse_date(
                    row.get("date"), f"archive row {index} date"
                ),
                episode_key=(
                    f"number:{episode_identity}"
                    if episode_identity
                    else f"title:{title.casefold()}"
                ),
                title=title,
                role=_clean_public_text(
                    row.get("type"), f"archive row {index} role", maximum=40
                ).casefold(),
                relationship_class=relationship_class,
            )
        )
    if not appearances:
        raise SuggestionError("archive must contain at least one appearance")
    return appearances


def _read_csv_rows(path: Path, *, source: str) -> list[Mapping[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise SuggestionError(f"{source} CSV must contain a header row")
            fields = [field.strip() for field in reader.fieldnames]
            if any(not field for field in fields) or len(fields) != len(set(fields)):
                raise SuggestionError(
                    f"{source} CSV headers must be non-empty and unique"
                )
            reader.fieldnames = fields
            rows: list[Mapping[str, Any]] = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise SuggestionError(f"cannot read {source} CSV: {exc}") from exc
    if any(None in row for row in rows):
        raise SuggestionError(f"{source} CSV rows cannot contain extra columns")
    return rows


def _public_source_url(value: Any, field: str) -> str:
    text = _clean_public_text(value, field, maximum=500)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SuggestionError(
            f"{field} must be a public HTTPS URL without credentials, query, or fragment"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def load_tour_events(path: str | Path) -> list[TourEvent]:
    """Load one allowlisted public JSON or CSV tour source."""
    selected = Path(path)
    suffix = selected.suffix.casefold()
    if suffix == ".json":
        rows = _read_json_rows(selected, source="tour")
    elif suffix == ".csv":
        rows = _read_csv_rows(selected, source="tour")
    else:
        raise SuggestionError("tour sources must be JSON or CSV")

    events: list[TourEvent] = []
    for index, row in enumerate(rows, start=1):
        _validate_keys(row, _TOUR_KEYS, source="tour", row_index=index)
        for optional_field in ("title", "venue"):
            if str(row.get(optional_field) or "").strip():
                _clean_public_text(
                    row.get(optional_field),
                    f"tour row {index} {optional_field}",
                    maximum=200,
                )
        source_label = str(row.get("source") or "").strip()
        source_url = str(row.get("source_url") or "").strip()
        if not source_label and not source_url:
            raise SuggestionError(
                f"tour row {index} requires source or source_url provenance"
            )
        provenance_parts: list[str] = []
        if source_label:
            provenance_parts.append(
                _clean_public_text(
                    source_label, f"tour row {index} source", maximum=160
                )
            )
        if source_url:
            provenance_parts.append(
                _public_source_url(source_url, f"tour row {index} source_url")
            )
        events.append(
            TourEvent(
                guest_name=_clean_public_text(
                    row.get("guest") or row.get("name"),
                    f"tour row {index} guest",
                    maximum=160,
                ),
                event_date=_parse_date(
                    row.get("date"), f"tour row {index} date"
                ),
                city=_clean_public_text(
                    row.get("city"), f"tour row {index} city", maximum=120
                ),
                source="; ".join(provenance_parts),
            )
        )
    return events


def _identity_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def _source_key(name: str) -> tuple[str, str]:
    identity = _identity_key(name)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    ascii_name = (
        unicodedata.normalize("NFKD", identity)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = _SLUG.sub("-", ascii_name).strip("-")[:80] or "guest"
    return f"unlicensed-therapy:guest:{slug}:{digest}", digest


def _completed_years(start: date, end: date) -> int:
    years = end.year - start.year
    if (end.month, end.day) < (start.month, start.day):
        years -= 1
    return years


def estimate_social_cost(relationship_class: str, gap_years: int) -> int:
    """Estimate bounded social cost; recent relationships cost more."""
    normalized = relationship_class.upper()
    if normalized not in RELATIONSHIP_CLASSES:
        raise SuggestionError("relationship_class must be C1, C2, or C3")
    if gap_years < 0:
        raise SuggestionError("gap_years cannot be negative")
    if normalized == "C1":
        return 2 if gap_years < 3 else 1
    if normalized == "C2":
        return 3 if gap_years < 4 else 2
    return 4 if gap_years < 4 else 3


def _latest_tour(
    events: Iterable[TourEvent], *, as_of: date
) -> dict[str, TourEvent]:
    selected: dict[str, TourEvent] = {}
    for event in events:
        if event.event_date < as_of:
            continue
        key = _identity_key(event.guest_name)
        existing = selected.get(key)
        if existing is None or event.event_date < existing.event_date:
            selected[key] = event
    return selected


def suggest_guests(
    *,
    archive_path: str | Path = DEFAULT_ARCHIVE,
    tour_paths: Iterable[str | Path] = (),
    as_of: date | None = None,
    min_gap_years: int = 2,
    max_social_cost: int = 3,
    relationship_class: str = "C2",
    limit: int = 20,
    relationship_owner: str = "ari_owner",
    preferred_city: str = "Los Angeles",
) -> list[GuestSuggestion]:
    """Build deterministic, import-safe suggestions from public sources."""
    effective_date = as_of or datetime.now(timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    if effective_date > today:
        raise SuggestionError("as_of cannot be in the future")
    if not 0 <= min_gap_years <= 100:
        raise SuggestionError("min_gap_years must be between 0 and 100")
    if not 1 <= max_social_cost <= 5:
        raise SuggestionError("max_social_cost must be between 1 and 5")
    if not 1 <= limit <= 500:
        raise SuggestionError("limit must be between 1 and 500")
    selected_class = relationship_class.upper()
    if selected_class not in RELATIONSHIP_CLASSES:
        raise SuggestionError("relationship_class must be C1, C2, or C3")
    if not _BOUNDARY_ID.fullmatch(relationship_owner):
        raise SuggestionError("relationship_owner must be an opaque lower-case id")
    city = _clean_public_text(preferred_city, "preferred_city", maximum=120)

    appearances = [
        item
        for item in load_archive(archive_path)
        if item.appeared_on <= effective_date
    ]
    all_episodes = {
        (item.appeared_on, item.episode_key) for item in appearances
    }
    by_guest: dict[str, list[ArchiveAppearance]] = {}
    for appearance in appearances:
        by_guest.setdefault(_identity_key(appearance.guest_name), []).append(appearance)

    tour_events: list[TourEvent] = []
    for path in tour_paths:
        tour_events.extend(load_tour_events(path))
    tours = _latest_tour(tour_events, as_of=effective_date)

    suggestions: list[GuestSuggestion] = []
    for identity, guest_appearances in by_guest.items():
        classes = {item.relationship_class for item in guest_appearances}
        if len(classes) != 1:
            raise SuggestionError(
                "archive contains conflicting relationship classes for one guest"
            )
        archive_class = next(iter(classes))
        if archive_class != selected_class:
            continue
        latest = max(
            guest_appearances,
            key=lambda item: (item.appeared_on, item.title.casefold()),
        )
        gap_years = _completed_years(latest.appeared_on, effective_date)
        if gap_years < min_gap_years:
            continue
        social_cost = estimate_social_cost(archive_class, gap_years)
        if social_cost > max_social_cost:
            continue
        episodes_since = sum(
            episode_date > latest.appeared_on
            for episode_date, _episode_key in all_episodes
        )
        source_key, digest = _source_key(latest.guest_name)
        tour = tours.get(identity)
        if tour is None:
            why_now = (
                f"The public archive shows a completed {gap_years}-year gap since "
                "the most recent appearance."
            )
            source_extra = ""
            suggestion_city = city
        else:
            why_now = (
                f"A public tour listing places {latest.guest_name} in {tour.city} "
                f"on {tour.event_date.isoformat()}; review timing privately."
            )
            source_extra = (
                f" Public tour provenance: {tour.source}; event "
                f"{tour.event_date.isoformat()} in {tour.city}."
            )
            suggestion_city = tour.city
        provenance = (
            "Unlicensed Therapy public archive; "
            f"{latest.appeared_on.isoformat()}; role {latest.role}; "
            f"episode title {latest.title}; {len(guest_appearances)} appearance(s)."
            f"{source_extra} Opaque suggestion reference "
            f"suggestion://unlicensed-therapy/{digest}."
        )
        suggestions.append(
            GuestSuggestion(
                source_key=source_key,
                name=latest.guest_name,
                relationship_class=archive_class,
                last_appearance=latest.appeared_on,
                episodes_since=episodes_since,
                estimated_social_cost=social_cost,
                suggested_route=ROUTE_LABELS[archive_class],
                notes=(
                    f"Last appeared on {latest.appeared_on.isoformat()} in "
                    f"{latest.title} as {latest.role}; {episodes_since} archive "
                    "episode(s) followed."
                ),
                why_guest=(
                    "Unlicensed Therapy archive alumnus with "
                    f"{len(guest_appearances)} documented appearance(s)."
                ),
                why_now=why_now,
                episode_thesis=(
                    f"Revisit {latest.guest_name}'s work since {latest.title} and "
                    "identify what has materially changed."
                ),
                proposed_artifact="A before-and-now conversation map",
                relationship_owner=relationship_owner,
                route_type=ROUTE_TYPES[archive_class],
                route_reference=f"suggestion://unlicensed-therapy/{digest}",
                preferred_city=suggestion_city,
                source_provenance=provenance,
            )
        )

    suggestions.sort(
        key=lambda item: (
            item.estimated_social_cost,
            -item.last_appearance.toordinal(),
            item.name.casefold(),
        )
    )
    return suggestions[:limit]


def write_csv(suggestions: Iterable[GuestSuggestion], stream: TextIO) -> None:
    """Write suggestions to ``stream`` without diagnostic output."""
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for suggestion in suggestions:
        writer.writerow(suggestion.as_csv_row())
