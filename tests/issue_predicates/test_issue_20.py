"""Executable completion predicate for HOSPES issue #20."""

from __future__ import annotations

import base64
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import synthetic_bearer_authenticator
from hospes import authentication, candidate_import, contact_roster, encryption
from hospes import migrations, service, store
from hospes.__main__ import main
from hospes.api import create_app


UTC = timezone.utc
NOW = datetime(2026, 8, 10, 16, 0, tzinfo=UTC)
MASTER_REF = "credential://hospes/master-key"
MASTER_KEY = bytes(range(32))
BOUNDARY = {
    "tenant_id": "fixture_tenant",
    "network_id": "fixture_network",
    "show_id": "fixture_show",
    "actor_id": "producer_fixture",
}
PRIVATE_VALUES = {
    "name": "Synthetic Publicist",
    "email": "publicist@example.test",
    "phone": "+1 555 010 0200",
    "notes": "Synthetic test-only contact note.",
}


def _vault() -> encryption.FieldVault:
    provider = encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY})
    return encryption.FieldVault(
        encryption.TenantKeyManager(provider, master_key_ref=MASTER_REF)
    )


def _row(source_key: str = "crm:synthetic:issue-20") -> dict[str, object]:
    return {
        "source_key": source_key,
        "guest_name": "Synthetic Roster Candidate",
        "why_guest": "This synthetic candidate proves private roster custody behavior.",
        "why_now": "The issue predicate needs an attributable bounded test fixture now.",
        "episode_thesis": "Private contact routes should be useful without becoming public data.",
        "proposed_artifact": "An encrypted route receipt",
        "relationship_class": "C2",
        "relationship_owner": "relationship_fixture",
        "route_type": "producer_system",
        "route_reference": "producer://fixture/route-20",
        "route_verified_at": "2026-08-10T12:00:00+00:00",
        "preferred_city": "Los Angeles",
        "social_cost_1_5": "2",
        "ari_effort": "review_only",
        "next_action": "Review the synthetic candidate in the operator workbench.",
        "source_provenance": "Opaque fixture record from the synthetic test CRM.",
    }


def _roster(
    *,
    permission_status: str = "permitted",
    usable: bool = True,
    preferred: bool = True,
    notes: str = PRIVATE_VALUES["notes"],
) -> str:
    return json.dumps(
        {
            "publicist": {
                **PRIVATE_VALUES,
                "notes": notes,
                "provenance_ref": "roster://fixture/publicist-20",
                "verified_at": "2026-08-10T12:00:00+00:00",
                "usable": usable,
                "preferred": preferred,
                "permission_status": permission_status,
            },
            "manager": {
                "name": "Synthetic Manager",
                "provenance_ref": "roster://fixture/manager-20",
                "permission_status": "pending_verification",
            },
        },
        sort_keys=True,
    )


def _import(
    conn: store.DatabaseConnection,
    *,
    row: dict[str, object] | None = None,
    vault: encryption.FieldVault | None = None,
    now: datetime = NOW,
    **boundary: str,
) -> candidate_import.CandidateImportResult:
    payload = row or {**_row(), "contact_roster": _roster()}
    return candidate_import.import_candidates(
        conn,
        [payload],
        field_vault=vault or _vault(),
        now=now,
        **(boundary or BOUNDARY),
    )


