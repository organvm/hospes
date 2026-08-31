"""Research / segment brief generator.

Produces the producer research brief (transcript §12) plus the day-of packet
skeleton (transcript §13) for a candidate, written to ``out/briefs/<slug>.md``.

The brief is anchored on the show's fixed format engine — **Claim / Stress Test
/ Artifact** — plus one *rotating* segment selected deterministically from the
candidate so re-running the demo is stable. It distinguishes verified fact from
guest claim from producer inference, per the transcript's brief contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import audit
from .paths import BRIEFS_DIR

# Rotating segment deck (from show_dna.yaml / flagship DNA). One is rotated in
# per episode; selection is deterministic on the guest name so it is stable.
ROTATING_SEGMENTS = [
    "Receipts",
    "Object Lesson",
    "Explain It to Host",
    "Build the Worst Version",
    "Future Headline",
    "Opposite Chair",
    "The Question for the Next Guest",
]

# §12 Episode Research Producer brief fields.
RESEARCH_BRIEF_FIELDS = [
    "Episode thesis",
    "Why this guest, now",
    "Essential biography",
    "Current work",
    "Three major tensions",
    "Five strong opening paths",
    "Ten primary questions",
    "Five follow-up branches",
    "Contradictions or unresolved claims",
    "Topics to avoid or approach carefully",
    "Relevant quotations",
    "Timeline",
    "Source notes",
    "Relationship history",
    "Promises made to guest",
]

# §13 Day-of Producer packet fields.
DAY_OF_PACKET_FIELDS = [
    "Today's timeline",
    "Guest contact (via producer — never a private number in this file)",
    "Arrival or connection instructions",
    "Host briefing",
    "Pronunciation",
    "Boundaries",
    "Release status",
    "Technical status",
    "Gift or hospitality plan",
    "Emergency escalation path",
]


class BriefError(ValueError):
    pass


@dataclass
class BriefResult:
    guest_name: str
    rotating_segment: str
    path: str


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "unnamed"


def select_rotating_segment(candidate: Dict[str, str]) -> str:
    """Deterministically pick one rotating segment for this candidate."""
    name = (candidate.get("guest_name") or "").strip()
    idx = sum(ord(c) for c in name) % len(ROTATING_SEGMENTS) if name else 0
    return ROTATING_SEGMENTS[idx]


def generate_brief(candidate: Dict[str, str], *, briefs_dir: Optional[Path] = None,
                   log_path: Optional[Path] = None) -> BriefResult:
    guest = (candidate.get("guest_name") or "").strip() or "<unnamed>"
    thesis = (candidate.get("episode_thesis") or "[episode thesis — to be developed]").strip()
    why_now = (candidate.get("why_now") or "[why now]").strip()
    why_guest = (candidate.get("why_guest") or "[editorial case]").strip()
    artifact = (candidate.get("proposed_artifact") or "[artifact the episode will produce]").strip()
    rotating = select_rotating_segment(candidate)

    lines: List[str] = []
    lines.append(f"# Research + Segment Brief — {guest}")
    lines.append("")
    lines.append("_HOSPES producer brief. Distinguish: **verified fact** vs **guest claim** "
                 "vs **producer inference**. No private guest data in this file._")
    lines.append("")

    # Fixed format engine.
    lines.append("## Format engine (fixed)")
    lines.append("")
    lines.append(f"### The Claim")
    lines.append(f"- **Claim:** {thesis}")
    lines.append(f"- Editorial case (why this guest): {why_guest}")
    lines.append("")
    lines.append(f"### The Stress Test")
    lines.append(f"- Designed pressure on the claim: counterexample, comedy, research, or a scenario.")
    lines.append(f"- Host applies instinct/friction; Producer applies structure/synthesis.")
    lines.append("")
    lines.append(f"### The Artifact")
    lines.append(f"- **Artifact to produce:** {artifact}")
    lines.append("")

    # One rotating segment.
    lines.append(f"## Rotating segment (selected): {rotating}")
    lines.append("")
    lines.append(f"- Chosen for {guest}; exposes a real tension and yields a discrete clip.")
    lines.append("")

    # §12 research brief fields.
    lines.append("## Episode research brief (§12)")
    lines.append("")
    for f in RESEARCH_BRIEF_FIELDS:
        if f == "Episode thesis":
            lines.append(f"- **{f}:** {thesis}")
        elif f == "Why this guest, now":
            lines.append(f"- **{f}:** {why_guest} / {why_now}")
        else:
            lines.append(f"- **{f}:** [to fill from research]")
    lines.append("")

    # §13 day-of packet skeleton.
    lines.append("## Day-of packet skeleton (§13)")
    lines.append("")
    for f in DAY_OF_PACKET_FIELDS:
        lines.append(f"- **{f}:** [to fill day-of]")
    lines.append("")

    content = "\n".join(lines)
    directory = briefs_dir or BRIEFS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_slug(guest)}.md"
    path.write_text(content, encoding="utf-8")

    audit.append("brief.generated", guest=guest, rotating_segment=rotating,
                 path=str(path), log_path=log_path)
    return BriefResult(guest_name=guest, rotating_segment=rotating, path=str(path))


def generate_briefs(candidates: List[Dict[str, str]], *, briefs_dir: Optional[Path] = None,
                    log_path: Optional[Path] = None) -> List[BriefResult]:
    return [generate_brief(c, briefs_dir=briefs_dir, log_path=log_path) for c in candidates]
