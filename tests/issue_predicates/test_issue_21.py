"""Executable completion predicate for HOSPES issue #21."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import synthetic_bearer_authenticator
from hospes import authentication, candidate_import, encryption, migrations
from hospes import partnerships, service, store, touchpoints
from hospes.api import create_app


UTC = timezone.utc
NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
MASTER_REF = "credential://hospes/master-key"
MASTER_KEY = bytes(reversed(range(32)))
TENANT = "fixture_tenant"
SHOW = "fixture_show"
OTHER_SHOW = "fixture_show_two"
PARTNERSHIP_ID = "partnership-fixture-21"
PRIVATE_NOTE = "Synthetic private note: maybe, will check the schedule."


def _vault() -> encryption.FieldVault:
    provider = encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY})
    return encryption.FieldVault(encryption.TenantKeyManager(provider, master_key_ref=MASTER_REF))


def _candidate(source_key: str, guest_name: str = "Synthetic Touchpoint Guest") -> dict[str, object]:
    return {
        "source_key": source_key,
        "guest_name": guest_name,
        "why_guest": "This synthetic guest proves attributable informal touchpoint behavior.",
        "why_now": "The issue predicate requires a bounded private-note fixture now.",
        "episode_thesis": "Informal conversations should remain useful without becoming workflow authority.",
        "proposed_artifact": "An encrypted informal touchpoint receipt",
        "relationship_class": "C2",
        "relationship_owner": "relationship_fixture",
        "route_type": "producer_system",
        "route_reference": "producer://fixture/route-21",
        "route_verified_at": "2026-08-10T12:00:00+00:00",
        "preferred_city": "Los Angeles",
        "social_cost_1_5": "2",
        "ari_effort": "review_only",
        "next_action": "Review this synthetic guest in the workbench.",
        "source_provenance": "Synthetic issue predicate fixture.",
    }


def _import_candidate(
    conn,
    *,
    show_id: str = SHOW,
    source_key: str = "crm:synthetic:issue-21",
    guest_name: str = "Synthetic Touchpoint Guest",
) -> str:
    result = candidate_import.import_candidates(
        conn,
        [_candidate(source_key, guest_name)],
        tenant_id=TENANT,
        network_id="fixture_network",
        show_id=show_id,
        actor_id="producer_fixture",
        now=NOW,
    )
    return result.created_ids[0]


def _fixture(tmp_path: Path):
    database = tmp_path / "touchpoints.sqlite3"
    conn = store.connect(database)
    opportunity_id = _import_candidate(conn)
    store.insert(
        conn,
        "partnerships",
        {
            "id": PARTNERSHIP_ID,
            "tenant_id": TENANT,
            "show_id": SHOW,
            "partnership_key": "synthetic-touchpoint-partnership",
            "label": "Synthetic touchpoint partnership",
            "purpose": "Exercise encrypted informational conversation evidence.",
            "status": "active",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        },
    )
    conn.commit()
    return database, conn, _vault(), opportunity_id


def _payload(
    opportunity_id: str,
    *,
    channel: str = "text",
    notes: str = PRIVATE_NOTE,
    occurred_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "guest_id": "crm:synthetic:issue-21",
        "opportunity_id": opportunity_id,
        "partnership_id": PARTNERSHIP_ID,
        "channel": channel,
        "notes": notes,
        "occurred_at": (occurred_at or NOW - timedelta(hours=1)).isoformat(),
    }


def _record(conn, vault, opportunity_id: str, **overrides):
    payload = {**_payload(opportunity_id), **overrides}
    return partnerships.record_informal_touchpoint(
        conn,
        payload,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_id="ari_fixture",
        actor_role="relationship_owner",
        field_vault=vault,
        now=NOW,
    )


def test_encrypted_touchpoint_is_attributable_idempotent_and_informational(
    tmp_path: Path,
) -> None:
    database, conn, vault, opportunity_id = _fixture(tmp_path)
    before = store.fetch_one(
        conn, "SELECT status, disposition, updated_at FROM appearance_opportunities WHERE id = ?", (opportunity_id,)
    )
    item = _record(conn, vault, opportunity_id)
    assert item["kind"] == "informal_touchpoint"
    assert item["notes"] == PRIVATE_NOTE
    assert item["notes_available"] is True
    assert item["initiator"] == "ari_fixture"
    assert item["initiator_role"] == "relationship_owner"
    assert item["notes_ref"].startswith("private-field://")

    stored = store.fetch_one(conn, "SELECT * FROM touchpoint_receipts WHERE id = ?", (item["touchpoint_id"],))
    assert stored is not None
    assert stored["notes_ref"] == item["notes_ref"]
    assert stored["notes_checksum"] and len(stored["notes_checksum"]) == 64
    assert "notes" not in stored
    assert PRIVATE_NOTE.encode() not in database.read_bytes()
    assert (
        store.fetch_one(
            conn, "SELECT status, disposition, updated_at FROM appearance_opportunities WHERE id = ?", (opportunity_id,)
        )
        == before
    )

    audit = store.fetch_one(
        conn,
        "SELECT * FROM partnership_audit_events WHERE event_type = 'touchpoint.recorded'",
    )
    assert audit is not None
    assert audit["actor_id"] == "ari_fixture"
    assert audit["actor_role"] == "relationship_owner"
    assert audit["show_id"] == SHOW
    assert audit["details"] == {
        "touchpoint_id": item["touchpoint_id"],
        "guest_id": "crm:synthetic:issue-21",
        "channel": "text",
        "occurred_at": (NOW - timedelta(hours=1)).isoformat(),
    }
    assert PRIVATE_NOTE not in json.dumps(audit)
    center = partnerships.command_center(conn, PARTNERSHIP_ID, TENANT)
    assert center["recent_events"][0]["event_type"] == "touchpoint.recorded"

    exact_retry = _record(conn, vault, opportunity_id)
    assert exact_retry["touchpoint_id"] == item["touchpoint_id"]
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM touchpoint_receipts")["count"] == 1
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM private_field_values")["count"] == 1
    assert (
        store.fetch_one(
            conn, "SELECT COUNT(*) AS count FROM partnership_audit_events WHERE event_type = 'touchpoint.recorded'"
        )["count"]
        == 1
    )
    with pytest.raises(touchpoints.TouchpointError, match="different evidence"):
        _record(conn, vault, opportunity_id, notes="A different synthetic note.")
    conn.close()


def test_validation_filters_roles_and_ciphertext_scope_fail_closed(tmp_path: Path) -> None:
    _, conn, vault, opportunity_id = _fixture(tmp_path)
    first = _record(conn, vault, opportunity_id)
    second = partnerships.record_informal_touchpoint(
        conn,
        _payload(
            opportunity_id,
            channel="phone",
            notes="Synthetic follow-up call.",
            occurred_at=NOW - timedelta(minutes=20),
        ),
        tenant_id=TENANT,
        show_id=SHOW,
        actor_id="producer_fixture",
        actor_role="producer",
        field_vault=vault,
        now=NOW,
    )
    assert second["initiator"] == "producer_fixture"
    assert [
        item["channel"]
        for item in touchpoints.list_touchpoints(
            conn, vault, tenant_id=TENANT, show_id=SHOW, actor_role="producer", channel="phone"
        )
    ] == ["phone"]
    assert [
        item["touchpoint_id"]
        for item in touchpoints.list_touchpoints(
            conn, vault, tenant_id=TENANT, show_id=SHOW, actor_role="producer", initiator="ari_fixture"
        )
    ] == [first["touchpoint_id"]]
    ranged = touchpoints.list_touchpoints(
        conn,
        vault,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        from_at=(NOW - timedelta(minutes=30)).isoformat(),
        to_at=NOW.isoformat(),
    )
    assert [item["touchpoint_id"] for item in ranged] == [second["touchpoint_id"]]
    assert touchpoints.list_touchpoints(conn, vault, tenant_id=TENANT, show_id=OTHER_SHOW, actor_role="producer") == []

    for invalid, message in (
        ({**_payload(opportunity_id), "initiator": "spoofed"}, "authenticated operator"),
        ({**_payload(opportunity_id), "channel": "dm"}, "channel"),
        ({**_payload(opportunity_id), "notes": ""}, "notes are required"),
        ({**_payload(opportunity_id), "occurred_at": "2026-08-10T17:00:00"}, "timezone"),
        ({**_payload(opportunity_id), "occurred_at": (NOW + timedelta(seconds=1)).isoformat()}, "future"),
    ):
        with pytest.raises(touchpoints.TouchpointError, match=message):
            touchpoints.TouchpointInput.from_mapping(invalid, now=NOW)
    with pytest.raises(touchpoints.TouchpointError, match="from_at"):
        touchpoints.list_touchpoints(
            conn,
            vault,
            tenant_id=TENANT,
            show_id=SHOW,
            actor_role="producer",
            from_at=NOW.isoformat(),
            to_at=(NOW - timedelta(days=1)).isoformat(),
        )
    with pytest.raises(touchpoints.TouchpointError, match="limit"):
        touchpoints.list_touchpoints(conn, vault, tenant_id=TENANT, show_id=SHOW, actor_role="producer", limit=True)
    with pytest.raises(touchpoints.TouchpointError, match="channel"):
        touchpoints.list_touchpoints(conn, vault, tenant_id=TENANT, show_id=SHOW, actor_role="producer", channel="dm")
    with pytest.raises(touchpoints.TouchpointError, match="cannot view"):
        touchpoints.list_touchpoints(conn, vault, tenant_id=TENANT, show_id=SHOW, actor_role="network_operator")
    with pytest.raises(touchpoints.TouchpointError, match="cannot record"):
        partnerships.record_informal_touchpoint(
            conn,
            _payload(opportunity_id),
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="network_fixture",
            actor_role="network_operator",
            field_vault=vault,
            now=NOW,
        )

    row = store.fetch_one(conn, "SELECT * FROM touchpoint_receipts WHERE id = ?", (first["touchpoint_id"],))
    assert row is not None
    wrong_scope = {**row, "show_id": OTHER_SHOW}
    with pytest.raises(touchpoints.TouchpointError, match="unavailable"):
        touchpoints.reveal_touchpoint(conn, vault, wrong_scope, actor_role="producer")
    store.update(conn, "touchpoint_receipts", first["touchpoint_id"], {"notes_checksum": "0" * 64})
    conn.commit()
    changed = store.fetch_one(conn, "SELECT * FROM touchpoint_receipts WHERE id = ?", (first["touchpoint_id"],))
    with pytest.raises(touchpoints.TouchpointError, match="checksum"):
        touchpoints.reveal_touchpoint(conn, vault, changed, actor_role="producer")
    conn.close()


def test_parser_guest_lookup_legacy_projection_and_write_failure_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, conn, vault, opportunity_id = _fixture(tmp_path)
    base = _payload(opportunity_id)
    for invalid, message in (
        ([], "must be an object"),
        ({**base, "unsupported": "value"}, "unsupported fields"),
        ({key: value for key, value in base.items() if key not in {"guest_id", "opportunity_id"}}, "is required"),
        ({**base, "guest_id": 42}, "opaque identifier"),
        ({**base, "opportunity_id": "bad/id"}, "opaque identifier"),
        ({**base, "notes": 42}, "notes must be text"),
        ({**base, "notes": "x" * 2_001}, "exceed"),
        ({**base, "notes": "bad\x00note"}, "control"),
        ({**base, "occurred_at": 42}, "ISO-8601"),
        ({**base, "occurred_at": "not-a-date"}, "ISO-8601"),
    ):
        with pytest.raises(touchpoints.TouchpointError, match=message):
            touchpoints.TouchpointInput.from_mapping(invalid, now=NOW)

    guest_only = {**base, "occurred_at": (NOW - timedelta(minutes=10)).isoformat()}
    guest_only.pop("opportunity_id")
    guest_only.pop("partnership_id")
    recorded = partnerships.record_informal_touchpoint(
        conn,
        guest_only,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_id="producer_fixture",
        actor_role="producer",
        field_vault=vault,
        now=NOW,
    )
    assert recorded["opportunity_id"] == opportunity_id
    assert recorded["partnership_id"] is None
    assert (
        store.fetch_one(
            conn, "SELECT COUNT(*) AS count FROM partnership_audit_events WHERE event_type = 'touchpoint.recorded'"
        )["count"]
        == 0
    )
    with pytest.raises(touchpoints.TouchpointError, match="does not match"):
        partnerships.record_informal_touchpoint(
            conn,
            {**base, "guest_id": "crm:synthetic:different"},
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            field_vault=vault,
            now=NOW,
        )
    with pytest.raises(touchpoints.TouchpointError, match="partnership not found"):
        partnerships.record_informal_touchpoint(
            conn,
            {**base, "partnership_id": "partnership-missing"},
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            field_vault=vault,
            now=NOW,
        )

    legacy = {
        "id": "legacy-touchpoint-21",
        "tenant_id": TENANT,
        "show_id": SHOW,
        "guest_id": "crm:synthetic:legacy-21",
        "opportunity_id": None,
        "partnership_id": None,
        "channel": "hallway",
        "notes_ref": "vault://legacy/touchpoint-21",
        "notes_checksum": None,
        "occurred_at": (NOW - timedelta(days=1)).isoformat(),
        "initiator": "legacy_fixture",
        "initiator_role": "legacy",
        "correlation_id": "touchpoint://legacy-touchpoint-21",
        "created_at": (NOW - timedelta(days=1)).isoformat(),
    }
    store.insert(conn, "touchpoint_receipts", legacy)
    conn.commit()
    projected = touchpoints.reveal_touchpoint(conn, vault, legacy, actor_role="producer")
    assert projected["notes"] is None
    assert projected["notes_available"] is False
    with pytest.raises(touchpoints.TouchpointError, match="cannot view"):
        touchpoints.reveal_touchpoint(conn, vault, legacy, actor_role="network_operator")

    real_insert = store.insert

    def fail_touchpoint(connection, table, values):
        if table == "touchpoint_receipts":
            raise RuntimeError("synthetic database rejection")
        return real_insert(connection, table, values)

    monkeypatch.setattr(touchpoints.store, "insert", fail_touchpoint)
    with pytest.raises(touchpoints.TouchpointError, match="write was rejected"):
        partnerships.record_informal_touchpoint(
            conn,
            {**base, "occurred_at": (NOW - timedelta(minutes=5)).isoformat()},
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="editor_fixture",
            actor_role="editorial_owner",
            field_vault=vault,
            now=NOW,
        )
    conn.close()


def test_api_create_list_filter_identity_and_cross_show_scope(tmp_path: Path) -> None:
    database, conn, vault, opportunity_id = _fixture(tmp_path)
    other_opportunity_id = _import_candidate(
        conn,
        show_id=OTHER_SHOW,
        source_key="crm:synthetic:issue-21-other",
        guest_name="Synthetic Other Show Guest",
    )
    conn.commit()
    conn.close()
    producer_token = "synthetic-issue-21-producer-token"  # allow-secret: fixture
    network_token = "synthetic-issue-21-network-token"  # allow-secret: fixture
    other_token = "synthetic-issue-21-other-token"  # allow-secret: fixture
    app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        csrf_secret=b"synthetic-csrf-secret-issue-21-32",  # allow-secret: fixture
        field_vault=vault,
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {
                producer_token: ("producer_fixture", "producer", TENANT),
                network_token: ("network_fixture", "network_operator", TENANT),
                other_token: ("producer_other", "producer", "other_tenant"),
            }
        ),
    )
    producer_headers = {"Authorization": f"Bearer {producer_token}"}
    network_headers = {"Authorization": f"Bearer {network_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    with TestClient(app) as client:
        context = client.get("/v1/operator-context", headers=producer_headers)
        assert context.json()["private_field_custody_configured"] is True
        spoofed = client.post(
            f"/v1/shows/{SHOW}/touchpoints",
            headers=producer_headers,
            json={**_payload(opportunity_id), "initiator": "spoofed_actor"},
        )
        assert spoofed.status_code == 422
        assert "authenticated operator" in spoofed.json()["detail"]
        created = client.post(
            f"/v1/shows/{SHOW}/touchpoints",
            headers=producer_headers,
            json=_payload(opportunity_id),
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store, private"
        assert created.headers["pragma"] == "no-cache"
        assert created.json()["initiator"] == "producer_fixture"
        assert created.json()["notes"] == PRIVATE_NOTE

        listing = client.get(
            f"/v1/shows/{SHOW}/touchpoints",
            headers=producer_headers,
            params={"channel": "text", "initiator": "producer_fixture"},
        )
        assert listing.status_code == 200
        assert listing.headers["cache-control"] == "no-store, private"
        assert [item["touchpoint_id"] for item in listing.json()] == [created.json()["touchpoint_id"]]
        guest_timeline = client.get(
            f"/v1/opportunities/{opportunity_id}/touchpoints",
            headers=producer_headers,
        )
        assert guest_timeline.status_code == 200
        assert guest_timeline.json()[0]["guest_name"] == "Synthetic Touchpoint Guest"
        assert client.get(f"/v1/shows/{SHOW}/touchpoints", headers=network_headers).status_code == 403
        assert (
            client.post(
                f"/v1/shows/{SHOW}/touchpoints",
                headers=other_headers,
                json=_payload(opportunity_id),
            ).status_code
            == 404
        )

    class ScopedAuthenticator:
        def authenticate(self, conn, assertion, requested_show=None):
            del conn, assertion, requested_show
            return authentication.AuthenticatedOperator(
                actor_id="producer_fixture",
                role=service.HumanRole.PRODUCER,
                tenant_id=TENANT,
                show_id=SHOW,
                provider="synthetic_show_scope",
                subject_digest="0" * 64,
            )

    scoped_app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        csrf_secret=b"synthetic-csrf-secret-issue-21-32",  # allow-secret: fixture
        field_vault=vault,
        access_authenticator=ScopedAuthenticator(),
    )
    with TestClient(scoped_app) as scoped_client:
        assert scoped_client.get(f"/v1/shows/{SHOW}/touchpoints").status_code == 200
        assert scoped_client.get(f"/v1/shows/{OTHER_SHOW}/touchpoints").status_code == 403
        assert (
            scoped_client.post(
                f"/v1/shows/{OTHER_SHOW}/touchpoints",
                json={
                    **_payload(other_opportunity_id),
                    "guest_id": "crm:synthetic:issue-21-other",
                },
            ).status_code
            == 403
        )
        assert scoped_client.get(f"/v1/opportunities/{other_opportunity_id}/touchpoints").status_code == 403

    unconfigured = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        csrf_secret=b"synthetic-csrf-secret-issue-21-32",  # allow-secret: fixture
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {producer_token: ("producer_fixture", "producer", TENANT)}
        ),
    )
    with TestClient(unconfigured) as client:
        assert (
            client.get("/v1/operator-context", headers=producer_headers).json()["private_field_custody_configured"]
            is False
        )
        assert client.get(f"/v1/shows/{SHOW}/touchpoints", headers=producer_headers).status_code == 503


def test_migration_schema_receipt_contract_dashboard_and_docs_are_complete(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "schema.sqlite3")
    ledger = {row["version"]: row["name"] for row in migrations.applied_migrations(conn)}
    assert ledger[13] == "encrypted_informal_touchpoints"
    assert ledger[14] == "multi_show_partnership_and_pilot_scope"
    assert migrations.current_version(conn) == migrations.LATEST_VERSION
    columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(touchpoint_receipts)")}
    assert {
        "opportunity_id",
        "partnership_id",
        "initiator_role",
        "correlation_id",
        "notes_checksum",
    } <= columns
    indexes = {row["name"] for row in store.fetch_all(conn, "PRAGMA index_list(touchpoint_receipts)")}
    assert {
        "ux_touchpoint_scope_id",
        "ux_touchpoint_correlation",
        "ix_touchpoint_scope_timeline",
        "ix_touchpoint_guest_timeline",
        "ix_touchpoint_initiator_timeline",
        "ix_touchpoint_partnership_timeline",
    } <= indexes
    conn.close()

    schema = json.loads(Path("spec/receipt.schema.json").read_text())
    informal_branch = next(
        branch["then"]
        for branch in schema["allOf"]
        if branch["if"]["properties"]["kind"]["const"] == "informal_touchpoint"
    )
    assert {"guest_id", "opportunity_id", "notes_ref", "initiator_role"} <= set(informal_branch["required"])
    assert informal_branch["properties"]["notes_ref"]["pattern"].startswith("^private-field://")
    for relative in (
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/partnership.js",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/partnership-shell.js",
        "dashboard/assets/styles.css",
        "config/domain_kernel.yaml",
        "spec/receipt.schema.json",
    ):
        assert Path(relative).read_bytes() == (Path("hospes/resources") / relative).read_bytes()

    html = "\n".join(
        Path(relative).read_text()
        for relative in (
            "dashboard/index.html",
            "dashboard/assets/partnership-workspace.html",
        )
    )
    javascript = Path("dashboard/assets/partnership-workspace.js").read_text()
    api_javascript = Path("dashboard/assets/api.js").read_text()
    capabilities = Path("dashboard/assets/capabilities.mjs").read_text()
    assert 'id="btn-log-touchpoint"' in html
    assert 'id="touchpoint-dialog"' in html
    assert 'id="guest-touchpoint-timeline"' in html
    assert "The authenticated operator becomes the initiator" in html
    assert "saveTouchpoint(opportunity.show_id, payload)" in javascript
    assert "escapeHTML(item.notes" in javascript
    assert "escapeHTML(touchpoint.notes" in javascript
    assert "loadTouchpoints" in api_javascript
    assert "canLogTouchpoint" in capabilities
    assert "private_field_custody_configured" in capabilities

    documentation = Path("docs/informal-touchpoints.md").read_text()
    for phrase in (
        "AES-256-GCM",
        "informational evidence only",
        "authenticated operator",
        "Cache-Control: no-store",
        "does not advance",
        "tenant/show scope",
    ):
        assert phrase in documentation
    domain = Path("config/domain_kernel.yaml").read_text()
    assert "touchpoint_contract:" in domain
    assert "never advance opportunity or Pilot state" in domain
    assert "Initiator id and role come only from the authenticated operator session" in domain


def test_transaction_rolls_back_first_key_private_value_receipt_and_audit(tmp_path: Path) -> None:
    _, conn, _, opportunity_id = _fixture(tmp_path)

    class FailingVault(encryption.FieldVault):
        def put_text(self, conn, scope, value, *, commit=True):
            super().put_text(conn, scope, value, commit=commit)
            raise encryption.EncryptionConfigurationError("synthetic custody outage")

    failing = FailingVault(_vault().keys)
    with pytest.raises(touchpoints.TouchpointError, match="encryption is unavailable"):
        partnerships.record_informal_touchpoint(
            conn,
            _payload(opportunity_id),
            tenant_id=TENANT,
            show_id=SHOW,
            actor_id="producer_fixture",
            actor_role="producer",
            field_vault=failing,
            now=NOW,
        )
    for table in (
        "tenant_encryption_keys",
        "private_field_values",
        "touchpoint_receipts",
        "partnership_audit_events",
    ):
        assert store.fetch_one(conn, f"SELECT COUNT(*) AS count FROM {table}")["count"] == 0
    conn.close()
