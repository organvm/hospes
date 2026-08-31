"""HTTP contract coverage for every partnership and Pilot route."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hospes.api import create_app
from conftest import synthetic_bearer_authenticator

TOKEN = "synthetic-partnership-api-token"  # allow-secret: inert test fixture
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Hospes-Actor": "producer_fixture",
    "X-Hospes-Role": "producer",
    "X-Hospes-Tenant": "fixture_tenant",
}


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(  # allow-secret: inert test fixture
        str(tmp_path / "partnership-api.sqlite3"),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {TOKEN: ("producer_fixture", "producer", "fixture_tenant")}  # allow-secret: inert test fixture
        ),
        csrf_required=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_partnership_list_requires_authentication(client: TestClient) -> None:
    assert client.get("/v1/partnerships").status_code == 401


def test_partnership_list_returns_an_empty_tenant_scope(client: TestClient) -> None:
    response = client.get("/v1/partnerships", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == []


def test_command_center_reports_missing_partnership(client: TestClient) -> None:
    response = client.get(
        "/v1/partnerships/missing/command-center", headers=HEADERS
    )
    assert response.status_code == 404


def test_create_item_validates_payload_before_lookup(client: TestClient) -> None:
    response = client.post(
        "/v1/partnerships/missing/items", json={}, headers=HEADERS
    )
    assert response.status_code == 422


def test_revise_item_reports_missing_partnership(client: TestClient) -> None:
    response = client.put(
        "/v1/partnerships/missing/items/missing",
        json={
            "item_key": "fixture.item",
            "category": "decision",
            "title": "Synthetic decision",
            "summary": "A bounded synthetic decision used only for API validation.",
            "owner": "Partners",
            "state": "unknown",
            "external_reference": "fixture://partnership/item",
            "expected_revision": 1,
        },
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_supersede_item_reports_missing_partnership(client: TestClient) -> None:
    response = client.post(
        "/v1/partnerships/missing/items/missing/supersede",
        json={"expected_revision": 1},
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_link_resource_reports_missing_partnership(client: TestClient) -> None:
    response = client.post(
        "/v1/partnerships/missing/resources",
        json={
            "resource_kind": "brief",
            "label": "Synthetic brief",
            "opaque_reference": "fixture://partnership/brief",
        },
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_record_review_reports_missing_partnership(client: TestClient) -> None:
    response = client.post(
        "/v1/partnerships/missing/reviews",
        json={
            "review_kind": "partner_review",
            "summary": "Synthetic review receipt.",
            "opaque_reference": "fixture://partnership/review",
        },
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_select_pilot_slot_reports_missing_partnership(client: TestClient) -> None:
    response = client.put(
        "/v1/partnerships/missing/pilot-slots/1",
        json={"opportunity_id": "missing"},
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_start_pilot_run_reports_missing_partnership(client: TestClient) -> None:
    response = client.post(
        "/v1/partnerships/missing/pilot-runs", json={}, headers=HEADERS
    )
    assert response.status_code == 404


def test_get_pilot_plan_reports_missing_run(client: TestClient) -> None:
    response = client.get(
        "/v1/partnerships/missing/pilot-runs/missing/plan", headers=HEADERS
    )
    assert response.status_code == 404


def test_pilot_decision_validates_payload(client: TestClient) -> None:
    response = client.post(
        "/v1/partnerships/missing/pilot-runs/missing/decisions",
        json={},
        headers=HEADERS,
    )
    assert response.status_code == 422
