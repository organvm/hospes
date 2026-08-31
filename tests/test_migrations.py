"""Focused tests for additive SQLite schema migrations."""

from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hospes import migrations, store


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA index_list({table})")}


def test_new_database_reaches_latest_schema(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "new.sqlite3")

    assert migrations.current_version(conn) == migrations.LATEST_VERSION
    assert migrations.LATEST_VERSION == 22
    assert [row["version"] for row in migrations.applied_migrations(conn)] == [
        migration.version for migration in migrations.MIGRATIONS
    ]
    assert {
        "source_key",
        "relationship_owner",
        "preferred_city",
        "social_cost_1_5",
        "ari_effort",
        "next_action",
        "source_provenance",
    } <= _columns(conn, "appearance_opportunities")
    opportunity_indexes = _indexes(conn, "appearance_opportunities")
    assert "ux_opp_tenant_show_source" in opportunity_indexes
    assert "ux_opp_tenant_source" not in opportunity_indexes
    assert "operational_receipts" in {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "ix_receipt_opportunity_occurred",
        "ix_receipt_tenant_type",
    } <= _indexes(conn, "operational_receipts")
    assert {
        "partnerships",
        "partnership_items",
        "partnership_audit_events",
        "partnership_item_revisions",
        "partnership_template_imports",
        "partnership_resource_links",
        "partnership_reviews",
        "pilot_candidate_slots",
        "pilot_policies",
        "pilot_runs",
        "pilot_candidate_assignments",
        "pilot_assignment_events",
        "pilot_plan_projections",
        "pilot_decisions",
        "runtime_metadata",
        "tenant_encryption_keys",
        "identity_mappings",
        "assignments",
        "background_jobs",
        "delivery_receipts",
        "portal_sessions",
        "private_field_values",
        "artifact_objects",
        "job_attempt_receipts",
        "contact_rosters",
        "portal_date_choices",
        "portal_receipts",
    } <= {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"revision", "superseded_at", "superseded_by_item_id"} <= _columns(conn, "partnership_items")
    assert {"show_id", "subject_ref", "correlation_id", "idempotency_key"} <= _columns(conn, "audit_events")
    assert {"preview_checksum", "policy_version", "authorized_at"} <= _columns(conn, "authorization_receipts")
    assert {"actor_id", "actor_role", "payload_checksum", "attempt"} <= _columns(conn, "provider_receipts")
    assert "authorization_action" in _columns(conn, "delivery_receipts")
    conn.close()


def test_legacy_database_is_upgraded_without_rewriting_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(migrations.BASE_SCHEMA)
    timestamp = datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat()
    legacy.execute(
        "INSERT INTO appearance_opportunities "
        "(id, tenant_id, network_id, show_id, guest_name, why_guest, why_now, "
        "proposed_artifact, relationship_class, status, disposition, episode_thesis, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-opportunity",
            "tenant_one",
            "network_one",
            "show_one",
            "Synthetic Legacy Guest",
            "A synthetic record proves that migration preserves existing rows.",
            "The migration boundary is being introduced before private operation.",
            "A migration receipt",
            "C1",
            "DISCOVERED",
            None,
            None,
            timestamp,
            timestamp,
        ),
    )
    legacy.commit()
    legacy.close()

    upgraded = store.connect(path)
    row = store.fetch_one(
        upgraded,
        "SELECT * FROM appearance_opportunities WHERE id = ?",
        ("legacy-opportunity",),
    )

    assert row is not None
    assert row["guest_name"] == "Synthetic Legacy Guest"
    assert row["created_at"] == timestamp
    assert row["source_key"] is None
    assert migrations.current_version(upgraded) == migrations.LATEST_VERSION
    assert len(list(tmp_path.glob("legacy.sqlite3.pre-v0-*.bak"))) == 1
    assert (tmp_path / f"legacy.sqlite3.migration-v0-v{migrations.LATEST_VERSION}.receipt.json").is_file()


def test_migrate_is_idempotent_at_exact_version(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "idempotent.sqlite3")
    first = migrations.applied_migrations(conn)
    second = migrations.migrate(conn)
    third = migrations.migrate(conn)

    assert second == first
    assert third == first
    assert len(second) == len(migrations.MIGRATIONS)


def test_migration_ten_revokes_pre_envelope_key_rows(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "pre-envelope.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
    )
    for migration in migrations.MIGRATIONS[:9]:
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            (migration.version, migration.name, "2026-08-10T00:00:00+00:00"),
        )
    conn.execute(
        "INSERT INTO tenant_encryption_keys "
        "(id, tenant_id, key_version, algorithm, wrapped_key_ref, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-key",
            "tenant-a",
            1,
            "AES-256-GCM",
            "credential://hospes/legacy-key",
            "active",
            "2026-08-10T00:00:00+00:00",
        ),
    )
    conn.commit()

    migrations.migrate(conn)

    row = conn.execute(
        "SELECT status, retired_at, wrapped_key_ciphertext FROM tenant_encryption_keys WHERE id = 'legacy-key'"
    ).fetchone()
    assert dict(row) == {
        "status": "revoked",
        "retired_at": "2026-08-10T00:00:00+00:00",
        "wrapped_key_ciphertext": None,
    }
    conn.close()


