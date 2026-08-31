from datetime import date

import pytest

from hospes import commitments
from hospes.commitments import CommitmentError


def test_create_and_list(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    c = commitments.create("opp-1", "Send transcript", owner="producer",
                           deadline="2026-09-01", path=csv, log_path=log)
    assert c.id == "C0001"
    all_c = commitments.list_all(csv)
    assert len(all_c) == 1
    assert all_c[0].description == "Send transcript"


def test_ids_increment(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    commitments.create("o", "a", path=csv, log_path=log)
    c2 = commitments.create("o", "b", path=csv, log_path=log)
    assert c2.id == "C0002"


def test_update(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    c = commitments.create("o", "a", path=csv, log_path=log)
    updated = commitments.update(c.id, status="completed", path=csv, log_path=log)
    assert updated.status == "completed"


def test_update_invalid_status_raises(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    c = commitments.create("o", "a", path=csv, log_path=log)
    with pytest.raises(CommitmentError):
        commitments.update(c.id, status="bogus", path=csv, log_path=log)


def test_delete(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    c = commitments.create("o", "a", path=csv, log_path=log)
    assert commitments.delete(c.id, path=csv, log_path=log) is True
    assert commitments.list_all(csv) == []


def test_list_due(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    commitments.create("o", "past", deadline="2020-01-01", path=csv, log_path=log)
    commitments.create("o", "future", deadline="2999-01-01", path=csv, log_path=log)
    done = commitments.create("o", "done-past", deadline="2020-01-01",
                              status="completed", path=csv, log_path=log)
    due = commitments.list_due(as_of=date(2026, 7, 13), path=csv)
    descriptions = {c.description for c in due}
    assert "past" in descriptions
    assert "future" not in descriptions
    assert done.description not in descriptions


def test_create_invalid_status_raises(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    with pytest.raises(CommitmentError):
        commitments.create("o", "a", status="nope", path=csv, log_path=log)
