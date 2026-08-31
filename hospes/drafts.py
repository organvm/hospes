"""Outreach draft generation — the system DRAFTS, it NEVER SENDS.

For each candidate, this module selects one of the three starter-pack outreach
templates by relationship class (per ``spec/permission-matrix.yaml``) and writes
a markdown *draft* to ``out/drafts/<slug>.md``. Every draft is stamped with a
DRAFT-NOT-SENT banner and routed through the Voice Constitution
(:mod:`hospes.voice`) before it is written — a banned phrase can never reach a
file.

Relationship-class routing:

* **C0 / C1**  -> producer-led cold / weak-tie template.
* **C2 / C3**  -> prior-collaborator / producer-with-host-note template.
* **C4 / C5**  -> REFUSE. These are protected relationships. The system emits a
  ``PROTECTED`` notice instead of a draft and takes no automated action.

Claim-evidence provenance
--------------------------
Every personalized or flattering claim in a draft must trace to a real,
verifiable source and must be explicitly approved before it appears in
external-facing material (design rule: claim → source → verified date →
approved for external use; see ``spec/claim_evidence.schema.json``).

A draft may carry an optional ``claims`` list, where each entry must have:

* ``claim`` — the claim text;
* ``source_url`` — a real public URL;
* ``verified_date`` — ISO 8601 date;
* ``approved_for_external_use`` — ``True``.

Use :func:`validate_claims` to check a claims list.  :func:`generate_draft`
calls it automatically and raises :class:`ClaimNotApprovedError` on any
unapproved or incomplete entry.

There is deliberately NO network or send code anywhere in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import audit, voice
from .approvals import is_protected
from .paths import CONFIG_DIR, DRAFTS_DIR, ensure_out_dirs

DRAFT_BANNER = (
    "<!-- HOSPES DRAFT — NOT SENT. The system drafts correspondence; a human "
    "reviews and sends. Sending is a human-gated action by design. -->"
)

@lru_cache(maxsize=1)
def load_template_config(path: Optional[Path] = None) -> dict[str, Any]:
    """Load the tracked outreach contract instead of hiding voice in code."""
    config_path = path or (CONFIG_DIR / "outreach_templates.yaml")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("outreach template config must declare version 1")
    mapping = data.get("class_to_template")
    templates = data.get("templates")
    if not isinstance(mapping, dict) or not isinstance(templates, dict):
        raise ValueError("outreach template config requires mapping and templates")
    required = {"producer_cold", "prior_collaborator", "ari_note"}
    if not required <= set(templates) or any(
        not isinstance(templates[key], str) or not templates[key].strip() for key in required
    ):
        raise ValueError("outreach template config is incomplete")
    if set(mapping) != {"C0", "C1", "C2", "C3"} or any(
        template not in templates for template in mapping.values()
    ):
        raise ValueError("outreach relationship routing is invalid")
    return data


_TEMPLATE_CONFIG = load_template_config()
CLASS_TO_TEMPLATE = dict(_TEMPLATE_CONFIG["class_to_template"])
_TEMPLATES = dict(_TEMPLATE_CONFIG["templates"])

PROTECTED_CLASSES = {"C4", "C5"}


class ProtectedRelationshipError(RuntimeError):
    """Raised when draft generation is attempted for a protected relationship."""


class ClaimNotApprovedError(ValueError):
    """Raised when a draft contains a claim that is not approved for external use.

    Every personalized or flattering claim in outreach material must have a
    verified source and must be explicitly approved (``approved_for_external_use
    == True``) before it may appear in any external-facing text.
    """


@dataclass
class DraftResult:
    guest_name: str
    template: str
    path: Optional[str]  # None when refused
    refused: bool = False
    reason: str = ""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "unnamed"


def _fill(text: str, candidate: Dict[str, str]) -> str:
    """Fill template placeholders from candidate fields. Missing -> neutral."""
    guest = (candidate.get("guest_name") or "the guest").strip()
    thesis = (candidate.get("episode_thesis") or "a specific editorial question").strip()
    why_now = (candidate.get("why_now") or "a timely reason").strip()
    project = (candidate.get("why_guest") or "their recent work").strip()
    window = (candidate.get("date_window") or "a window we can propose").strip()
    artifact = (candidate.get("proposed_artifact") or "a new artifact").strip()
    mapping = {
        "GUEST": guest,
        "NAME": guest,
        "THESIS": thesis,
        "EPISODE TENSION": thesis,
        "PRECISE QUESTION OR TENSION": thesis,
        "ONE SENTENCE": thesis,
        "WHY NOW": why_now,
        "SPECIFIC VERIFIED PROJECT": project,
        "SPECIFIC REASON": why_now,
        "VERIFIED SUBJECT": project,
        "PLACE": (candidate.get("place") or "the proposed setting").strip(),
        "OBJECT": (candidate.get("object") or artifact).strip(),
        "INTERVENTION": (candidate.get("intervention") or thesis).strip(),
        "VERIFIED REASON": why_now,
        "DATE WINDOW": window,
        "DURATION": "75 minutes",
        "OUTPUT CREATED IN EPISODE": artifact,
        "CO-HOST": "Producer",
        "REAL SENDER": "[the show's real producer — a human signs and sends]",
        "ROLE": "Guest Producer",
        "SHOW": "the show",
    }
    out = text
    for key, val in mapping.items():
        out = out.replace(f"[{key}]", val)
    return out


def _relationship_class(candidate: Dict[str, str]) -> str:
    return (candidate.get("relationship_class") or "").strip().upper()


def render_outreach(candidate: Dict[str, str], *, show_id: str | None = None) -> tuple[str, str]:
    """Render a configured template without writing it or sending anything."""
    rel = _relationship_class(candidate)
    if rel in PROTECTED_CLASSES or is_protected(candidate):
        raise ProtectedRelationshipError(
            f"protected relationship class {rel or 'flagged'} cannot be rendered"
        )
    mapping = CLASS_TO_TEMPLATE
    templates = _TEMPLATES
    show_voice: dict[str, Any] | None = None
    if show_id is not None:
        from .configuration import ConfigurationError, load_show, load_show_resource

        try:
            show = load_show(show_id)
        except ConfigurationError as exc:
            # Migrated and synthetic records can predate the configured-show
            # registry. They retain the legacy template until explicitly assigned.
            if "is not registered" not in str(exc):
                raise
            show = None
        if show is not None:
            if show.template_ref is not None:
                configured_templates = load_show_resource(show, "template")
                mapping = dict(configured_templates["class_to_template"])
                templates = dict(configured_templates["templates"])
            if show.voice_ref is not None:
                show_voice = load_show_resource(show, "voice")
    template_key = mapping.get(rel)
    if not isinstance(template_key, str) or template_key not in templates:
        raise ValueError(f"show draft template does not support relationship class {rel!r}")
    body = _fill(str(templates[template_key]), candidate)
    if show_id is None and template_key == "prior_collaborator":
        body = body + "\n\n---\n\n" + _fill(str(templates["ari_note"]), candidate)
    voice.validate(body)
    if show_voice is not None:
        normalized = re.sub(r"\s+", " ", body.lower())
        banned = show_voice.get("banned_phrases", [])
        if not isinstance(banned, list) or any(
            not isinstance(phrase, str) or not phrase.strip() for phrase in banned
        ):
            raise ValueError("show voice resource requires banned_phrases")
        violations = [phrase for phrase in banned if phrase.strip().lower() in normalized]
        if violations:
            raise ValueError(f"show voice resource rejected banned phrases: {violations}")
    return template_key, body


def validate_claims(claims: List[Dict]) -> None:
    """Validate a list of claim-evidence entries.

    Each entry must conform to ``spec/claim_evidence.schema.json``::

        {
            "claim": str,
            "source_url": str,
            "verified_date": str,          # ISO 8601 date
            "approved_for_external_use": True
        }

    Raises:
        ClaimNotApprovedError: If any entry is missing required fields or has
            ``approved_for_external_use`` set to False (or absent).
    """
    required = {"claim", "source_url", "verified_date", "approved_for_external_use"}
    for i, entry in enumerate(claims):
        missing = required - set(entry.keys())
        if missing:
            raise ClaimNotApprovedError(
                f"Claim entry {i} is missing required fields: {sorted(missing)}. "
                "All claims must have claim, source_url, verified_date, and "
                "approved_for_external_use before appearing in external material."
            )
        if not entry.get("approved_for_external_use"):
            raise ClaimNotApprovedError(
                f"Claim entry {i} ('{entry.get('claim', '')[:60]}') is not approved "
                "for external use. Set approved_for_external_use=True after human "
                "review before including this claim in outreach drafts."
            )


def generate_draft(candidate: Dict[str, str], *, drafts_dir: Optional[Path] = None,
                   log_path: Optional[Path] = None) -> DraftResult:
    """Generate one outreach draft (or refuse for a protected relationship)."""
    guest = (candidate.get("guest_name") or "").strip() or "<unnamed>"
    rel = _relationship_class(candidate)

    # Protected: either an explicit protected flag or a C4/C5 class.
    if rel in PROTECTED_CLASSES or is_protected(candidate):
        audit.append(
            "outreach.refused_protected",
            guest=guest,
            relationship_class=rel,
            log_path=log_path,
        )
        return DraftResult(
            guest_name=guest,
            template="none",
            path=None,
            refused=True,
            reason=(
                f"PROTECTED relationship (class {rel or 'flagged'}). "
                "No automated draft. The host decides and handles this contact directly."
            ),
        )

    # Validate claim-evidence provenance BEFORE generating draft content.
    # Any unapproved or incomplete claim raises ClaimNotApprovedError and
    # prevents the draft from being written.
    claims = candidate.get("claims") or []
    if isinstance(claims, list):
        validate_claims(claims)

    template_key, body = render_outreach(candidate)

    content = (
        f"{DRAFT_BANNER}\n\n"
        f"# Outreach draft — {guest}\n\n"
        f"- Relationship class: {rel or 'unspecified'}\n"
        f"- Template: {template_key}\n"
        f"- Status: DRAFT (not sent — a human sends)\n\n"
        f"---\n\n{body}"
    )

    directory = drafts_dir or DRAFTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_slug(guest)}.md"
    path.write_text(content, encoding="utf-8")

    audit.append(
        "outreach.draft_generated",
        guest=guest,
        relationship_class=rel,
        template=template_key,
        path=str(path),
        log_path=log_path,
    )
    return DraftResult(guest_name=guest, template=template_key, path=str(path))


def generate_drafts(candidates: List[Dict[str, str]], *, drafts_dir: Optional[Path] = None,
                    log_path: Optional[Path] = None) -> List[DraftResult]:
    if drafts_dir is None:
        ensure_out_dirs()
    return [generate_draft(c, drafts_dir=drafts_dir, log_path=log_path) for c in candidates]