def test_private_input_and_public_reference_schemas_are_packaged() -> None:
    candidate = json.loads(Path("spec/candidate.schema.json").read_text())
    appearance = json.loads(
        Path("spec/appearance_opportunity.schema.json").read_text()
    )
    public_route = json.loads(Path("spec/contact_route.schema.json").read_text())

    assert candidate["x-hospes-surface"] == "private-import-only"
    assert candidate["x-hospes-custody"] == "encrypt-before-persistence"
    assert set(candidate["definitions"]["contactRoster"]["properties"]) == {
        "publicist",
        "manager",
        "agent",
        "direct",
    }
    fields = candidate["definitions"]["contactRosterEntry"]["properties"]
    assert {"name", "email", "phone", "notes", "provenance_ref"} <= set(fields)

    projection = appearance["properties"]["contact_roster_refs"]["items"]
    assert projection["additionalProperties"] is False
    assert {"name_ref", "route_ref", "notes_ref"} <= set(projection["properties"])
    assert {"name", "email", "phone", "notes"}.isdisjoint(
        projection["properties"]
    )
    assert public_route["additionalProperties"] is False
    assert {"contact_person", "address", "source", "notes"}.isdisjoint(
        public_route["properties"]
    )
    assert {"contact_person_ref", "address_ref", "source_ref", "notes_ref"} <= set(
        public_route["properties"]
    )

    for relative in (
        "candidate.schema.json",
        "appearance_opportunity.schema.json",
        "contact_route.schema.json",
    ):
        assert (Path("spec") / relative).read_bytes() == (
            Path("hospes/resources/spec") / relative
        ).read_bytes()