def test_v4_partnership_rows_gain_history_without_count_or_audit_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v4.sqlite3"
    rollback_path = tmp_path / "v4-rollback.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
    )
    for migration in migrations.MIGRATIONS[:4]:
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            (migration.version, migration.name, "2026-07-22T00:00:00+00:00"),
        )
    timestamp = "2026-07-22T00:00:00+00:00"
    conn.execute(
        "INSERT INTO partnerships VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "partnership-v4",
            "private_pilot",
            "example_partnership",
            "Host + Producer",
            "A bounded private pilot partnership.",
            "active",
            timestamp,
            timestamp,
        ),
    )
    for index in range(22):
        conn.execute(
            "INSERT INTO partnership_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"item-{index}",
                "private_pilot",
                "partnership-v4",
                f"item.{index}",
                "plan",
                f"Item {index}",
                "A bounded migrated item.",
                "Partners",
                "current",
                None,
                None,
                timestamp,
                timestamp,
            ),
        )
    for index in range(23):
        conn.execute(
            "INSERT INTO partnership_audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"event-{index}",
                "private_pilot",
                "partnership-v4",
                "partnership.item_created",
                "migration_fixture",
                "producer",
                "{}",
                timestamp,
            ),
        )
    conn.commit()
    rollback = sqlite3.connect(rollback_path)
    conn.backup(rollback)
    rollback.close()
    conn.close()

    upgraded = store.connect(path)
    assert upgraded.execute("SELECT COUNT(*) FROM partnership_items").fetchone()[0] == 22
    assert upgraded.execute("SELECT COUNT(*) FROM partnership_audit_events").fetchone()[0] == 23
    assert upgraded.execute("SELECT COUNT(*) FROM partnership_item_revisions").fetchone()[0] == 22
    assert {row[0] for row in upgraded.execute("SELECT revision FROM partnership_items")} == {1}
    assert migrations.current_version(upgraded) == migrations.LATEST_VERSION
    assert upgraded.execute("SELECT COUNT(*) FROM pilot_runs").fetchone()[0] == 0

    restored = sqlite3.connect(rollback_path)
    assert migrations.current_version(restored) == 4
    assert restored.execute("SELECT COUNT(*) FROM partnership_items").fetchone()[0] == 22
    assert restored.execute("SELECT COUNT(*) FROM partnership_audit_events").fetchone()[0] == 23
    assert (
        restored.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'pilot_runs'").fetchone()[
            0
        ]
        == 0
    )
    restored.close()

    automatic_backups = list(tmp_path.glob("v4.sqlite3.pre-v4-*.bak"))
    assert len(automatic_backups) == 1
    automatic = sqlite3.connect(automatic_backups[0])
    assert migrations.current_version(automatic) == 4
    assert automatic.execute("SELECT COUNT(*) FROM partnership_items").fetchone()[0] == 22
    automatic.close()
    receipt = (tmp_path / f"v4.sqlite3.migration-v4-v{migrations.LATEST_VERSION}.receipt.json").read_text(
        encoding="utf-8"
    )
    assert '"schema": "HOSPESSQLiteMigrationReceiptV1"' in receipt
    assert '"rows": 22' in receipt


