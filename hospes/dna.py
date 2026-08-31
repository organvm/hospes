"""Show DNA loader + validator.

Loads ``dna/*.show.yaml`` files and validates them against the *semantics* of
``dna/show.schema.json`` using stdlib-only checks (no ``jsonschema`` runtime
dependency). We verify the required top-level keys, the required ``show`` and
``format_engine`` sub-keys, non-empty segment lists, and that every declared
segment name is drawn from the known-segment vocabulary the schema documents.

We also expose the recording-city registry consumed by ``routing.py``. Cities
may live under ``show.recording_cities`` (canonical) or top-level
``recording_cities`` (legacy starter-pack shape); both are accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .paths import DNA_DIR

# Required top-level keys. Kept minimal + tolerant: the shipped schema requires
# a wide set, but the engine only *depends* on identity + format engine +
# recording cities. Extra keys are allowed; missing engine-critical keys fail.
REQUIRED_TOP_KEYS = ["show", "format_engine"]
REQUIRED_SHOW_KEYS = ["title", "status", "primary_format"]

# Known segment vocabulary (from show_dna.yaml / schema known_segments). Matching
# is case-insensitive; a segment may be a bare string or a {name, ...} object.
KNOWN_FIXED_SEGMENTS = {"the claim", "the stress test", "the artifact"}
KNOWN_ROTATING_SEGMENTS = {
    "receipts",
    "object lesson",
    "explain it to ari",
    "build the worst version",
    "future headline",
    "opposite chair",
    "the unfinished thing",
    "the question for the next guest",
    "question for the next guest",
}
# Field-show segment logic (Place -> Object -> Intervention -> Artifact).
KNOWN_FIELD_SEGMENTS = {"place", "object", "intervention", "artifact"}


class DnaValidationError(ValueError):
    """Raised when a show DNA file fails structural validation."""


@dataclass
class DnaResult:
    path: str
    show_id: str = ""
    title: str = ""
    recording_cities: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _segment_name(seg: Any) -> str:
    if isinstance(seg, str):
        return seg.strip().lower()
    if isinstance(seg, dict):
        return str(seg.get("name", "")).strip().lower()
    return ""


def load_dna(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise DnaValidationError(f"{path.name}: top-level YAML must be a mapping")
    return data


def _recording_cities(data: Dict[str, Any]) -> List[str]:
    show = data.get("show") or {}
    if isinstance(show, dict) and isinstance(show.get("recording_cities"), list):
        return [str(c) for c in show["recording_cities"]]
    if isinstance(data.get("recording_cities"), list):
        return [str(c) for c in data["recording_cities"]]
    return []


def validate_dna(data: Dict[str, Any], path_name: str = "<dna>") -> List[str]:
    """Return a list of validation error strings ([] means valid)."""
    errors: List[str] = []

    for key in REQUIRED_TOP_KEYS:
        if key not in data:
            errors.append(f"{path_name}: missing required top-level key {key!r}")

    show = data.get("show")
    if not isinstance(show, dict):
        errors.append(f"{path_name}: 'show' must be a mapping")
    else:
        for key in REQUIRED_SHOW_KEYS:
            if not show.get(key):
                errors.append(f"{path_name}: show is missing {key!r}")

    engine = data.get("format_engine")
    if not isinstance(engine, dict):
        errors.append(f"{path_name}: 'format_engine' must be a mapping")
    else:
        fixed = engine.get("fixed")
        rotating = engine.get("rotating")
        if not isinstance(fixed, list) or not fixed:
            errors.append(f"{path_name}: format_engine.fixed must be a non-empty list")
        if not isinstance(rotating, list) or not rotating:
            errors.append(f"{path_name}: format_engine.rotating must be a non-empty list")

        allowed = KNOWN_FIXED_SEGMENTS | KNOWN_ROTATING_SEGMENTS | KNOWN_FIELD_SEGMENTS
        for seg in (fixed or []):
            name = _segment_name(seg)
            if name and name not in allowed:
                errors.append(f"{path_name}: unknown fixed segment {name!r}")
        for seg in (rotating or []):
            name = _segment_name(seg)
            if name and name not in allowed:
                errors.append(f"{path_name}: unknown rotating segment {name!r}")

    return errors


def validate_file(path: Path) -> DnaResult:
    try:
        data = load_dna(path)
    except (DnaValidationError, yaml.YAMLError) as exc:
        return DnaResult(path=str(path), errors=[f"{path.name}: {exc}"])
    errors = validate_dna(data, path.name)
    show = data.get("show") or {}
    return DnaResult(
        path=str(path),
        show_id=str(show.get("id") or path.stem),
        title=str(show.get("title") or ""),
        recording_cities=_recording_cities(data),
        errors=errors,
    )


def discover_dna_files(dna_dir: Optional[Path] = None) -> List[Path]:
    directory = dna_dir or DNA_DIR
    if not directory.exists():
        return []
    return sorted(directory.glob("*.show.yaml"))


def validate_all(dna_dir: Optional[Path] = None) -> List[DnaResult]:
    return [validate_file(p) for p in discover_dna_files(dna_dir)]


def recording_cities(dna_dir: Optional[Path] = None) -> List[str]:
    """Union of recording cities across all valid DNA files, order-preserved."""
    seen: List[str] = []
    for result in validate_all(dna_dir):
        for city in result.recording_cities:
            if city not in seen:
                seen.append(city)
    return seen