def test_migration_twelve_has_composite_scope_and_foreign_keys(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "migration.sqlite3")
    assert migrations.current_version(conn) == migrations.LATEST_VERSION
    assert migrations.LATEST_VERSION >= 12
    assert any(
        item["version"] == 12 and item["name"] == "encrypted_contact_rosters"
        for item in migrations.applied_migrations(conn)
    )
    indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(appearance_opportunities)")
    }
    assert "ux_opp_tenant_show_source" in indexes
    assert "ux_opp_tenant_source" not in indexes
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(contact_rosters)")
    }
    assert {
        "tenant_id",
        "show_id",
        "opportunity_id",
        "role_kind",
        "name_ref",
        "email_ref",
        "phone_ref",
        "notes_ref",
        "provenance_ref",
        "verified_at",
        "usable",
        "preferred",
        "permission_status",
    } <= columns
    foreign_keys = list(conn.execute("PRAGMA foreign_key_list(contact_rosters)"))
    assert {row[2] for row in foreign_keys} == {
        "appearance_opportunities",
        "show_registry",
    }

    created = _import(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO contact_rosters "
            "(id, tenant_id, show_id, opportunity_id, role_kind, name_ref, "
            "provenance_ref, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "wrong-scope",
                BOUNDARY["tenant_id"],
                "other_show",
                created.created_ids[0],
                "direct",
                "private-field://00000000-0000-4000-8000-000000000001",
                "roster://fixture/wrong-scope",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    conn.close()


def test_import_encrypts_each_value_and_exposes_only_opaque_public_refs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "roster.sqlite3"
    conn = store.connect(path)
    vault = _vault()
    result = _import(conn, vault=vault)
    conn.commit()
    opportunity_id = result.created_ids[0]

    stored = store.fetch_one(
        conn,
        "SELECT * FROM contact_rosters WHERE opportunity_id = ? AND role_kind = ?",
        (opportunity_id, "publicist"),
    )
    assert stored is not None
    assert all(
        str(stored[f"{field_name}_ref"]).startswith("private-field://")
        for field_name in ("name", "email", "phone", "notes")
    )
    persisted = json.dumps(stored, sort_keys=True)
    assert all(value not in persisted for value in PRIVATE_VALUES.values())

    public = contact_roster.public_contact_roster(
        conn,
        tenant_id=BOUNDARY["tenant_id"],
        show_id=BOUNDARY["show_id"],
        opportunity_id=opportunity_id,
    )
    public_payload = json.dumps(public, sort_keys=True)
    assert all(value not in public_payload for value in PRIVATE_VALUES.values())
    assert {"name", "email", "phone", "notes"}.isdisjoint(public[0])

    revealed = contact_roster.reveal_contact_roster(
        conn,
        vault,
        tenant_id=BOUNDARY["tenant_id"],
        show_id=BOUNDARY["show_id"],
        opportunity_id=opportunity_id,
        actor_role="producer",
    )
    publicist = next(item for item in revealed if item["role"] == "publicist")
    assert {key: publicist[key] for key in PRIVATE_VALUES} == PRIVATE_VALUES
    assert contact_roster.invitation_prefill(revealed) == {
        "role": "publicist",
        "name": PRIVATE_VALUES["name"],
        "route_kind": "email",
        "route_value": PRIVATE_VALUES["email"],
        "route_ref": publicist["route_ref"],
        "provenance_ref": "roster://fixture/publicist-20",
        "verified_at": "2026-08-10T12:00:00+00:00",
    }
    conn.close()
    database_bytes = path.read_bytes()
    assert all(value.encode() not in database_bytes for value in PRIVATE_VALUES.values())


def test_same_private_source_is_isolated_between_shows(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "show-scope.sqlite3")
    vault = _vault()
    first = _import(conn, vault=vault)
    second = _import(
        conn,
        vault=vault,
        show_id="fixture_show_two",
        **{key: value for key, value in BOUNDARY.items() if key != "show_id"},
    )
    assert first.created_ids[0] != second.created_ids[0]
    rows = store.fetch_all(
        conn,
        "SELECT show_id, COUNT(*) AS count FROM contact_rosters GROUP BY show_id "
        "ORDER BY show_id",
    )
    assert rows == [
        {"show_id": "fixture_show", "count": 2},
        {"show_id": "fixture_show_two", "count": 2},
    ]
    assert contact_roster.reveal_contact_roster(
        conn,
        vault,
        tenant_id=BOUNDARY["tenant_id"],
        show_id=BOUNDARY["show_id"],
        opportunity_id=second.created_ids[0],
        actor_role="producer",
    ) == []
    conn.close()


def test_legacy_same_show_identity_is_reused_without_orphaning_routes(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "legacy-id.sqlite3")
    created = candidate_import.import_candidates(conn, [_row()], now=NOW, **BOUNDARY)
    generated_id = created.created_ids[0]
    conn.execute(
        "DELETE FROM contact_routes WHERE opportunity_id = ?", (generated_id,)
    )
    conn.execute("DELETE FROM audit_events WHERE opportunity_id = ?", (generated_id,))
    conn.execute(
        "UPDATE appearance_opportunities SET id = ? WHERE id = ?",
        ("legacy-opportunity-20", generated_id),
    )
    conn.commit()

    updated = _import(conn, vault=_vault(), now=NOW + timedelta(hours=1))
    assert updated.updated_ids == ["legacy-opportunity-20"]
    assert store.fetch_one(conn, "SELECT opportunity_id FROM contact_routes") == {
        "opportunity_id": "legacy-opportunity-20"
    }
    assert {
        row["opportunity_id"]
        for row in store.fetch_all(conn, "SELECT opportunity_id FROM contact_rosters")
    } == {"legacy-opportunity-20"}
    conn.close()


def test_reveal_is_role_tenant_show_and_aad_scoped(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "scope.sqlite3")
    vault = _vault()
    opportunity_id = _import(conn, vault=vault).created_ids[0]

    with pytest.raises(contact_roster.ContactRosterError) as denied:
        contact_roster.reveal_contact_roster(
            conn,
            vault,
            tenant_id=BOUNDARY["tenant_id"],
            show_id=BOUNDARY["show_id"],
            opportunity_id=opportunity_id,
            actor_role="network_operator",
        )
    assert denied.value.status_code == 403
    assert contact_roster.reveal_contact_roster(
        conn,
        vault,
        tenant_id="other_tenant",
        show_id=BOUNDARY["show_id"],
        opportunity_id=opportunity_id,
        actor_role="producer",
    ) == []
    assert contact_roster.reveal_contact_roster(
        conn,
        vault,
        tenant_id=BOUNDARY["tenant_id"],
        show_id="other_show",
        opportunity_id=opportunity_id,
        actor_role="producer",
    ) == []

    row = store.fetch_one(
        conn,
        "SELECT id, email_ref FROM contact_rosters WHERE opportunity_id = ? "
        "AND role_kind = 'publicist'",
        (opportunity_id,),
    )
    store.update(conn, "contact_rosters", row["id"], {"name_ref": row["email_ref"]})
    with pytest.raises(contact_roster.ContactRosterError) as replay:
        contact_roster.reveal_contact_roster(
            conn,
            vault,
            tenant_id=BOUNDARY["tenant_id"],
            show_id=BOUNDARY["show_id"],
            opportunity_id=opportunity_id,
            actor_role="relationship_owner",
        )
    assert replay.value.status_code == 503
    assert PRIVATE_VALUES["email"] not in str(replay.value)
    conn.close()


def test_exact_reimport_is_write_free_and_changed_fields_are_audited(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "idempotent.sqlite3")
    vault = _vault()
    opportunity_id = _import(conn, vault=vault).created_ids[0]
    first_fields = store.fetch_all(
        conn,
        "SELECT id, ciphertext, updated_at FROM private_field_values ORDER BY id",
    )
    first_audits = store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM audit_events"
    )["count"]

    unchanged = _import(conn, vault=vault, now=NOW + timedelta(hours=1))
    assert unchanged.unchanged_ids == [opportunity_id]
    assert store.fetch_all(
        conn,
        "SELECT id, ciphertext, updated_at FROM private_field_values ORDER BY id",
    ) == first_fields
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM audit_events"
    )["count"] == first_audits

    changed_row = {**_row(), "contact_roster": _roster(notes="Changed synthetic note.")}
    changed = _import(
        conn,
        row=changed_row,
        vault=vault,
        now=NOW + timedelta(hours=2),
    )
    assert changed.updated_ids == [opportunity_id]
    correction = store.fetch_one(
        conn,
        "SELECT details FROM audit_events WHERE opportunity_id = ? "
        "AND event_type = 'correction.appended' ORDER BY created_at DESC LIMIT 1",
        (opportunity_id,),
    )
    assert correction["details"]["changed_fields"] == [
        "contact_roster.publicist.updated"
    ]
    conn.close()


