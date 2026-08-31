import json

from hospes import approvals


def _row(name="Guest X", rel="C1", status="RESEARCHING"):
    return {"guest_name": name, "relationship_class": rel, "status": status, "notes": ""}


def test_approve_sets_status(tmp_path):
    log = tmp_path / "audit.log"
    row = _row()
    approvals.apply_decision(row, "APPROVE", log_path=log)
    assert row["status"] == "APPROVED"
    assert log.exists()


def test_reject_sets_declined_and_note(tmp_path):
    log = tmp_path / "audit.log"
    row = _row()
    approvals.apply_decision(row, "REJECTED", "not a fit", log_path=log)
    assert row["status"] == "DECLINED"
    assert "not a fit" in row["notes"]


def test_protect_raises_class_and_blocks(tmp_path):
    log = tmp_path / "audit.log"
    row = _row(rel="C1")
    approvals.apply_decision(row, "PROTECT", "friend", log_path=log)
    assert row["relationship_class"] in ("C4", "C5")
    assert approvals.is_protected(row)
    assert row["ari_effort"] == "direct_contact_required"


def test_protect_does_not_downgrade_c5(tmp_path):
    log = tmp_path / "audit.log"
    row = _row(rel="C5")
    approvals.apply_decision(row, "PROTECT", log_path=log)
    assert row["relationship_class"] == "C5"


def test_note_only_preserves_status(tmp_path):
    log = tmp_path / "audit.log"
    row = _row(status="RESEARCHING")
    approvals.apply_decision(row, "NOTE", "hold for fall", log_path=log)
    assert row["status"] == "RESEARCHING"
    assert "hold for fall" in row["notes"]


def test_apply_decisions_matches_by_name(tmp_path):
    log = tmp_path / "audit.log"
    rows = [_row("Dana Reyes", "C0"), _row("Jordan Blake", "C4")]
    decisions = [
        {"guest_name": "Dana Reyes", "decision": "APPROVE"},
        {"guest_name": "Jordan Blake", "decision": "PROTECT", "note": "friend"},
    ]
    res = approvals.apply_decisions(rows, decisions, log_path=log)
    assert res.ok
    assert rows[0]["status"] == "APPROVED"
    assert approvals.is_protected(rows[1])


def test_dashboard_export_shape_with_name_key(tmp_path):
    # Dashboard exports {name, decision:"PROTECTED"} rather than guest_name/PROTECT.
    log = tmp_path / "audit.log"
    rows = [_row("Sam Okafor", "C2")]
    decisions = [{"name": "Sam Okafor", "decision": "PROTECTED", "note": "handle myself"}]
    res = approvals.apply_decisions(rows, decisions, log_path=log)
    assert res.ok
    assert approvals.is_protected(rows[0])


def test_unmatched_decision_reported(tmp_path):
    log = tmp_path / "audit.log"
    rows = [_row("Dana Reyes")]
    decisions = [{"guest_name": "Nobody Here", "decision": "APPROVE"}]
    res = approvals.apply_decisions(rows, decisions, log_path=log)
    assert not res.ok
    assert res.unmatched


def test_audit_log_appends_json_line(tmp_path):
    log = tmp_path / "audit.log"
    row = _row()
    approvals.apply_decision(row, "APPROVE", log_path=log)
    approvals.apply_decision(row, "NOTE", "second", log_path=log)
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["action"] == "approval.decision"


def test_pending_with_no_note_is_skipped(tmp_path):
    log = tmp_path / "audit.log"
    rows = [_row("Dana Reyes")]
    decisions = [{"guest_name": "Dana Reyes", "decision": "PENDING", "note": ""}]
    res = approvals.apply_decisions(rows, decisions, log_path=log)
    assert res.ok
    assert rows[0]["status"] == "RESEARCHING"
