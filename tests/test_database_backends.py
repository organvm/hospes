from __future__ import annotations

from dataclasses import dataclass
import os
import sqlite3
from pathlib import Path
import sys
import types

import pytest

from hospes import migrations, store


@dataclass
class _Status:
    name: str = "IDLE"


@dataclass
class _Info:
    transaction_status: _Status


class _Cursor:
    def __init__(self, version: str = "160004") -> None:
        self.version = version

    def fetchone(self):
        return {"server_version_num": self.version}

    def fetchall(self):
        return []


class _RawConnection:
    def __init__(self, *, version: str = "160004", status: str = "IDLE") -> None:
        self.info = _Info(_Status(status))
        self.version = version
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql: str, params: tuple[object, ...]):
        self.calls.append((sql, params))
        return _Cursor(self.version)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def test_postgres_adapter_converts_bindings_only_outside_literals() -> None:
    raw = _RawConnection()
    conn = store.PostgresConnection(raw)
    conn.execute(
        "SELECT '?' AS literal FROM records WHERE first = ? AND second IS ?",
        ("one", None),
    )
    assert raw.calls == [
        (
            "SELECT '?' AS literal FROM records WHERE first = %s "
            "AND second IS NOT DISTINCT FROM %s",
            ("one", None),
        )
    ]
    assert conn.backend == "postgresql"
    assert not conn.in_transaction
    assert isinstance(conn, store.DatabaseConnection)
    assert store._postgres_sql("SELECT 'it''s ?' WHERE id = ?") == (  # noqa: SLF001
        "SELECT 'it''s ?' WHERE id = %s"
    )
    assert store._postgres_sql(  # noqa: SLF001
        "SELECT id FROM records WHERE ref LIKE 'calendar://%' AND tenant_id = ?"
    ) == "SELECT id FROM records WHERE ref LIKE 'calendar://%%' AND tenant_id = %s"


def test_postgres_adapter_converts_sqlite_idempotent_insert() -> None:
    raw = _RawConnection()
    conn = store.PostgresConnection(raw)
    conn.execute("INSERT OR IGNORE INTO records (id) VALUES (?)", ("record-a",))
    assert raw.calls[0] == (
        "INSERT INTO records (id) VALUES (%s) ON CONFLICT DO NOTHING",
        ("record-a",),
    )
    conn.commit()
    conn.rollback()
    conn.close()
    assert (raw.commits, raw.rollbacks, raw.closed) == (1, 1, True)


def _install_fake_psycopg(monkeypatch: pytest.MonkeyPatch, raw: _RawConnection) -> None:
    package = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    package.connect = lambda *_args, **_kwargs: raw
    monkeypatch.setitem(sys.modules, "psycopg", package)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)


def test_postgres_connection_bootstrap_and_query_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _RawConnection()
    _install_fake_psycopg(monkeypatch, raw)
    migrated: list[store.DatabaseConnection] = []
    monkeypatch.setattr(store.migrations, "migrate", lambda conn: migrated.append(conn))
    connection = store._connect_postgres(  # noqa: SLF001
        "postgresql://example.test/hospes?sslmode=require",
        query_only=False,
        migrate=True,
    )
    assert connection.backend == "postgresql"
    assert migrated == [connection]
    assert raw.commits == 1

    readonly_raw = _RawConnection()
    _install_fake_psycopg(monkeypatch, readonly_raw)
    store._connect_postgres(  # noqa: SLF001
        "postgresql://example.test/hospes",
        query_only=True,
        migrate=False,
    )
    assert ("SET default_transaction_read_only = on", ()) in readonly_raw.calls
    assert readonly_raw.commits == 2


def test_postgres_connection_rejects_old_or_failed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _RawConnection(version="150009")
    _install_fake_psycopg(monkeypatch, old)
    with pytest.raises(store.DatabaseConfigurationError, match="16 or newer"):
        store._connect_postgres(  # noqa: SLF001
            "postgresql://example.test/hospes?sslmode=verify-full",
            query_only=False,
            migrate=False,
        )
    assert old.closed

    package = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider detail that must not escape")

    package.connect = fail
    monkeypatch.setitem(sys.modules, "psycopg", package)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    with pytest.raises(store.DatabaseConfigurationError, match="connection failed") as caught:
        store._connect_postgres(  # noqa: SLF001
            "postgresql://example.test/hospes?sslmode=require",
            query_only=False,
            migrate=False,
        )
    assert "provider detail" not in str(caught.value)