def test_opt_out_is_terminal_and_unusable_routes_do_not_prefill(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "opt-out.sqlite3")
    vault = _vault()
    opted_out_row = {
        **_row(),
        "contact_roster": _roster(
            permission_status="opted_out", usable=False, preferred=False
        ),
    }
    opportunity_id = _import(conn, row=opted_out_row, vault=vault).created_ids[0]
    revealed = contact_roster.reveal_contact_roster(
        conn,
        vault,
        tenant_id=BOUNDARY["tenant_id"],
        show_id=BOUNDARY["show_id"],
        opportunity_id=opportunity_id,
        actor_role="relationship_owner",
    )
    assert contact_roster.invitation_prefill(revealed) is None

    with pytest.raises(candidate_import.CandidateImportError, match="cannot be reactivated"):
        _import(conn, vault=vault, now=NOW + timedelta(hours=1))
    state = store.fetch_one(
        conn,
        "SELECT permission_status, usable FROM contact_rosters "
        "WHERE opportunity_id = ? AND role_kind = 'publicist'",
        (opportunity_id,),
    )
    assert state == {"permission_status": "opted_out", "usable": False}
    conn.close()


@pytest.mark.parametrize(
    ("raw_roster", "message"),
    [
        ("not-json", "valid JSON"),
        (json.dumps({"assistant": {"name": "Synthetic"}}), "supports only"),
        (json.dumps({"direct": {"name": "Synthetic", "secret": "x"}}), "unsupported fields"),
        (
            json.dumps(
                {
                    "direct": {
                        "name": "Synthetic",
                        "email": "direct@example.test",
                        "usable": True,
                        "permission_status": "permitted",
                    }
                }
            ),
            "verified",
        ),
        (
            '{"direct":{"name":"First"},"direct":{"name":"Second"}}',
            "keys must be unique",
        ),
    ],
)
def test_malformed_rosters_fail_before_any_write(
    tmp_path: Path, raw_roster: str, message: str
) -> None:
    conn = store.connect(tmp_path / "invalid.sqlite3")
    with pytest.raises(candidate_import.CandidateImportError, match=message):
        _import(conn, row={**_row(), "contact_roster": raw_roster})
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM private_field_values"
    )["count"] == 0
    conn.close()


