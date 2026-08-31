"""Append-only audit log.

Every consequential action (approval decisions, protections, draft generation)
appends one structured line here. The log is append-only by contract: we open
in ``"a"`` mode and never rewrite prior lines. Lines are single-line JSON so the
log is both human-skimmable and machine-parseable.

Writes go ONLY to ``out/audit.log`` (gitignored runtime output).

Human-correction capture
------------------------
Two events feed the Operations Analyst human-correction metrics
(see ``docs/evaluation.md`` — Human-correction metrics section):

* ``draft.edited``            — a human revised an AI draft before sending.
* ``classification.overridden`` — a human changed the triage label on a reply.

Helper functions :func:`record_draft_edited` and
:func:`record_classification_overridden` are thin wrappers around
:func:`append` that ensure the canonical event names are used and that the
required fields are always present. Use :func:`human_correction_counts` to
query raw totals from a log file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import AUDIT_LOG, ensure_out_dirs


def append(action: str, *, log_path: Optional[Path] = None, **fields: Any) -> Dict[str, Any]:
    """Append one audit line and return the record that was written."""
    path = log_path or AUDIT_LOG
    if log_path is None:
        ensure_out_dirs()
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
    }
    record.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


# ---------------------------------------------------------------------------
# Human-correction capture helpers
# ---------------------------------------------------------------------------

def record_draft_edited(
    opportunity_id: str,
    *,
    guest_name: str = "",
    editor: str = "",
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record that a human edited an AI-generated outreach draft.

    Emits event ``draft.edited`` — feeds the revisions-per-draft metric.

    Args:
        opportunity_id: The AppearanceOpportunity this draft belongs to.
        guest_name: Human-readable name for log legibility.
        editor: Identifier of the human who made the edit (e.g. a seat label).
        log_path: Override path for the audit log (default: out/audit.log).
    """
    return append(
        "draft.edited",
        opportunity_id=opportunity_id,
        guest_name=guest_name,
        editor=editor,
        log_path=log_path,
    )


def record_classification_overridden(
    touchpoint_id: str,
    *,
    original_label: str,
    override_label: str,
    overridden_by: str = "",
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record that a human overrode the triage agent's classification.

    Emits event ``classification.overridden`` — feeds the override-rate metric.

    Args:
        touchpoint_id: The Touchpoint whose classification was overridden.
        original_label: The label the triage agent assigned.
        override_label: The label the human selected instead.
        overridden_by: Identifier of the human who made the override.
        log_path: Override path for the audit log (default: out/audit.log).
    """
    return append(
        "classification.overridden",
        touchpoint_id=touchpoint_id,
        original_label=original_label,
        override_label=override_label,
        overridden_by=overridden_by,
        log_path=log_path,
    )


def human_correction_counts(
    log_path: Optional[Path] = None,
    *,
    since: Optional[str] = None,
) -> Dict[str, Any]:
    """Return raw human-correction counts from the audit log.

    Reads the append-only log and counts:

    * ``drafts_generated``    — total ``outreach.draft_generated`` events.
    * ``drafts_edited``       — total ``draft.edited`` events.
    * ``classifications_made``— total ``reply.classified`` events.
    * ``classifications_overridden`` — total ``classification.overridden`` events.

    Derived rates (returned as floats, or None when the denominator is 0):

    * ``revisions_per_draft``   = drafts_edited / drafts_generated
    * ``classification_override_rate`` = classifications_overridden / classifications_made

    Args:
        log_path: Path to the audit log. Defaults to out/audit.log.
        since: Optional ISO 8601 date-time string; only events at or after this
               timestamp are counted.

    Returns:
        Dict with raw counts and derived rates.
    """
    path = log_path or AUDIT_LOG
    counters: Dict[str, int] = {
        "drafts_generated": 0,
        "drafts_edited": 0,
        "classifications_made": 0,
        "classifications_overridden": 0,
    }
    action_map = {
        "outreach.draft_generated": "drafts_generated",
        "draft.edited": "drafts_edited",
        "reply.classified": "classifications_made",
        "classification.overridden": "classifications_overridden",
    }

    if not path.exists():
        return {
            **counters,
            "revisions_per_draft": None,
            "classification_override_rate": None,
        }

    lines: List[str] = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and record.get("ts", "") < since:
            continue
        key = action_map.get(record.get("action", ""))
        if key:
            counters[key] += 1

    drafts_generated = counters["drafts_generated"]
    classifications_made = counters["classifications_made"]

    return {
        **counters,
        "revisions_per_draft": (
            counters["drafts_edited"] / drafts_generated if drafts_generated else None
        ),
        "classification_override_rate": (
            counters["classifications_overridden"] / classifications_made
            if classifications_made
            else None
        ),
    }
