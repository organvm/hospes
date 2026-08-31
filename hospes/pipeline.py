"""Guest pipeline: load / validate / save candidate rows.

The pipeline is a CSV whose headers come from the starter-pack
``guest_pipeline_template.csv``. Each row is one candidate (an
AppearanceOpportunity seed). ``workflow.json`` declares the required fields a
candidate must carry before it can move through the lifecycle; we validate each
row against that contract and return *structured* errors (never a bare
exception) so the demo and dashboard can surface exactly which field is missing
on which row.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Header order, verbatim from the starter-pack guest_pipeline_template.csv.
PIPELINE_HEADERS: List[str] = [
    "guest_name",
    "category",
    "why_guest",
    "why_now",
    "episode_thesis",
    "proposed_artifact",
    "relationship_class",
    "relationship_owner",
    "contact_route",
    "verified_contact",
    "preferred_city",
    "date_window",
    "social_cost_1_5",
    "ari_effort",
    "status",
    "next_action",
    "next_action_date",
    "source_provenance",
    "notes",
]

# workflow.json candidate_required_fields -> the CSV column that satisfies it.
# The workflow uses shorthand names (route, city, social_cost); the CSV uses
# the fuller template column names. This map is the single source of truth for
# that correspondence.
REQUIRED_FIELD_TO_COLUMN: Dict[str, str] = {
    "guest_name": "guest_name",
    "why_guest": "why_guest",
    "why_now": "why_now",
    "episode_thesis": "episode_thesis",
    "proposed_artifact": "proposed_artifact",
    "relationship_class": "relationship_class",
    "route": "contact_route",
    "social_cost": "social_cost_1_5",
    "city": "preferred_city",
    "ari_effort": "ari_effort",
    "source_provenance": "source_provenance",
    "next_action": "next_action",
}

VALID_RELATIONSHIP_CLASSES = {"C0", "C1", "C2", "C3", "C4", "C5"}

# Template placeholder tokens (e.g. "[CANDIDATE A]") count as unfilled.
_PLACEHOLDER = ("[", "]")


@dataclass
class CandidateError:
    row_index: int          # 0-based index into the data rows (not counting header)
    guest_name: str
    field_name: str
    message: str


@dataclass
class ValidationResult:
    valid: List[Dict[str, str]] = field(default_factory=list)
    errors: List[CandidateError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_blank(value: Optional[str]) -> bool:
    if value is None:
        return True
    v = value.strip()
    if not v:
        return True
    # A bare template placeholder like "[FIELD]" is unfilled.
    if v.startswith(_PLACEHOLDER[0]) and v.endswith(_PLACEHOLDER[1]):
        return True
    return False


def load_candidates(csv_path: Path) -> List[Dict[str, str]]:
    """Load candidate rows from ``csv_path`` (dict per row, header-keyed)."""
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [dict(row) for row in reader]
    return rows


def save_candidates(csv_path: Path, rows: List[Dict[str, str]]) -> None:
    """Write candidate rows back to ``csv_path`` with the canonical header order."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=PIPELINE_HEADERS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in PIPELINE_HEADERS})


def validate_candidate(row: Dict[str, str], row_index: int) -> List[CandidateError]:
    """Validate one row against the required-field contract."""
    errors: List[CandidateError] = []
    guest_name = (row.get("guest_name") or "").strip() or "<unnamed>"

    for req_name, column in REQUIRED_FIELD_TO_COLUMN.items():
        if _is_blank(row.get(column)):
            errors.append(
                CandidateError(
                    row_index=row_index,
                    guest_name=guest_name,
                    field_name=column,
                    message=f"required field {column!r} (required as {req_name!r}) is missing or a placeholder",
                )
            )

    rel = (row.get("relationship_class") or "").strip().upper()
    if rel and rel not in VALID_RELATIONSHIP_CLASSES:
        errors.append(
            CandidateError(
                row_index=row_index,
                guest_name=guest_name,
                field_name="relationship_class",
                message=f"relationship_class {rel!r} not in {sorted(VALID_RELATIONSHIP_CLASSES)}",
            )
        )
    return errors


def validate_candidates(rows: List[Dict[str, str]]) -> ValidationResult:
    """Validate every row; partition into valid rows and structured errors."""
    result = ValidationResult()
    for i, row in enumerate(rows):
        row_errors = validate_candidate(row, i)
        if row_errors:
            result.errors.extend(row_errors)
        else:
            result.valid.append(row)
    return result


def validate_file(csv_path: Path) -> ValidationResult:
    return validate_candidates(load_candidates(csv_path))
