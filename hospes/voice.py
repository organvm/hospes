"""The Voice Constitution validator.

Every outreach draft the system produces must pass this gate. It enforces the
correspondence rules from transcript §6/§B6 by rejecting the classic markers of
generic AI outreach: filler openers, unsupported overpraise, and manufactured
familiarity. The goal (per the show DNA) is *specificity over praise* — a real
editorial proposition, not flattery.

This is a pure, deterministic, stdlib-only check. No network, no models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml

from .paths import CONFIG_DIR


class VoiceConstitutionError(ValueError):
    """Raised when text violates the Voice Constitution."""

    def __init__(self, violations: List["VoiceViolation"]):
        self.violations = violations
        summary = "; ".join(v.message for v in violations)
        super().__init__(f"Voice Constitution violated: {summary}")


@dataclass
class VoiceViolation:
    phrase: str
    message: str


def load_banned_phrases(path: Path | None = None) -> List[str]:
    """Load the tracked voice contract and fail closed on malformed policy."""
    config_path = path or (CONFIG_DIR / "voice.yaml")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    phrases = data.get("banned_phrases") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(phrases, list)
        or not phrases
        or any(not isinstance(item, str) or not item.strip() for item in phrases)
    ):
        raise ValueError("voice config must declare version 1 and banned_phrases")
    normalized = [item.strip().lower() for item in phrases]
    if len(normalized) != len(set(normalized)):
        raise ValueError("voice config contains duplicate banned phrases")
    return normalized


BANNED_PHRASES: List[str] = load_banned_phrases()


def _normalize(text: str) -> str:
    # Collapse runs of whitespace so "hope   this  email" still matches.
    return re.sub(r"\s+", " ", text.lower())


def find_violations(text: str) -> List[VoiceViolation]:
    """Return every Voice Constitution violation found in ``text``."""
    normalized = _normalize(text)
    violations: List[VoiceViolation] = []
    for phrase in BANNED_PHRASES:
        if phrase in normalized:
            violations.append(
                VoiceViolation(
                    phrase=phrase,
                    message=f"banned phrase {phrase!r}",
                )
            )
    return violations


def is_compliant(text: str) -> bool:
    return not find_violations(text)


def validate(text: str) -> str:
    """Return ``text`` unchanged if compliant; otherwise raise.

    ``drafts.py`` routes every generated draft through this function, so a
    banned phrase can never reach an output file.
    """
    violations = find_violations(text)
    if violations:
        raise VoiceConstitutionError(violations)
    return text
