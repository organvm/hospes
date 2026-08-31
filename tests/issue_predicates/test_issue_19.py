"""Executable completion predicate for HOSPES issue #19."""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import synthetic_bearer_authenticator
from hospes import candidate_import, store, suggest
from hospes.__main__ import main
from hospes.api import create_app


AS_OF = date(2026, 8, 10)
BOUNDARY = {
    "tenant_id": "fixture_tenant",
    "network_id": "fixture_network",
    "show_id": "fixture_show",
    "actor_id": "producer_fixture",
}


def _archive_row(
    name: str,
    appeared_on: str,
    *,
    title: str | None = None,
    relationship_class: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "episode_no": None,
        "title": title or f"{name} returns",
        "date": appeared_on,
        "duration": "01:00:00",
        "type": "guest",
        "guest": name,
        "guest_raw": name,
        "confidence": "high",
        "description_head": "Public fixture description.",
    }
    if relationship_class is not None:
        row["relationship_class"] = relationship_class
    return row


def _write_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "archive.json"
    archive.write_text(
        json.dumps(
            [
                _archive_row("Older Alum", "2019-04-01"),
                _archive_row("Returning Alum", "2020-03-01"),
                _archive_row("Returning Alum", "2023-07-01", title="A New Chapter"),
                _archive_row("Recent Alum", "2025-08-09"),
                _archive_row(
                    "Direct Alum", "2021-01-01", relationship_class="C1"
                ),
                _archive_row(
                    "Cold Alum", "2020-01-01", relationship_class="C3"
                ),
            ]
        ),
        encoding="utf-8",
    )
    return archive


def _suggestion_csv(archive: Path, **kwargs: object) -> str:
    rows = suggest.suggest_guests(archive_path=archive, as_of=AS_OF, **kwargs)
    output = io.StringIO()
    suggest.write_csv(rows, output)
    return output.getvalue()


def test_archive_gap_cost_class_route_and_limit_filters(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path)

    c2 = suggest.suggest_guests(
        archive_path=archive,
        as_of=AS_OF,
        min_gap_years=2,
        max_social_cost=3,
        relationship_class="C2",
        limit=10,
    )
    assert [row.name for row in c2] == ["Older Alum", "Returning Alum"]
    assert [row.estimated_social_cost for row in c2] == [2, 3]
    assert c2[1].last_appearance.isoformat() == "2023-07-01"
    assert c2[1].episodes_since == 1
    assert c2[1].suggested_route == "Anthony warm intro"
    assert "A New Chapter" in c2[1].notes

    low_cost = suggest.suggest_guests(
        archive_path=archive,
        as_of=AS_OF,
        max_social_cost=2,
        relationship_class="C2",
        limit=1,
    )
    assert [row.name for row in low_cost] == ["Older Alum"]

    direct = suggest.suggest_guests(
        archive_path=archive,
        as_of=AS_OF,
        relationship_class="C1",
        limit=10,
    )
    cold = suggest.suggest_guests(
        archive_path=archive,
        as_of=AS_OF,
        relationship_class="C3",
        limit=10,
    )
    assert (direct[0].suggested_route, direct[0].route_type) == (
        "Ari direct",
        "ari_direct",
    )
    assert (cold[0].suggested_route, cold[0].route_type) == (
        "Producer cold",
        "producer_cold",
    )
    assert suggest.estimate_social_cost("C2", 3) == 3
    assert suggest.estimate_social_cost("C2", 4) == 2


