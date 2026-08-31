"""Demo persona and guide registry.

``hospes/resources/dashboard/assets/guide.json`` is the single declaration of
the demonstration's personas, capability glossary, element tooltips, and guided
tour beats.  The browser fetches it as a static asset; this module reads the
same file so the CLI cannot drift from what the dashboard explains.

Nothing here performs I/O beyond reading that one packaged registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from .paths import DASHBOARD_DIR

#: Resolved through the same helper the operator surface serves from, so the
#: CLI and the browser always read one file.  In a source checkout that is the
#: repo-root ``dashboard/``; from an installed wheel it is the packaged overlay.
GUIDE_PATH = DASHBOARD_DIR / "assets" / "guide.json"

#: Roles the operator surface accepts.  Mirrors ``capabilities.mjs``.
_KNOWN_ROLES = frozenset({"host", "producer", "editorial_owner", "relationship_owner"})


class DemoGuideError(ValueError):
    """The guide registry is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class DemoPersona:
    """One named audience for the synthetic demonstration.

    Every persona carries full ``relationship_owner`` authority on purpose: the
    demonstration exists to show the human gates working, and a viewer who
    cannot reach a gate cannot evaluate it.  Personas differ in narration depth
    and in the identity attributed to their decisions, not in what they may do.
    """

    key: str
    label: str
    actor: str
    role: str
    depth: str
    headline: str
    opening: str

    @classmethod
    def from_mapping(cls, key: str, raw: Mapping[str, Any]) -> "DemoPersona":
        missing = [
            field
            for field in ("label", "actor", "role", "depth", "headline", "opening")
            if not str(raw.get(field, "")).strip()
        ]
        if missing:
            raise DemoGuideError(f"persona {key!r} is missing required fields: {', '.join(sorted(missing))}")
        role = str(raw["role"])
        if role not in _KNOWN_ROLES:
            raise DemoGuideError(
                f"persona {key!r} declares unknown role {role!r}; expected one of {', '.join(sorted(_KNOWN_ROLES))}"
            )
        return cls(
            key=key,
            label=str(raw["label"]),
            actor=str(raw["actor"]),
            role=role,
            depth=str(raw["depth"]),
            headline=str(raw["headline"]),
            opening=str(raw["opening"]),
        )


@lru_cache(maxsize=1)
def load_guide() -> Mapping[str, Any]:
    """Return the parsed guide registry."""
    try:
        document = json.loads(GUIDE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - packaging boundary
        raise DemoGuideError(f"guide registry is missing: {GUIDE_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise DemoGuideError(f"guide registry is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise DemoGuideError("guide registry must be a JSON object")
    for section in ("personas", "capabilities", "elements", "beats", "depths"):
        if section not in document:
            raise DemoGuideError(f"guide registry is missing the {section!r} section")
    return document


@lru_cache(maxsize=1)
def personas() -> Mapping[str, DemoPersona]:
    """Return every declared persona, keyed by its CLI name."""
    raw = load_guide()["personas"]
    if not isinstance(raw, Mapping) or not raw:
        raise DemoGuideError("guide registry declares no personas")
    declared_depths = set(load_guide()["depths"])
    resolved: dict[str, DemoPersona] = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            raise DemoGuideError(f"persona {key!r} must be an object")
        persona = DemoPersona.from_mapping(str(key), value)
        if persona.depth not in declared_depths:
            raise DemoGuideError(f"persona {key!r} uses undeclared narration depth {persona.depth!r}")
        resolved[persona.key] = persona
    return resolved


def persona_names() -> tuple[str, ...]:
    """Return persona keys in declaration order, for CLI choices and help."""
    return tuple(personas())


def default_persona() -> str:
    """Return the persona assumed when a caller names none."""
    return persona_names()[0]


def resolve_persona(name: str | None) -> DemoPersona:
    """Return the named persona, or the default when ``name`` is empty."""
    key = str(name or default_persona())
    try:
        return personas()[key]
    except KeyError as exc:
        raise DemoGuideError(f"unknown persona {key!r}; expected one of {', '.join(persona_names())}") from exc