def test_missing_vault_and_unauthorized_import_fail_before_write(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "denied.sqlite3")
    row = {**_row(), "contact_roster": _roster()}
    with pytest.raises(candidate_import.CandidateImportError, match="vault is required"):
        candidate_import.import_candidates(conn, [row], now=NOW, **BOUNDARY)
    with pytest.raises(candidate_import.CandidateImportError, match="cannot import"):
        candidate_import.import_candidates(
            conn,
            [row],
            field_vault=_vault(),
            actor_role="network_operator",
            now=NOW,
            **{key: value for key, value in BOUNDARY.items() if key != "actor_id"},
            actor_id=BOUNDARY["actor_id"],
        )
    assert store.fetch_one(
        conn, "SELECT COUNT(*) AS count FROM appearance_opportunities"
    )["count"] == 0
    conn.close()


def test_first_tenant_key_and_partial_batch_roll_back_together(tmp_path: Path) -> None:
    class FailingVault(encryption.FieldVault):
        def __init__(self) -> None:
            super().__init__(_vault().keys)
            self.calls = 0

        def put_text(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 6:
                raise encryption.EncryptionConfigurationError(
                    "synthetic custody outage"
                )
            return super().put_text(*args, **kwargs)

    conn = store.connect(tmp_path / "rollback.sqlite3")
    rows = [
        {**_row("crm:synthetic:first"), "contact_roster": _roster()},
        {**_row("crm:synthetic:second"), "contact_roster": _roster()},
    ]
    with pytest.raises(candidate_import.CandidateImportError, match="unavailable"):
        candidate_import.import_candidates(
            conn,
            rows,
            field_vault=FailingVault(),
            now=NOW,
            **BOUNDARY,
        )
    for table in (
        "appearance_opportunities",
        "contact_rosters",
        "private_field_values",
        "tenant_encryption_keys",
        "show_registry",
        "audit_events",
    ):
        assert store.fetch_one(conn, f"SELECT COUNT(*) AS count FROM {table}")[
            "count"
        ] == 0
    conn.close()


def test_api_role_gates_values_and_prefills_without_public_leakage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "api.sqlite3"
    conn = store.connect(database)
    vault = _vault()
    opportunity_id = _import(conn, vault=vault).created_ids[0]
    other_show_id = _import(
        conn,
        vault=vault,
        show_id="fixture_show_two",
        **{key: value for key, value in BOUNDARY.items() if key != "show_id"},
    ).created_ids[0]
    conn.commit()
    conn.close()

    producer_token = "synthetic-issue-20-producer-token"  # allow-secret: synthetic fixture
    network_token = "synthetic-issue-20-network-token"  # allow-secret: synthetic fixture
    other_token = "synthetic-issue-20-other-token"  # allow-secret: synthetic fixture
    app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        csrf_secret=b"synthetic-csrf-secret-issue-20-32",  # allow-secret: fixture
        field_vault=vault,
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {  # allow-secret: synthetic bearer lookup fixture
                producer_token: ("producer_fixture", "producer", "fixture_tenant"),
                network_token: ("network_fixture", "network_operator", "fixture_tenant"),
                other_token: ("producer_other", "producer", "other_tenant"),
            }
        ),
    )
    producer_headers = {"Authorization": f"Bearer {producer_token}"}
    network_headers = {"Authorization": f"Bearer {network_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    with TestClient(app) as client:
        public = client.get(
            f"/v1/opportunities/{opportunity_id}", headers=producer_headers
        )
        assert public.status_code == 200
        assert "contact_roster" not in public.json()
        public_payload = json.dumps(public.json(), sort_keys=True)
        assert all(value not in public_payload for value in PRIVATE_VALUES.values())
        listing = client.get("/v1/opportunities", headers=producer_headers)
        assert listing.status_code == 200
        assert listing.json()[0]["contact_roster_refs"][0]["role"] == "publicist"
        assert all(
            value not in json.dumps(listing.json(), sort_keys=True)
            for value in PRIVATE_VALUES.values()
        )

        private = client.get(
            f"/v1/opportunities/{opportunity_id}/contact-roster",
            headers=producer_headers,
        )
        assert private.status_code == 200
        assert private.headers["cache-control"] == "no-store, private"
        assert private.headers["pragma"] == "no-cache"
        assert private.json()["invitation_prefill"]["route_value"] == PRIVATE_VALUES[
            "email"
        ]
        assert private.json()["items"][0]["name"] == PRIVATE_VALUES["name"]

        queue = client.get("/v1/approval-queue", headers=producer_headers)
        assert queue.status_code == 200
        assert queue.headers["cache-control"] == "no-store, private"
        assert queue.json()[0]["contact_roster"][0]["name"] == PRIVATE_VALUES["name"]
        restricted_queue = client.get("/v1/approval-queue", headers=network_headers)
        assert "contact_roster" not in restricted_queue.json()[0]
        assert "contact_roster_refs" in restricted_queue.json()[0]
        assert client.get(
            f"/v1/opportunities/{opportunity_id}/contact-roster",
            headers=network_headers,
        ).status_code == 403
        assert client.get(
            f"/v1/opportunities/{opportunity_id}/contact-roster",
            headers=other_headers,
        ).status_code == 403

    class ScopedAuthenticator:
        def authenticate(self, conn, assertion, requested_show=None):
            del conn, assertion, requested_show
            return authentication.AuthenticatedOperator(
                actor_id="producer_fixture",
                role=service.HumanRole.PRODUCER,
                tenant_id="fixture_tenant",
                show_id="fixture_show",
                provider="synthetic_show_scope",
                subject_digest="0" * 64,
            )

    scoped_app = create_app(
        str(database),
        runtime_kind="synthetic_test",
        csrf_required=False,
        csrf_secret=b"synthetic-csrf-secret-issue-20-32",  # allow-secret: fixture
        field_vault=vault,
        access_authenticator=ScopedAuthenticator(),
    )
    with TestClient(scoped_app) as scoped_client:
        scoped_listing = scoped_client.get("/v1/opportunities")
        assert {row["show_id"] for row in scoped_listing.json()} == {"fixture_show"}
        scoped_queue = scoped_client.get("/v1/approval-queue")
        assert {row["show_id"] for row in scoped_queue.json()} == {"fixture_show"}
        assert scoped_client.get(
            f"/v1/opportunities/{other_show_id}"
        ).status_code == 403
        assert scoped_client.get(
            f"/v1/opportunities/{other_show_id}/contact-roster"
        ).status_code == 403


def test_cli_import_uses_environment_custody_without_echoing_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.csv"
    row = {**_row(), "contact_roster": _roster()}
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    monkeypatch.setenv(
        "HOSPES_MASTER_KEY_B64", base64.b64encode(MASTER_KEY).decode("ascii")
    )
    database = tmp_path / "cli.sqlite3"
    assert main(
        [
            "import-candidates",
            str(path),
            "--db",
            str(database),
            "--tenant",
            BOUNDARY["tenant_id"],
            "--network",
            BOUNDARY["network_id"],
            "--show",
            BOUNDARY["show_id"],
            "--actor",
            BOUNDARY["actor_id"],
        ]
    ) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["created"] == 1
    assert output.err == ""
    assert all(value not in output.out for value in PRIVATE_VALUES.values())
    conn = store.connect(database)
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM contact_rosters")[
        "count"
    ] == 2
    conn.close()


