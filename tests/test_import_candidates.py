"""Focused tests for private, source-keyed candidate ingestion."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hospes import candidate_import, store
from hospes.__main__ import main


UTC = timezone.utc
BOUNDARY = {
    "tenant_id": "tenant_one",
    "network_id": "network_one",
    "show_id": "show_one",
    "actor_id": "producer_one",
}


def candidate_row(source_key: str = "crm:person:synthetic-001") -> dict[str, str]:
    return {
        "source_key": source_key,
        "guest_name": "Synthetic Candidate",
        "category": "synthetic-practitioner",
        "why_guest": "This synthetic candidate can stress-test a specific operating claim.",
        "why_now": "A fictional public project reached a useful decision point this month.",
        "episode_thesis": (
            "Small teams should treat operational receipts as product infrastructure."
        ),
        "proposed_artifact": "A one-page receipt map",
        "relationship_class": "C2",
        "relationship_owner": "ari_owner",
        "route_type": "producer_system",
        "route_reference": "producer://fixture/route-001",
        "route_verified_at": "2026-07-22T19:00:00+00:00",
        "preferred_city": "Los Angeles",
        "social_cost_1_5": "2",
        "ari_effort": "review_only",
        "next_action": "Review the synthetic thesis in the private operator queue.",
        "next_action_date": "2026-07-24",
        "source_provenance": "Opaque fixture record from the synthetic test CRM.",
    }


def test_import_creates_deterministic_tenant_scoped_opportunity(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "import.sqlite3")
    now = datetime(2026, 7, 22, 20, 0, tzinfo=UTC)

    result = candidate_import.import_candidates(
        conn, [candidate_row()], now=now, **BOUNDARY
    )
    expected_id = candidate_import.candidate_id(
        BOUNDARY["tenant_id"], BOUNDARY["show_id"], "crm:person:synthetic-001"
    )

    assert result.as_dict() == {
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "created_ids": [expected_id],
        "updated_ids": [],
        "unchanged_ids": [],
    }
    opportunity = store.fetch_one(
        conn, "SELECT * FROM appearance_opportunities WHERE id = ?", (expected_id,)
    )
    assert opportunity is not None
    assert opportunity["tenant_id"] == "tenant_one"
    assert opportunity["source_key"] == "crm:person:synthetic-001"
    assert opportunity["status"] == "EDITORIAL_REVIEW"
    assert opportunity["social_cost_1_5"] == 2
    audit = store.fetch_all(
        conn, "SELECT * FROM audit_events WHERE opportunity_id = ?", (expected_id,)
    )
    assert [event["event_type"] for event in audit] == [
        "appearance.candidate_created", "contact_route.verified"
    ]
    assert {event["show_id"] for event in audit} == {BOUNDARY["show_id"]}
    assert audit[0]["details"]["source"] == "private_import"
    route = store.fetch_one(
        conn, "SELECT * FROM contact_routes WHERE opportunity_id = ?", (expected_id,)
    )
    assert route is not None
    assert route["route_label"] == "producer://fixture/route-001"


def test_exact_reimport_is_a_write_free_noop(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "idempotent.sqlite3")
    first_time = datetime(2026, 7, 22, 20, 0, tzinfo=UTC)
    candidate_import.import_candidates(conn, [candidate_row()], now=first_time, **BOUNDARY)

    result = candidate_import.import_candidates(
        conn, [candidate_row()], now=first_time + timedelta(days=1), **BOUNDARY
    )
    opportunity = store.fetch_one(
        conn,
        "SELECT * FROM appearance_opportunities WHERE tenant_id = ? AND source_key = ?",
        ("tenant_one", "crm:person:synthetic-001"),
    )

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1
    assert opportunity is not None
    assert opportunity["updated_at"] == first_time.isoformat()
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM audit_events")["count"] == 2


def test_changed_reimport_updates_metadata_but_preserves_lifecycle(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "update.sqlite3")
    first_time = datetime(2026, 7, 22, 20, 0, tzinfo=UTC)
    created = candidate_import.import_candidates(
        conn, [candidate_row()], now=first_time, **BOUNDARY
    )
    opportunity_id = created.created_ids[0]
    store.update(conn, "appearance_opportunities", opportunity_id, {
        "status": "APPROVED", "disposition": "APPROVED"
    })
    conn.commit()

    changed = candidate_row()
    changed["next_action"] = "Generate a synthetic invitation draft for human review."
    second_time = first_time + timedelta(hours=1)
    result = candidate_import.import_candidates(
        conn, [changed], now=second_time, **BOUNDARY
    )
    opportunity = store.fetch_one(
        conn, "SELECT * FROM appearance_opportunities WHERE id = ?", (opportunity_id,)
    )

    assert result.updated_ids == [opportunity_id]
    assert opportunity is not None
    assert opportunity["next_action"] == changed["next_action"]
    assert opportunity["status"] == "APPROVED"
    assert opportunity["disposition"] == "APPROVED"
    events = store.fetch_all(
        conn, "SELECT * FROM audit_events WHERE opportunity_id = ? ORDER BY created_at",
        (opportunity_id,),
    )
    assert [event["event_type"] for event in events] == [
        "appearance.candidate_created", "contact_route.verified", "correction.appended"
    ]
    assert events[-1]["details"]["changed_fields"] == ["next_action"]


def test_same_source_key_is_isolated_between_tenants(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "tenants.sqlite3")
    first = candidate_import.import_candidates(conn, [candidate_row()], **BOUNDARY)
    second = candidate_import.import_candidates(
        conn,
        [candidate_row()],
        **{**BOUNDARY, "tenant_id": "tenant_two", "actor_id": "producer_two"},
    )

    assert first.created_ids[0] != second.created_ids[0]
    rows = store.fetch_all(
        conn,
        "SELECT tenant_id, source_key FROM appearance_opportunities ORDER BY tenant_id",
    )
    assert rows == [
        {"tenant_id": "tenant_one", "source_key": "crm:person:synthetic-001"},
        {"tenant_id": "tenant_two", "source_key": "crm:person:synthetic-001"},
    ]


def test_same_source_key_is_isolated_between_shows(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "shows.sqlite3")
    first = candidate_import.import_candidates(conn, [candidate_row()], **BOUNDARY)
    second = candidate_import.import_candidates(
        conn,
        [candidate_row()],
        **{**BOUNDARY, "show_id": "show_two"},
    )

    assert first.created_ids[0] != second.created_ids[0]
    rows = store.fetch_all(
        conn,
        "SELECT show_id, source_key FROM appearance_opportunities ORDER BY show_id",
    )
    assert rows == [
        {"show_id": "show_one", "source_key": "crm:person:synthetic-001"},
        {"show_id": "show_two", "source_key": "crm:person:synthetic-001"},
    ]


def test_declared_tenant_mismatch_fails_before_any_write(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "mismatch.sqlite3")
    row = {**candidate_row(), "tenant_id": "tenant_two"}

    with pytest.raises(candidate_import.CandidateImportError) as caught:
        candidate_import.import_candidates(conn, [row], **BOUNDARY)

    assert caught.value.issues[0].field_name == "tenant_id"
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0


@pytest.mark.parametrize("field_name", ["email_address", "private_notes", "signed_release"])
def test_private_payload_fields_are_rejected(tmp_path: Path, field_name: str) -> None:
    conn = store.connect(tmp_path / f"private-{field_name}.sqlite3")
    row = {**candidate_row(), field_name: "synthetic-private-value"}

    with pytest.raises(candidate_import.CandidateImportError) as caught:
        candidate_import.import_candidates(conn, [row], **BOUNDARY)

    assert field_name in {issue.field_name for issue in caught.value.issues}
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0


@pytest.mark.parametrize(
    ("field_name", "private_value"),
    [
        ("source_provenance", "Imported from the producer at person@example.org."),
        ("next_action", "Call +1 (310) 555-0199 after the review."),
        ("why_guest", "The candidate is reachable at 3105550199 for this private pilot."),
    ],
)
def test_contact_content_in_any_persisted_string_rejects_the_whole_batch(
    tmp_path: Path, field_name: str, private_value: str
) -> None:
    conn = store.connect(tmp_path / f"contact-{field_name}.sqlite3")
    invalid = {**candidate_row("crm:person:synthetic-002"), field_name: private_value}

    with pytest.raises(candidate_import.CandidateImportError) as caught:
        candidate_import.import_candidates(
            conn,
            [candidate_row("crm:person:synthetic-001"), invalid],
            now=datetime(2026, 7, 22, 20, 0, tzinfo=UTC),
            **BOUNDARY,
        )

    assert field_name in {issue.field_name for issue in caught.value.issues}
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0


def test_future_route_verification_is_rejected_after_utc_normalization(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "future-route.sqlite3")
    row = candidate_row()
    row["route_verified_at"] = "2026-07-22T17:01:00-03:00"

    with pytest.raises(candidate_import.CandidateImportError) as caught:
        candidate_import.import_candidates(
            conn,
            [row],
            now=datetime(2026, 7, 22, 20, 0, tzinfo=UTC),
            **BOUNDARY,
        )

    assert any(
        issue.field_name == "route_verified_at" and "future" in issue.message
        for issue in caught.value.issues
    )
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0


def test_reimport_keeps_a_protected_contact_route_disabled(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "protected-route.sqlite3")
    created = candidate_import.import_candidates(
        conn,
        [candidate_row()],
        now=datetime(2026, 7, 22, 20, 0, tzinfo=UTC),
        **BOUNDARY,
    )
    opportunity_id = created.created_ids[0]
    store.update(
        conn,
        "appearance_opportunities",
        opportunity_id,
        {"status": "DO_NOT_CONTACT", "disposition": "PROTECTED"},
    )
    route = store.fetch_one(
        conn, "SELECT * FROM contact_routes WHERE opportunity_id = ?", (opportunity_id,)
    )
    store.update(conn, "contact_routes", route["id"], {"usable": False})
    conn.commit()

    result = candidate_import.import_candidates(
        conn,
        [candidate_row()],
        now=datetime(2026, 7, 23, 20, 0, tzinfo=UTC),
        **BOUNDARY,
    )

    assert result.unchanged_ids == [opportunity_id]
    assert store.fetch_one(
        conn, "SELECT usable FROM contact_routes WHERE opportunity_id = ?", (opportunity_id,)
    )["usable"] is False


def test_invalid_batch_is_atomic(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "atomic.sqlite3")
    invalid = candidate_row("crm:person:synthetic-002")
    invalid["source_key"] = ""

    with pytest.raises(candidate_import.CandidateImportError):
        candidate_import.import_candidates(
            conn, [candidate_row(), invalid], **BOUNDARY
        )

    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0


def test_duplicate_source_key_in_one_batch_is_rejected(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "duplicate.sqlite3")

    with pytest.raises(candidate_import.CandidateImportError) as caught:
        candidate_import.import_candidates(
            conn, [candidate_row(), candidate_row()], **BOUNDARY
        )

    assert any("duplicated" in issue.message for issue in caught.value.issues)


def test_csv_file_adapter_is_cli_ready(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "file.sqlite3")
    csv_path = tmp_path / "private-candidates.csv"
    row = candidate_row()
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)

    result = candidate_import.import_candidate_file(conn, csv_path, **BOUNDARY)

    assert result.created == 1
    assert result.updated == 0
    assert result.unchanged == 0


def test_import_candidates_cli_is_idempotent(tmp_path: Path, capsys) -> None:
    csv_path = tmp_path / "private-candidates.csv"
    database = tmp_path / "operator.sqlite3"
    row = candidate_row()
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)

    argv = [
        "import-candidates", str(csv_path), "--db", str(database),
        "--tenant", "tenant_one", "--network", "network_one",
        "--show", "show_one", "--actor", "producer_one",
    ]
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created"] == 1

    assert main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second == {
        "created": 0,
        "created_ids": [],
        "unchanged": 1,
        "unchanged_ids": first["created_ids"],
        "updated": 0,
        "updated_ids": [],
    }
