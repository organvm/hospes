"""The human-gated approval flow.

A human reviews candidates in the approval dashboard and exports a decisions
JSON. This module applies those decisions to pipeline rows:

* **APPROVE**  -> row status becomes ``APPROVED``; opportunity may proceed to
  contact-route + drafting.
* **REJECT**   -> row status becomes ``DECLINED``; note preserved as decline reason.
* **PROTECT**  -> relationship class is raised to at least ``C4`` and the row is
  flagged ``protected``; no automated route may ever run on it.
* **NOTE**     -> attach a personal note without changing status.

Decisions accept the dashboard's export shape as well as the compact
``[{guest_name, decision, note?}]`` shape. Decision verbs are matched
tolerantly (``APPROVE``/``APPROVED``, ``PROTECT``/``PROTECTED``, etc.).

Every decision appends one line to the audit log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import audit

# Decision verb -> canonical form. The dashboard exports past-tense
# (APPROVED/REJECTED/PROTECTED); the compact contract uses imperative.
_DECISION_ALIASES = {
    "APPROVE": "APPROVE",
    "APPROVED": "APPROVE",
    "REJECT": "REJECT",
    "REJECTED": "REJECT",
    "DECLINE": "REJECT",
    "DECLINED": "REJECT",
    "PROTECT": "PROTECT",
    "PROTECTED": "PROTECT",
    "NOTE": "NOTE",
    "PENDING": "NOTE",  # a pending decision with a note -> just record the note
}

PROTECTED_MIN_CLASS = "C4"


@dataclass
class DecisionResult:
    applied: List[Dict[str, Any]] = field(default_factory=list)
    unmatched: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unmatched


def normalize_decision(raw: str) -> Optional[str]:
    if raw is None:
        return None
    return _DECISION_ALIASES.get(raw.strip().upper())


def load_decisions(json_path: Path) -> List[Dict[str, Any]]:
    """Load a decisions JSON exported by the dashboard.

    Accepts either a top-level list or ``{"decisions": [...]}``.
    """
    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "decisions" in data:
        data = data["decisions"]
    if not isinstance(data, list):
        raise ValueError("decisions JSON must be a list or {'decisions': [...]}")
    return data


def _decision_name(entry: Dict[str, Any]) -> Optional[str]:
    """Extract the guest name a decision refers to (guest_name or name)."""
    for key in ("guest_name", "name"):
        val = entry.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _row_name(row: Dict[str, str]) -> str:
    return (row.get("guest_name") or "").strip()


def apply_decision(row: Dict[str, str], decision: str, note: str = "", *,
                   log_path: Optional[Path] = None) -> Dict[str, str]:
    """Apply a single normalized decision to a row (in place) and audit it."""
    canonical = normalize_decision(decision)
    if canonical is None:
        raise ValueError(f"Unknown decision verb: {decision!r}")

    guest = _row_name(row) or "<unnamed>"

    if canonical == "APPROVE":
        row["status"] = "APPROVED"
    elif canonical == "REJECT":
        row["status"] = "DECLINED"
        if note:
            row["notes"] = _append_note(row.get("notes", ""), f"decline reason: {note}")
    elif canonical == "PROTECT":
        # Raise relationship class to at least C4 and flag as protected so no
        # automated route can ever run on this row.
        current = (row.get("relationship_class") or "").strip().upper()
        if current not in ("C4", "C5"):
            row["relationship_class"] = PROTECTED_MIN_CLASS
        row["protected"] = "true"
        row["ari_effort"] = "direct_contact_required"
        if note:
            row["notes"] = _append_note(row.get("notes", ""), note)
    elif canonical == "NOTE":
        if note:
            row["notes"] = _append_note(row.get("notes", ""), note)

    audit.append(
        "approval.decision",
        guest=guest,
        decision=canonical,
        note=note or "",
        resulting_status=row.get("status", ""),
        relationship_class=row.get("relationship_class", ""),
        protected=row.get("protected", "false"),
        log_path=log_path,
    )
    return row


def _append_note(existing: str, note: str) -> str:
    existing = (existing or "").strip()
    if not existing:
        return note
    return f"{existing} | {note}"


def apply_decisions(rows: List[Dict[str, str]], decisions: List[Dict[str, Any]], *,
                    log_path: Optional[Path] = None) -> DecisionResult:
    """Apply a batch of decisions to pipeline rows, matched by guest name."""
    by_name: Dict[str, Dict[str, str]] = {}
    for row in rows:
        name = _row_name(row)
        if name:
            by_name[name.lower()] = row

    result = DecisionResult()
    for entry in decisions:
        name = _decision_name(entry)
        decision_raw = entry.get("decision")
        if name is None or decision_raw is None:
            result.unmatched.append(entry)
            continue
        canonical = normalize_decision(decision_raw)
        # A bare PENDING with no note is a no-op we skip silently.
        if canonical == "NOTE" and not (entry.get("note") or "").strip():
            continue
        row = by_name.get(name.lower())
        if row is None:
            result.unmatched.append(entry)
            continue
        apply_decision(row, decision_raw, entry.get("note", "") or "", log_path=log_path)
        result.applied.append({"guest_name": name, "decision": canonical})
    return result


def is_protected(row: Dict[str, str]) -> bool:
    """A row is protected if flagged, or its relationship class is C4/C5."""
    if str(row.get("protected", "")).strip().lower() == "true":
        return True
    return (row.get("relationship_class") or "").strip().upper() in ("C4", "C5")