def test_dashboard_docs_and_domain_contract_are_complete_and_packaged() -> None:
    for relative in (
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/app.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/partnership.js",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/partnership-shell.js",
        "dashboard/assets/styles.css",
        "config/domain_kernel.yaml",
    ):
        packaged = Path("hospes/resources") / relative
        assert Path(relative).read_bytes() == packaged.read_bytes()

    workbench = Path("dashboard/assets/partnership-workspace.js").read_text()
    queue = Path("dashboard/assets/app.js").read_text()
    html = "\n".join(
        Path(relative).read_text()
        for relative in (
            "dashboard/index.html",
            "dashboard/assets/partnership-workspace.html",
        )
    )
    capabilities = Path("dashboard/assets/capabilities.mjs").read_text()
    assert "loadContactRoster" in workbench
    assert "invitationRoutePrefill" in workbench
    assert "escapeHTML(item.name)" in workbench
    assert "contactPills(candidate)" in queue
    assert "if (!capabilities.canViewContacts) return '';" in queue
    assert "escapeHTML(label)" in queue
    assert 'id="invitation-route-prefill"' in html
    assert "Prefill never sends" in html
    assert "canViewContacts" in capabilities

    documentation = Path("docs/contact-roster.md").read_text()
    assert "private-input-only" in documentation
    assert "AES-256-GCM" in documentation
    assert "network_operator" in documentation
    assert "does not book, send, or persist" in documentation
    domain = Path("config/domain_kernel.yaml").read_text()
    assert "ContactRoster" in domain
    assert "opted_out are terminal" in domain