def test_postgres_adapter_reports_active_transaction() -> None:
    assert store.PostgresConnection(_RawConnection(status="INTRANS")).in_transaction


def test_postgres_rows_preserve_keyed_and_positional_access() -> None:
    class RawCursor:
        def fetchone(self):
            return {"count": 3, "label": "ready"}

        def fetchall(self):
            return [{"count": 3, "label": "ready"}]

    cursor = store.PostgresCursor(RawCursor())
    row = cursor.fetchone()
    assert row[0] == row["count"] == 3
    assert row[1] == row["label"] == "ready"
    assert dict(cursor.fetchall()[0]) == {"count": 3, "label": "ready"}


def test_postgres_missing_migration_ledger_is_an_empty_state() -> None:
    class CatalogCursor:
        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class CatalogConnection:
        backend = "postgresql"
        in_transaction = False

        def execute(self, sql, params=()):
            assert "information_schema.tables" in sql
            assert params == ("schema_migrations",)
            return CatalogCursor()

    assert migrations.applied_migrations(CatalogConnection()) == []


def test_database_target_rejects_unsafe_postgres_and_path_confusion(tmp_path: Path) -> None:
    with pytest.raises(store.DatabaseConfigurationError, match="requires TLS"):
        store._validate_postgres_url(  # noqa: SLF001 - adapter boundary regression
        "postgresql://example.test/hospes?sslmode=disable"
        )
    with pytest.raises(store.DatabaseConfigurationError, match="valid database URL"):
        store._validate_postgres_url("postgresql:///hospes")  # noqa: SLF001
    with pytest.raises(ValueError, match="URLs"):
        store.resolve_db_path("postgresql://example.test/hospes")
    assert store.resolve_db_path(tmp_path / "local.sqlite3") == tmp_path / "local.sqlite3"
    assert store._validate_postgres_url(  # noqa: SLF001
        "postgresql://example.test/hospes?sslmode=verify-ca"
    ) == "verify-ca"


def test_database_environment_selection_and_query_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "local.sqlite3"
    monkeypatch.setenv("HOSPES_DB", str(local))
    assert store.resolve_db_path() == local
    monkeypatch.setenv("HOSPES_DB", "postgresql://example.test/hospes")
    with pytest.raises(ValueError, match="HOSPES_DATABASE_URL"):
        store.resolve_db_path()

    hosted = "postgresql://example.test/hospes?sslmode=require"
    monkeypatch.setenv("HOSPES_DATABASE_URL", hosted)
    assert store._database_target(None) == hosted  # noqa: SLF001
    sentinel = _RawConnection()
    monkeypatch.setattr(store, "_connect_postgres", lambda *_args, **_kwargs: sentinel)
    assert store.connect(query_only=True) is sentinel

    with pytest.raises(ValueError, match="existing regular file"):
        store.connect(tmp_path / "missing.sqlite3", query_only=True)
    with pytest.raises(ValueError, match="existing database file"):
        store.connect(":memory:", query_only=True)

    monkeypatch.delenv("HOSPES_DATABASE_URL")
    monkeypatch.setenv("HOSPES_DB", str(local))
    connection = store.connect()
    connection.close()
    readonly = store.connect(local, query_only=True)
    assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
    readonly.close()


def _create_version_eight(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
    )
    for migration in migrations.MIGRATIONS[:8]:
        migration.apply(connection)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            (migration.version, migration.name, "2026-08-10T00:00:00+00:00"),
        )
    connection.commit()
    return connection


