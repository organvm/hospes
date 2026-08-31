"""Recording asset-package checklist generator.

For each recording, HOSPES defines a predefined asset package (ASK A: "the
recording produces a predefined asset package"). This module writes the
checklist to ``out/assets/<slug>.md`` so production can track delivery.

The package is derived from the show's asset requirements and the transcript's
post-production deliverables list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import audit
from .paths import ASSETS_DIR

# The predefined asset package for every recording. Each item maps to an
# asset_type in spec/asset.schema.json where applicable.
ASSET_PACKAGE: List[str] = [
    "Episode (edited master)",
    "Trailer",
    "Horizontal clip 1",
    "Horizontal clip 2",
    "Horizontal clip 3",
    "Vertical clips",
    "Chapters / chapter markers",
    "Stills",
    "Artifact asset (the object produced in the episode)",
    "Guest delivery package (clips + stills for the guest's own use)",
    "Audio edition",
    "Transcript + description",
]


@dataclass
class AssetChecklistResult:
    guest_name: str
    slug: str
    path: str
    items: List[str] = field(default_factory=list)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "unnamed"


def generate_asset_checklist(candidate: Dict[str, str], *, assets_dir: Optional[Path] = None,
                             log_path: Optional[Path] = None) -> AssetChecklistResult:
    guest = (candidate.get("guest_name") or "").strip() or "<unnamed>"
    slug = _slug(guest)

    lines: List[str] = [f"# Asset package — {guest}", "",
                        "_Predefined deliverables for this recording. Check each on delivery._", ""]
    for item in ASSET_PACKAGE:
        lines.append(f"- [ ] {item}")
    lines.append("")
    content = "\n".join(lines)

    directory = assets_dir or ASSETS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.md"
    path.write_text(content, encoding="utf-8")

    audit.append("assets.checklist_generated", guest=guest, path=str(path),
                 item_count=len(ASSET_PACKAGE), log_path=log_path)
    return AssetChecklistResult(guest_name=guest, slug=slug, path=str(path), items=list(ASSET_PACKAGE))


def generate_asset_checklists(candidates: List[Dict[str, str]], *, assets_dir: Optional[Path] = None,
                             log_path: Optional[Path] = None) -> List[AssetChecklistResult]:
    return [generate_asset_checklist(c, assets_dir=assets_dir, log_path=log_path) for c in candidates]