def test_parser_bounded_types_defaults_and_safe_errors() -> None:
    entries = contact_roster.parse_contact_roster(
        {
            "direct": {
                "name": "  Synthetic Direct  ",
                "phone": "+1 (555) 010-0200 ext 4",
                "provenance_ref": "roster://fixture/direct-20",
            }
        },
        source_key="crm:synthetic:direct",
        now=NOW,
    )
    assert entries[0].name == "Synthetic Direct"
    assert entries[0].permission_status == "pending_verification"
    assert entries[0].usable is False
    assert "Synthetic Direct" not in repr(entries[0])
    assert contact_roster.parse_contact_roster(None, source_key="x", now=NOW) == ()
    assert contact_roster.parse_contact_roster("", source_key="x", now=NOW) == ()

    for raw in (
        {"direct": {"name": 5}},
        {"direct": {"name": "Synthetic", "email": "bad-address"}},
        {"direct": {"name": "Synthetic", "phone": "123"}},
        {"direct": {"name": "Synthetic", "usable": "yes"}},
        {"direct": {"name": "Synthetic", "permission_status": 1}},
        {
            "direct": {
                "name": "Synthetic",
                "verified_at": "2026-08-10T18:00:00+00:00",
            }
        },
        {"direct": {"name": "Synthetic", "provenance_ref": "not-opaque"}},
        {"direct": {"name": "Synthetic", "provenance_ref": 1}},
        {1: {"name": "Synthetic"}},
        {"direct": {1: "Synthetic", "name": "Synthetic"}},
    ):
        with pytest.raises(contact_roster.ContactRosterError) as caught:
            contact_roster.parse_contact_roster(
                raw, source_key="crm:synthetic:invalid", now=NOW
            )
        assert "Synthetic" not in str(caught.value)

    with pytest.raises(contact_roster.ContactRosterError, match="JSON object"):
        contact_roster.parse_contact_roster([], source_key="x", now=NOW)
    with pytest.raises(contact_roster.ContactRosterError, match="32 KiB"):
        contact_roster.parse_contact_roster(
            json.dumps({"direct": {"name": "x" * 33000}}),
            source_key="x",
            now=NOW,
        )
    with pytest.raises(contact_roster.ContactRosterError, match="timezone"):
        contact_roster.parse_contact_roster(
            {"direct": {"name": "Synthetic"}},
            source_key="x",
            now=datetime(2026, 8, 10, 16, 0),
        )
    assert contact_roster.invitation_prefill([]) is None