def test_migration_nine_enforces_scoped_delivery_foreign_keys(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "scoped.sqlite3")
    timestamp = "2026-08-10T16:00:00+00:00"
    conn.execute(
        "INSERT INTO show_registry "
        "(id, tenant_id, show_id, label, config_ref, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("show-a", "tenant-a", "flagship", "Flagship", "config://show/a", "active", timestamp, timestamp),
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, tenant_id, show_id, episode_id, platform, status, metadata, idempotency_key, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "distribution-a",
            "tenant-a",
            "flagship",
            "episode-a",
            "rss",
            "draft",
            "{}",
            "distribution-key",
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        "INSERT INTO authorization_receipts "
        "(id, tenant_id, show_id, action, subject_ref, idempotency_key, authorized_by, authorization_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "authorization-a",
            "tenant-a",
            "flagship",
            "publish",
            "distribution-a",
            "authorization-key",
            "operator-a",
            "receipt://auth/a",
            timestamp,
        ),
    )
    conn.execute(
        "INSERT INTO delivery_receipts "
        "(id, tenant_id, show_id, distribution_id, authorization_receipt_id, provider, attempt, result, external_reference, payload_checksum, started_at, completed_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "delivery-a",
            "tenant-a",
            "flagship",
            "distribution-a",
            "authorization-a",
            "manual",
            1,
            "succeeded",
            "external://delivery/a",
            "a" * 64,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO delivery_receipts "
            "(id, tenant_id, show_id, distribution_id, authorization_receipt_id, provider, attempt, result, external_reference, payload_checksum, started_at, completed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "delivery-b",
                "tenant-b",
                "flagship",
                "distribution-a",
                "authorization-a",
                "manual",
                2,
                "succeeded",
                "external://delivery/b",
                "b" * 64,
                timestamp,
                timestamp,
                timestamp,
            ),
        )

    conn.execute(
        "INSERT INTO authorization_receipts "
        "(id, tenant_id, show_id, action, subject_ref, idempotency_key, authorized_by, authorization_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "authorization-b",
            "tenant-a",
            "flagship",
            "publish",
            "distribution-other",
            "authorization-other-subject",
            "operator-a",
            "receipt://auth/b",
            timestamp,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO delivery_receipts "
            "(id, tenant_id, show_id, distribution_id, authorization_receipt_id, provider, attempt, result, external_reference, payload_checksum, started_at, completed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "delivery-c",
                "tenant-a",
                "flagship",
                "distribution-a",
                "authorization-b",
                "manual",
                2,
                "succeeded",
                "external://delivery/c",
                "c" * 64,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    conn.execute(
        "INSERT INTO authorization_receipts "
        "(id, tenant_id, show_id, action, subject_ref, idempotency_key, authorized_by, authorization_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "authorization-c",
            "tenant-a",
            "flagship",
            "send",
            "distribution-a",
            "authorization-wrong-action",
            "operator-a",
            "receipt://auth/c",
            timestamp,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO delivery_receipts "
            "(id, tenant_id, show_id, distribution_id, authorization_receipt_id, provider, attempt, result, external_reference, payload_checksum, started_at, completed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "delivery-d",
                "tenant-a",
                "flagship",
                "distribution-a",
                "authorization-c",
                "manual",
                3,
                "succeeded",
                "external://delivery/d",
                "d" * 64,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    conn.close()


def test_migration_nine_enforces_tenant_wide_identity_uniqueness(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "identity.sqlite3")
    timestamp = "2026-08-10T16:00:00+00:00"
    values = (
        "identity-a",
        "tenant-a",
        None,
        "cloudflare-access",
        "subject-digest",
        "operator-a",
        "network_operator",
        timestamp,
    )
    conn.execute(
        "INSERT INTO identity_mappings "
        "(id, tenant_id, show_id, provider, subject_hash, actor_id, actor_role, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO identity_mappings "
            "(id, tenant_id, show_id, provider, subject_hash, actor_id, actor_role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("identity-b", *values[1:]),
        )
    conn.close()


def test_receipt_uniqueness_and_json_roundtrip(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "receipt.sqlite3")
    now = datetime.now(timezone.utc).isoformat()
    store.insert(
        conn,
        "appearance_opportunities",
        {
            "id": "opportunity_one",
            "tenant_id": "tenant_one",
            "network_id": "network_one",
            "show_id": "show_one",
            "guest_name": "Synthetic Guest",
            "why_guest": "A synthetic guest makes the receipt migration test concrete.",
            "why_now": "The receipt ledger is required before lifecycle service work.",
            "proposed_artifact": "A receipt",
            "relationship_class": "C1",
            "status": "DISCOVERED",
            "disposition": None,
            "episode_thesis": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    receipt = {
        "id": "receipt_one",
        "tenant_id": "tenant_one",
        "opportunity_id": "opportunity_one",
        "receipt_type": "outreach.sent",
        "external_reference": "mailbox://opaque-001",
        "occurred_at": now,
        "actor_id": "producer_one",
        "actor_role": "producer",
        "details": {"scope": "private_pilot"},
        "created_at": now,
    }
    store.insert(conn, "operational_receipts", receipt)
    conn.commit()

    stored = store.fetch_one(conn, "SELECT * FROM operational_receipts WHERE id = ?", ("receipt_one",))
    assert stored is not None
    assert stored["details"] == {"scope": "private_pilot"}

    with pytest.raises(sqlite3.IntegrityError):
        store.insert(conn, "operational_receipts", {**receipt, "id": "receipt_two"})


def test_private_runtime_file_and_new_parent_permissions(tmp_path: Path) -> None:
    database = tmp_path / "private" / "hospes.sqlite3"
    conn = store.connect(database)
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600

    assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
    conn.execute("CREATE TABLE runtime_permission_fixture (id INTEGER)")
    conn.execute("INSERT INTO runtime_permission_fixture VALUES (1)")
    conn.commit()
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    assert wal.exists() and shm.exists()
    wal.chmod(0o644)
    shm.chmod(0o644)
    second = store.connect(database)
    assert stat.S_IMODE(wal.stat().st_mode) == 0o600
    assert stat.S_IMODE(shm.stat().st_mode) == 0o600
    second.close()
    conn.close()
