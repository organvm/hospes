"""Studio routing.

Given a candidate, choose a recording studio (LA / NYC / AUSTIN) and explain
why. The studios registry is derived from the recording cities declared across
the show DNA files (``dna/*.show.yaml`` -> ``recording_cities``); the three
canonical HOSPES studio cities are Los Angeles, New York City, and Austin.

Routing inputs:

* ``preferred_city`` — the guest's stated/home city (mapped to the nearest
  studio city).
* ``date_window``    — carried into the rationale so batching can group by trip.

Batching: guests routed to the same city are grouped so a single trip / studio
setup can capture several recordings (touring-guest interception).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import dna

# Canonical studio codes.
LA = "LA"
NYC = "NYC"
AUSTIN = "AUSTIN"
REMOTE = "REMOTE"

# City-name / synonym -> studio code. Lower-cased keys.
_CITY_TO_STUDIO: Dict[str, str] = {
    "la": LA,
    "l.a.": LA,
    "los angeles": LA,
    "losangeles": LA,
    "melrose": LA,
    "hollywood": LA,
    "nyc": NYC,
    "ny": NYC,
    "new york": NYC,
    "new york city": NYC,
    "manhattan": NYC,
    "brooklyn": NYC,
    "austin": AUSTIN,
    "atx": AUSTIN,
}

# Studio code -> the canonical display city.
_STUDIO_CITY = {
    LA: "Los Angeles",
    NYC: "New York City",
    AUSTIN: "Austin",
}


@dataclass
class RouteDecision:
    guest_name: str
    studio: str
    city: str
    rationale: str
    date_window: str = ""
    matched: bool = True


def _normalize_city(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def studios_registry(dna_dir: Optional[Path] = None) -> Dict[str, str]:
    """Return the active studio registry (code -> display city).

    Derived from the DNA recording cities. Any city that maps to LA/NYC/AUSTIN
    activates that studio. If no DNA cities are present the three canonical
    studios are still available (they are the standing HOSPES studios).
    """
    registry: Dict[str, str] = {}
    for city in dna.recording_cities(dna_dir):
        code = _CITY_TO_STUDIO.get(_normalize_city(city))
        if code and code not in registry:
            registry[code] = _STUDIO_CITY[code]
    # Fall back to all three canonical studios if DNA declared none.
    if not registry:
        registry = dict(_STUDIO_CITY)
    return registry


def route_to_studio(candidate: Dict[str, str], dna_dir: Optional[Path] = None) -> RouteDecision:
    """Route a single candidate to a studio with a rationale."""
    registry = studios_registry(dna_dir)
    guest = (candidate.get("guest_name") or "").strip() or "<unnamed>"
    preferred = candidate.get("preferred_city") or candidate.get("city") or ""
    date_window = (candidate.get("date_window") or "").strip()

    code = _CITY_TO_STUDIO.get(_normalize_city(preferred))
    if code and code in registry:
        city = registry[code]
        window_note = f" within window {date_window!r}" if date_window else ""
        return RouteDecision(
            guest_name=guest,
            studio=code,
            city=city,
            rationale=(
                f"Guest city {preferred!r} maps to the {city} studio ({code}); "
                f"recording there minimizes guest travel{window_note}."
            ),
            date_window=date_window,
            matched=True,
        )

    # No studio-city match -> remote/nearest fallback. Pick the first active
    # studio deterministically so batching still has a bucket.
    fallback_code = next(iter(registry))
    fallback_city = registry[fallback_code]
    return RouteDecision(
        guest_name=guest,
        studio=REMOTE,
        city=preferred.strip() or "unknown",
        rationale=(
            f"Guest city {preferred!r} is not a HOSPES studio city; "
            f"route as REMOTE or intercept during a {fallback_city} trip."
        ),
        date_window=date_window,
        matched=False,
    )


def route_all(candidates: List[Dict[str, str]], dna_dir: Optional[Path] = None) -> List[RouteDecision]:
    return [route_to_studio(c, dna_dir) for c in candidates]


def batching_suggestion(decisions: List[RouteDecision]) -> Dict[str, List[str]]:
    """Group guest names by studio so same-city recordings can share a trip."""
    groups: Dict[str, List[str]] = {}
    for d in decisions:
        groups.setdefault(d.studio, []).append(d.guest_name)
    return groups