def test_shadow_migration_fails_closed_on_checksum_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "checksum.sqlite3"
    _create_version_eight(path).close()
    original_snapshot = store._sqlite_snapshot  # noqa: SLF001
    calls = 0

    def changed_snapshot(connection, columns_by_table=None):
        nonlocal calls
        calls += 1
        columns, snapshot = original_snapshot(connection, columns_by_table)
        if columns_by_table is not None:
            first = next(iter(snapshot))
            snapshot[first]["sha256"] = "0" * 64
        return columns, snapshot

    monkeypatch.setattr(store, "_sqlite_snapshot", changed_snapshot)
    with pytest.raises(migrations.MigrationError, match="changed existing rows"):
        store.connect(path)
    preserved = sqlite3.connect(path)
    assert migrations.current_version(preserved) == 8
    preserved.close()
    assert calls == 2


def test_shadow_migration_fails_closed_on_existing_foreign_key_violation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "foreign-key.sqlite3"
    connection = _create_version_eight(path)
    connection.execute(
        "INSERT INTO contact_routes "
        "(id, tenant_id, opportunity_id, route_type, route_label, source_provenance, verified_at, usable, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "route-a",
            "tenant-a",
            "missing-opportunity",
            "manual",
            "opaque",
            "source://fixture",
            "2026-08-10T00:00:00+00:00",
            1,
            "2026-08-10T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(migrations.MigrationError, match="foreign-key validation"):
        store.connect(path)


def test_shadow_migration_never_exposes_database_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt-failure.sqlite3"
    _create_version_eight(path).close()

    def fail_receipt(*_args, **_kwargs):
        raise OSError("synthetic receipt storage failure")

    monkeypatch.setattr(store, "_write_migration_receipt", fail_receipt)
    with pytest.raises(OSError, match="receipt storage failure"):
        store.connect(path)
    preserved = sqlite3.connect(path)
    assert migrations.current_version(preserved) == 8
    preserved.close()
    assert not list(tmp_path.glob("receipt-failure.sqlite3.migration-*.receipt.json"))


def test_live_sqlite_connection_holds_shared_custody_lock(tmp_path: Path) -> None:
    database = tmp_path / "custody.sqlite3"
    connection = store.connect(database)
    custody = connection._custody_lock  # noqa: SLF001 - exact custody regression
    handle = custody._handle  # noqa: SLF001 - exact release regression
    assert handle is not None and os.fstat(handle)
    if os.name != "nt":
        import fcntl

        contender = os.open(database.with_name(f".{database.name}.migration.lock"), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
    connection.close()
    with pytest.raises(OSError):
        os.fstat(handle)


def test_windows_custody_lock_adapter_uses_read_and_unlock_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int]] = []
    module = types.ModuleType("msvcrt")
    module.LK_LOCK = 1
    module.LK_RLCK = 2
    module.LK_UNLCK = 3
    module.locking = lambda fd, mode, count: calls.append((fd, mode, count))
    monkeypatch.setitem(sys.modules, "msvcrt", module)
    monkeypatch.setattr(store.os, "name", "nt")
    custody = store._acquire_sqlite_lock(  # noqa: SLF001 - platform regression
        tmp_path / "windows.sqlite3", exclusive=False
    )
    descriptor = custody._handle  # noqa: SLF001
    custody.close()
    assert calls == [(descriptor, module.LK_RLCK, 1), (descriptor, module.LK_UNLCK, 1)]


def test_row_helpers_cover_empty_bool_and_noop_update(tmp_path: Path) -> None:
    assert store.row_to_dict(None) is None
    connection = store.connect(tmp_path / "helpers.sqlite3")
    store.insert(
        connection,
        "runtime_metadata",
        {
            "id": "metadata-a",
            "tenant_id": "tenant-a",
            "metadata_key": "fixture",
            "metadata_value": {"ready": True},
            "created_at": "2026-08-10T00:00:00+00:00",
        },
    )
    store.update(connection, "runtime_metadata", "metadata-a", {})
    assert store.fetch_one(
        connection, "SELECT * FROM runtime_metadata WHERE id = ?", ("metadata-a",)
    )["metadata_value"] == {"ready": True}
    assert store._encode("usable", False) == 0  # noqa: SLF001
