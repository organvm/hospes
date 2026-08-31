"""Tests for human-correction metrics (AA4).

Guards:
- spec/events.json: draft.edited and classification.overridden events exist
- hospes/audit.record_draft_edited: emits the correct event
- hospes/audit.record_classification_overridden: emits the correct event
- hospes/audit.human_correction_counts: counts round-trip correctly
"""
import json
from pathlib import Path

import pytest

from hospes import audit

ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = ROOT / "spec" / "events.json"


# -----------------------------------------------------------------------
# spec/events.json tests
# -----------------------------------------------------------------------

def _load_events() -> dict:
    return json.loads(EVENTS_PATH.read_text(encoding="utf-8"))


def _event_names(events_doc: dict) -> set[str]:
    return {e["name"] for e in events_doc.get("events", [])}


def test_events_file_exists():
    assert EVENTS_PATH.exists()


def test_draft_edited_event_exists():
    names = _event_names(_load_events())
    assert "draft.edited" in names, "events.json must contain draft.edited event"


def test_classification_overridden_event_exists():
    names = _event_names(_load_events())
    assert "classification.overridden" in names, (
        "events.json must contain classification.overridden event"
    )


def test_draft_edited_has_aggregate():
    doc = _load_events()
    entry = next(e for e in doc["events"] if e["name"] == "draft.edited")
    assert entry.get("aggregate"), "draft.edited must have an aggregate field"


def test_classification_overridden_has_aggregate():
    doc = _load_events()
    entry = next(e for e in doc["events"] if e["name"] == "classification.overridden")
    assert entry.get("aggregate"), "classification.overridden must have an aggregate field"


def test_draft_edited_has_description():
    doc = _load_events()
    entry = next(e for e in doc["events"] if e["name"] == "draft.edited")
    assert entry.get("description"), "draft.edited must have a description"


def test_classification_overridden_has_description():
    doc = _load_events()
    entry = next(e for e in doc["events"] if e["name"] == "classification.overridden")
    assert entry.get("description"), "classification.overridden must have a description"


# -----------------------------------------------------------------------
# audit.record_draft_edited tests
# -----------------------------------------------------------------------

def test_record_draft_edited_writes_log(tmp_path):
    log = tmp_path / "audit.log"
    rec = audit.record_draft_edited("opp-001", guest_name="Alice Smith", log_path=log)
    assert log.exists()
    assert rec["action"] == "draft.edited"
    assert rec["opportunity_id"] == "opp-001"
    assert rec["guest_name"] == "Alice Smith"


def test_record_draft_edited_is_json_line(tmp_path):
    log = tmp_path / "audit.log"
    audit.record_draft_edited("opp-002", log_path=log)
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["action"] == "draft.edited"


def test_record_draft_edited_appends(tmp_path):
    log = tmp_path / "audit.log"
    audit.record_draft_edited("opp-001", log_path=log)
    audit.record_draft_edited("opp-002", log_path=log)
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2


# -----------------------------------------------------------------------
# audit.record_classification_overridden tests
# -----------------------------------------------------------------------

def test_record_classification_overridden_writes_log(tmp_path):
    log = tmp_path / "audit.log"
    rec = audit.record_classification_overridden(
        "tp-001",
        original_label="AMBIGUOUS",
        override_label="POSITIVE_INTEREST",
        log_path=log,
    )
    assert log.exists()
    assert rec["action"] == "classification.overridden"
    assert rec["touchpoint_id"] == "tp-001"
    assert rec["original_label"] == "AMBIGUOUS"
    assert rec["override_label"] == "POSITIVE_INTEREST"


def test_record_classification_overridden_is_json_line(tmp_path):
    log = tmp_path / "audit.log"
    audit.record_classification_overridden(
        "tp-002", original_label="SOFT_DECLINE", override_label="HARD_DECLINE", log_path=log
    )
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["action"] == "classification.overridden"


# -----------------------------------------------------------------------
# audit.human_correction_counts round-trip tests
# -----------------------------------------------------------------------

def test_counts_with_empty_log(tmp_path):
    log = tmp_path / "audit.log"
    # Log does not exist yet
    counts = audit.human_correction_counts(log_path=log)
    assert counts["drafts_generated"] == 0
    assert counts["drafts_edited"] == 0
    assert counts["classifications_made"] == 0
    assert counts["classifications_overridden"] == 0
    assert counts["revisions_per_draft"] is None
    assert counts["classification_override_rate"] is None


def test_counts_round_trip(tmp_path):
    log = tmp_path / "audit.log"
    # Emit 3 draft_generated, 1 draft_edited, 2 reply_classified, 1 classification_overridden
    audit.append("outreach.draft_generated", guest="A", log_path=log)
    audit.append("outreach.draft_generated", guest="B", log_path=log)
    audit.append("outreach.draft_generated", guest="C", log_path=log)
    audit.record_draft_edited("opp-A", log_path=log)
    audit.append("reply.classified", label="POSITIVE_INTEREST", log_path=log)
    audit.append("reply.classified", label="SOFT_DECLINE", log_path=log)
    audit.record_classification_overridden(
        "tp-1", original_label="SOFT_DECLINE", override_label="HARD_DECLINE", log_path=log
    )

    counts = audit.human_correction_counts(log_path=log)
    assert counts["drafts_generated"] == 3
    assert counts["drafts_edited"] == 1
    assert counts["classifications_made"] == 2
    assert counts["classifications_overridden"] == 1
    assert abs(counts["revisions_per_draft"] - 1 / 3) < 1e-9
    assert abs(counts["classification_override_rate"] - 0.5) < 1e-9


def test_counts_no_edits_gives_zero_rate(tmp_path):
    log = tmp_path / "audit.log"
    audit.append("outreach.draft_generated", guest="A", log_path=log)
    audit.append("outreach.draft_generated", guest="B", log_path=log)
    counts = audit.human_correction_counts(log_path=log)
    assert counts["drafts_generated"] == 2
    assert counts["drafts_edited"] == 0
    assert counts["revisions_per_draft"] == 0.0


def test_counts_since_filter(tmp_path):
    log = tmp_path / "audit.log"
    # Manually write two records with known timestamps to test since= filtering.
    old_record = json.dumps({"ts": "2026-01-01T00:00:00+00:00", "action": "outreach.draft_generated", "guest": "old"})
    new_record = json.dumps({"ts": "2026-07-14T00:00:00+00:00", "action": "outreach.draft_generated", "guest": "new"})
    log.write_text(old_record + "\n" + new_record + "\n", encoding="utf-8")

    counts_all = audit.human_correction_counts(log_path=log)
    assert counts_all["drafts_generated"] == 2

    counts_recent = audit.human_correction_counts(log_path=log, since="2026-07-01T00:00:00+00:00")
    assert counts_recent["drafts_generated"] == 1