def test_public_tour_provenance_is_bounded_and_private_fields_fail_closed(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path)
    tour = tmp_path / "tour.csv"
    tour.write_text(
        "guest,date,city,source,source_url\n"
        "Returning Alum,2026-09-12,Austin,Official public tour feed,"
        "https://tour.example/events/returning-alum\n",
        encoding="utf-8",
    )
    rows = suggest.suggest_guests(
        archive_path=archive,
        tour_paths=[tour],
        as_of=AS_OF,
        relationship_class="C2",
        limit=10,
    )
    returning = next(row for row in rows if row.name == "Returning Alum")
    assert returning.preferred_city == "Austin"
    assert "2026-09-12" in returning.why_now
    assert "https://tour.example/events/returning-alum" in returning.source_provenance

    unsafe = tmp_path / "unsafe-tour.json"
    unsafe.write_text(
        json.dumps(
            [
                {
                    "guest": "Returning Alum",
                    "date": "2026-09-12",
                    "city": "Austin",
                    "source": "Public fixture",
                    "email_address": "private-value@example.test",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(suggest.SuggestionError) as caught:
        suggest.suggest_guests(
            archive_path=archive,
            tour_paths=[unsafe],
            as_of=AS_OF,
        )
    assert "private-field names" in str(caught.value)
    assert "private-value" not in str(caught.value)


def test_csv_contract_imports_from_stream_as_unverified_review(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path)
    payload = _suggestion_csv(
        archive,
        relationship_class="C2",
        min_gap_years=2,
        max_social_cost=3,
        limit=1,
    )
    reader = csv.DictReader(io.StringIO(payload))
    assert tuple(reader.fieldnames or ())[:8] == suggest.DISPLAY_FIELDS
    row = next(reader)
    assert row["suggestion_format"] == suggest.SUGGESTION_FORMAT
    assert row["route_usable"] == "false"
    assert row["name"] == row["guest_name"]
    assert set(candidate_import._REQUIRED_FIELDS).issubset(row)  # noqa: SLF001

    conn = store.connect(tmp_path / "suggestions.sqlite3")
    result = candidate_import.import_candidate_stream(
        conn,
        io.StringIO(payload),
        **BOUNDARY,
    )
    assert result.created == 1
    opportunity = store.fetch_one(
        conn,
        "SELECT * FROM appearance_opportunities WHERE id = ?",
        (result.created_ids[0],),
    )
    route = store.fetch_one(
        conn,
        "SELECT * FROM contact_routes WHERE opportunity_id = ?",
        (result.created_ids[0],),
    )
    events = store.fetch_all(
        conn,
        "SELECT * FROM audit_events WHERE opportunity_id = ? ORDER BY created_at",
        (result.created_ids[0],),
    )
    assert opportunity is not None
    assert opportunity["tenant_id"] == BOUNDARY["tenant_id"]
    assert opportunity["show_id"] == BOUNDARY["show_id"]
    assert opportunity["source_provenance"].startswith(
        "Unlicensed Therapy public archive"
    )
    assert route is not None and route["usable"] is False
    assert route["route_type"] == "anthony_warm_intro"
    assert [event["event_type"] for event in events] == [
        "appearance.candidate_created",
        "contact_route.proposed",
    ]
    assert {event["details"]["source"] for event in events} == {
        "public_suggestion"
    }
    conn.close()


def test_suggestion_envelope_cannot_promote_or_mislabel_a_route(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path)
    row = next(csv.DictReader(io.StringIO(_suggestion_csv(archive, limit=1))))
    conn = store.connect(tmp_path / "invalid-envelope.sqlite3")

    for field, value in (
        ("route_usable", "true"),
        ("suggested_route", "Ari direct"),
        ("route_type", "ari_direct"),
        ("name", "Different Name"),
        ("estimated_social_cost", "5"),
    ):
        invalid = {**row, field: value}
        with pytest.raises(candidate_import.CandidateImportError):
            candidate_import.import_candidates(conn, [invalid], **BOUNDARY)
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0
    conn.close()


def test_cli_stdout_pipes_directly_to_stdin_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_archive(tmp_path)
    assert main(
        [
            "suggest-guests",
            "--archive",
            str(archive),
            "--as-of",
            AS_OF.isoformat(),
            "--min-gap-years",
            "2",
            "--max-social-cost",
            "3",
            "--limit",
            "1",
        ]
    ) == 0
    generated = capsys.readouterr()
    assert generated.err == ""
    assert generated.out.startswith("source_key,name,relationship_class,")

    database = tmp_path / "operator.sqlite3"
    monkeypatch.setattr(sys, "stdin", io.StringIO(generated.out))
    assert main(
        [
            "import-candidates",
            "-",
            "--db",
            str(database),
            "--tenant",
            "fixture_tenant",
            "--network",
            "fixture_network",
            "--show",
            "fixture_show",
            "--actor",
            "producer_fixture",
        ]
    ) == 0
    imported = capsys.readouterr()
    assert json.loads(imported.out)["created"] == 1
    conn = store.connect(database)
    assert store.fetch_one(conn, "SELECT usable FROM contact_routes")["usable"] is False
    conn.close()


def test_authenticated_api_and_dashboard_keep_tenant_show_scope(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path)
    rows = list(csv.DictReader(io.StringIO(_suggestion_csv(archive, limit=2))))
    database = tmp_path / "api.sqlite3"
    conn = store.connect(database)
    candidate_import.import_candidates(conn, [rows[0]], **BOUNDARY)
    second = {
        **rows[1],
        "source_key": f"{rows[1]['source_key']}:second-show",
    }
    candidate_import.import_candidates(
        conn,
        [second],
        **{**BOUNDARY, "show_id": "second_show"},
    )
    conn.commit()
    conn.close()

    token = "synthetic-issue-19-token"  # allow-secret: synthetic test fixture
    app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {  # allow-secret: synthetic bearer lookup fixture
                token: (  # allow-secret: synthetic bearer lookup fixture
                    "producer_fixture",
                    "producer",
                    "fixture_tenant",
                )
            }
        ),
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Hospes-Tenant": "spoofed_tenant",
    }
    with TestClient(app) as client:
        unauthenticated = client.get("/v1/shows/fixture_show/suggestions")
        assert unauthenticated.status_code == 401
        response = client.get(
            "/v1/shows/fixture_show/suggestions",
            params={
                "relationship_class": "C2",
                "max_social_cost": 3,
                "limit": 1,
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["show_id"] == "fixture_show"
        assert response.json()[0]["source_provenance"].startswith(
            "Unlicensed Therapy public archive"
        )
        assert client.get(
            "/v1/shows/missing_show/suggestions", headers=headers
        ).json() == []
        assert client.get(
            "/v1/shows/fixture_show/suggestions",
            params={"relationship_class": "C5"},
            headers=headers,
        ).status_code == 422
        queue = client.get("/v1/approval-queue", headers=headers)
        assert queue.status_code == 200
        assert all(row["source_provenance"] for row in queue.json())

    canonical = Path("dashboard/assets/app.js").read_text(encoding="utf-8")
    packaged = Path("hospes/resources/dashboard/assets/app.js").read_text(
        encoding="utf-8"
    )
    assert canonical == packaged
    assert "['Provenance'" in canonical
    assert "Anthony warm intro" in canonical
    assert "escapeHTML(value)" in canonical


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_gap_years": -1}, "min_gap_years"),
        ({"max_social_cost": 0}, "max_social_cost"),
        ({"relationship_class": "C5"}, "relationship_class"),
        ({"limit": 0}, "limit"),
        ({"relationship_owner": "Not Opaque"}, "relationship_owner"),
        ({"as_of": date(2099, 1, 1)}, "as_of"),
    ],
)
def test_invalid_filters_fail_before_output(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    archive = _write_archive(tmp_path)
    with pytest.raises(suggest.SuggestionError, match=message):
        suggest.suggest_guests(archive_path=archive, **kwargs)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"not": "a list"}, "list of rows"),
        (["not an object"], "rows must be objects"),
        ([], "at least one appearance"),
        ([_archive_row("Bad Class", "2020-01-01", relationship_class="C5")], "C1, C2, or C3"),
        ([{**_archive_row("Unknown Field", "2020-01-01"), "extra": "x"}], "unsupported fields"),
        ([{**_archive_row("=Formula", "2020-01-01")}], "spreadsheet prefix"),
        ([{**_archive_row("No Date", "2020-01-01"), "date": "tomorrow"}], "ISO date"),
    ],
)
def test_malformed_archive_contract_fails_closed(
    tmp_path: Path, payload: object, message: str
) -> None:
    archive = tmp_path / "malformed.json"
    archive.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(suggest.SuggestionError, match=message):
        suggest.load_archive(archive)


