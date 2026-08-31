"""Promises ledger — the Commitment Tracker (transcript §15).

Podcasts damage relationships by forgetting small promises ("we'll send the
transcript", "we'll confirm the title"). This module is a CRUD ledger over
``out/commitments.csv`` so every promise becomes an owned, deadline-bearing
task.

Columns: ``id, opportunity, description, owner, deadline, status``. Financial /
contractual / editorial-removal promises should carry a status of
``requires_human_approval`` — those are human-gated by policy.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import audit
from .paths import COMMITMENTS_CSV, ensure_out_dirs

FIELDNAMES = ["id", "opportunity", "description", "owner", "deadline", "status"]

VALID_STATUSES = {
    "pending",
    "in_progress",
    "completed",
    "cancelled",
    "requires_human_approval",
    "overdue",
}


@dataclass
class Commitment:
    id: str
    opportunity: str
    description: str
    owner: str = ""
    deadline: str = ""       # ISO date/datetime string, or ""
    status: str = "pending"

    def to_row(self) -> Dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}


class CommitmentError(ValueError):
    pass


def _load_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def _write_rows(path: Path, rows: List[Dict[str, str]]) -> None:
    if path == COMMITMENTS_CSV:
        ensure_out_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=FIELDNAMES,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})


def _next_id(rows: List[Dict[str, str]]) -> str:
    max_n = 0
    for r in rows:
        rid = r.get("id", "")
        if rid.startswith("C") and rid[1:].isdigit():
            max_n = max(max_n, int(rid[1:]))
    return f"C{max_n + 1:04d}"


def list_all(path: Optional[Path] = None) -> List[Commitment]:
    rows = _load_rows(path or COMMITMENTS_CSV)
    return [Commitment(**{k: r.get(k, "") for k in FIELDNAMES}) for r in rows]


def create(opportunity: str, description: str, *, owner: str = "", deadline: str = "",
           status: str = "pending", path: Optional[Path] = None,
           log_path: Optional[Path] = None) -> Commitment:
    if status not in VALID_STATUSES:
        raise CommitmentError(f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}")
    target = path or COMMITMENTS_CSV
    rows = _load_rows(target)
    commitment = Commitment(
        id=_next_id(rows),
        opportunity=opportunity,
        description=description,
        owner=owner,
        deadline=deadline,
        status=status,
    )
    rows.append(commitment.to_row())
    _write_rows(target, rows)
    audit.append("commitment.created", commitment_id=commitment.id, opportunity=opportunity,
                 status=status, log_path=log_path)
    return commitment


def get(commitment_id: str, path: Optional[Path] = None) -> Optional[Commitment]:
    for c in list_all(path):
        if c.id == commitment_id:
            return c
    return None


def update(commitment_id: str, *, path: Optional[Path] = None, log_path: Optional[Path] = None,
           **changes: str) -> Commitment:
    target = path or COMMITMENTS_CSV
    rows = _load_rows(target)
    found = None
    for row in rows:
        if row.get("id") == commitment_id:
            for key, val in changes.items():
                if key not in FIELDNAMES or key == "id":
                    raise CommitmentError(f"cannot update field {key!r}")
                if key == "status" and val not in VALID_STATUSES:
                    raise CommitmentError(f"invalid status {val!r}")
                row[key] = str(val)
            found = row
            break
    if found is None:
        raise CommitmentError(f"commitment {commitment_id!r} not found")
    _write_rows(target, rows)
    audit.append("commitment.updated", commitment_id=commitment_id, changes=list(changes.keys()),
                 log_path=log_path)
    return Commitment(**{k: found.get(k, "") for k in FIELDNAMES})


def delete(commitment_id: str, *, path: Optional[Path] = None, log_path: Optional[Path] = None) -> bool:
    target = path or COMMITMENTS_CSV
    rows = _load_rows(target)
    remaining = [r for r in rows if r.get("id") != commitment_id]
    if len(remaining) == len(rows):
        return False
    _write_rows(target, remaining)
    audit.append("commitment.deleted", commitment_id=commitment_id, log_path=log_path)
    return True


def _parse_deadline(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def list_due(as_of: Optional[date] = None, path: Optional[Path] = None) -> List[Commitment]:
    """Commitments whose deadline is on/before ``as_of`` and not yet resolved."""
    today = as_of or date.today()
    due: List[Commitment] = []
    for c in list_all(path):
        if c.status in ("completed", "cancelled"):
            continue
        deadline = _parse_deadline(c.deadline)
        if deadline is not None and deadline <= today:
            due.append(c)
    return due