def test_tour_contract_rejects_unattributed_or_unsafe_sources(tmp_path: Path) -> None:
    unattributed = tmp_path / "unattributed.json"
    unattributed.write_text(
        json.dumps(
            [
                {
                    "guest": "Public Guest",
                    "date": "2026-09-01",
                    "city": "Austin",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(suggest.SuggestionError, match="requires source"):
        suggest.load_tour_events(unattributed)

    bad_url = tmp_path / "bad-url.json"
    bad_url.write_text(
        json.dumps(
            [
                {
                    "guest": "Public Guest",
                    "date": "2026-09-01",
                    "city": "Austin",
                    "source_url": "https://tour.example/event?private=1",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(suggest.SuggestionError, match="without credentials"):
        suggest.load_tour_events(bad_url)

    unsupported = tmp_path / "tour.txt"
    unsupported.write_text("fixture", encoding="utf-8")
    with pytest.raises(suggest.SuggestionError, match="JSON or CSV"):
        suggest.load_tour_events(unsupported)


def test_conflicting_archive_classification_and_invalid_cost_fail_closed(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "conflicting.json"
    archive.write_text(
        json.dumps(
            [
                _archive_row(
                    "Conflicted Guest", "2020-01-01", relationship_class="C1"
                ),
                _archive_row(
                    "Conflicted Guest", "2021-01-01", relationship_class="C2"
                ),
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(suggest.SuggestionError, match="conflicting"):
        suggest.suggest_guests(
            archive_path=archive,
            as_of=AS_OF,
            relationship_class="C2",
        )
    with pytest.raises(suggest.SuggestionError, match="relationship_class"):
        suggest.estimate_social_cost("C5", 2)
    with pytest.raises(suggest.SuggestionError, match="negative"):
        suggest.estimate_social_cost("C2", -1)


def test_canonical_archive_and_documented_issue_contract_are_live(
    tmp_path: Path,
) -> None:
    rows = suggest.suggest_guests(
        as_of=AS_OF,
        min_gap_years=2,
        max_social_cost=3,
        relationship_class="C2",
        limit=500,
    )
    assert rows
    assert all(row.relationship_class == "C2" for row in rows)
    assert all(row.estimated_social_cost in {2, 3} for row in rows)
    payload = io.StringIO()
    suggest.write_csv(rows, payload)
    conn = store.connect(tmp_path / "canonical-import.sqlite3")
    imported = candidate_import.import_candidate_stream(
        conn,
        io.StringIO(payload.getvalue()),
        **BOUNDARY,
    )
    assert imported.created == len(rows)
    conn.close()
    documentation = Path("docs/guest-suggestions.md").read_text(encoding="utf-8")
    assert "hospes-suggestions-v1" in documentation
    assert "route_usable=false" in documentation
    assert "tests/issue_predicates/test_issue_19.py" in documentation
