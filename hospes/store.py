"""Provider-neutral persistent store for the guest-operations service layer.

Ported from the overnight ``agent/hospes-core-api`` slice (PR #2), which used
SQLAlchemy + a hosted Postgres URL. The ORM is replaced with a small
provider-neutral adapter, and the default database remains a durable,
local-first SQLite file:

* default location ``out/hospes.sqlite3`` (a gitignored runtime artifact, per
  :mod:`hospes.paths`),
* overridable with the ``HOSPES_DB`` environment variable, or an explicit
  ``db_path`` passed to :func:`connect`.

SQLite remains the safe local default. Hosted profiles may use PostgreSQL 16+
through the optional ``hosted`` extra. Both backends preserve the historical
qmark-binding store API; PostgreSQL binding conversion happens only at its
adapter boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from . import migrations
from .paths import SERVICE_DB, ensure_out_dirs

# Backward-compatible schema export for callers that used the original module.
SCHEMA = migrations.BASE_SCHEMA


class ManagedConnection(sqlite3.Connection):
    """Close SQLite handles and release their shared custody lock."""

    backend = "sqlite"
    _custody_lock: Any | None = None

    def hold_custody_lock(self, handle: Any) -> None:
        self._custody_lock = handle

    def close(self) -> None:
        handle = getattr(self, "_custody_lock", None)
        try:
            super().close()
        finally:
            if handle is not None:
                _release_file_lock(handle)
                self._custody_lock = None

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup timing
        try:
            self.close()
        except Exception:
            pass


class DatabaseConfigurationError(RuntimeError):
    """Raised when a database target cannot satisfy the runtime contract."""


@runtime_checkable
class DatabaseCursor(Protocol):
    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


@runtime_checkable
class DatabaseConnection(Protocol):
    """Small connection contract retained by every HOSPES service."""

    backend: str

    @property
    def in_transaction(self) -> bool: ...

    def execute(self, sql: str, params: Iterable[Any] = ()) -> DatabaseCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class _CustodyLock:
    """Own a platform lock handle and release it safely at finalization."""

    def __init__(self, handle: Any):
        self._handle = handle

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        _release_locked_handle(handle)

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup timing
        try:
            self.close()
        except Exception:
            pass


class DatabaseRow(Mapping[str, Any]):
    """Mapping row that also preserves SQLite-style positional access."""

    def __init__(self, values: Mapping[str, Any]):
        self._values = dict(values)
        self._keys = tuple(self._values)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            key = self._keys[key]
        return self._values[key]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


class PostgresCursor:
    """Adapt psycopg mapping rows to the historical dual-access row API."""

    def __init__(self, raw: Any):
        self._raw = raw

    @staticmethod
    def _row(value: Any) -> Any:
        return DatabaseRow(value) if isinstance(value, Mapping) else value

    def fetchone(self) -> Any:
        return self._row(self._raw.fetchone())

    def fetchall(self) -> list[Any]:
        return [self._row(row) for row in self._raw.fetchall()]


def _postgres_sql(sql: str) -> str:
    """Convert HOSPES qmark SQL without touching quoted question marks."""
    converted = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
        "INSERT INTO",
        sql,
        flags=re.IGNORECASE,
    )
    ignored_insert = converted != sql
    converted = re.sub(
        r"\bIS\s+\?",
        "IS NOT DISTINCT FROM ?",
        converted,
        flags=re.IGNORECASE,
    )
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(converted):
        character = converted[index]
        if quote:
            output.append(character)
            if character == "%":
                output.append("%")
            if character == quote:
                if index + 1 < len(converted) and converted[index + 1] == quote:
                    output.append(converted[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?":
            output.append("%s")
        elif character == "%":
            # psycopg parses percent placeholders even inside SQL literals.
            output.append("%%")
        else:
            output.append(character)
        index += 1
    result = "".join(output)
    if ignored_insert:
        result = result.rstrip().removesuffix(";") + " ON CONFLICT DO NOTHING"
    return result


class PostgresConnection:
    """Thin psycopg wrapper that keeps the existing HOSPES connection API."""

    backend = "postgresql"

    def __init__(self, raw: Any):
        self._raw = raw

    @property
    def in_transaction(self) -> bool:
        status = getattr(getattr(self._raw, "info", None), "transaction_status", None)
        name = getattr(status, "name", str(status))
        return name not in {"IDLE", "0", "None"}

    def execute(self, sql: str, params: Iterable[Any] = ()) -> DatabaseCursor:
        return PostgresCursor(self._raw.execute(_postgres_sql(sql), tuple(params)))

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


# Columns whose stored value is JSON text (decoded on read).
_JSON_COLUMNS = {
    "research_claims",
    "segments",
    "assets",
    "details",
    "allowed_relationship_classes",
    "production_gate_durations",
    "human_authority_rules",
    "ranked_action",
    "alternatives",
    "candidate_timing",
    "constraints",
    "rationale",
    "evidence",
    "decision_payload",
    "resulting_state_changes",
    "metadata_value",
    "metadata",
    "metrics",
    "brief",
    "citations",
    "counterarguments",
    "seeds",
    "segment_candidates",
    "risk_flags",
    "offered_dates",
}
# Columns stored as 0/1 integers but surfaced as bool.
_BOOL_COLUMNS = {"preferred", "usable", "committed", "approved_for_external_use", "do_not_contact"}


def _is_postgres_target(value: str | Path | None) -> bool:
    return isinstance(value, str) and value.startswith(("postgresql://", "postgres://"))


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the database file: explicit arg > ``HOSPES_DB`` env > default."""
    if db_path is not None:
        if _is_postgres_target(db_path):
            raise ValueError("PostgreSQL targets are URLs, not local database paths")
        return Path(db_path)
    env = os.environ.get("HOSPES_DB")
    if env:
        if _is_postgres_target(env):
            raise ValueError("HOSPES_DB contains a URL; use HOSPES_DATABASE_URL")
        return Path(env)
    return SERVICE_DB


def _database_target(db_path: str | Path | None) -> str | Path:
    if db_path is not None:
        return db_path
    hosted = os.environ.get("HOSPES_DATABASE_URL")
    if hosted:
        return hosted
    return resolve_db_path()


def _validate_postgres_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise DatabaseConfigurationError("PostgreSQL target must be a valid database URL")
    sslmode = parse_qs(parsed.query).get("sslmode", [None])[-1]
    if sslmode not in {None, "require", "verify-ca", "verify-full"}:
        raise DatabaseConfigurationError("PostgreSQL requires TLS sslmode require or stronger")
    return sslmode


def _connect_postgres(
    url: str,
    *,
    query_only: bool,
    migrate: bool,
) -> PostgresConnection:
    sslmode = _validate_postgres_url(url)
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - optional dependency boundary
        raise DatabaseConfigurationError("PostgreSQL requires the HOSPES hosted extra") from exc
    try:
        kwargs = {"row_factory": dict_row}
        if sslmode is None:
            kwargs["sslmode"] = "require"
        raw = psycopg.connect(url, **kwargs)
        connection = PostgresConnection(raw)
        version_row = connection.execute("SHOW server_version_num").fetchone()
        version_value = next(iter(version_row.values())) if isinstance(version_row, Mapping) else version_row[0]
        if int(version_value) < 160000:
            connection.close()
            raise DatabaseConfigurationError("PostgreSQL 16 or newer is required")
        connection.commit()
        if query_only:
            connection.execute("SET default_transaction_read_only = on")
            connection.commit()
        elif migrate:
            migrations.migrate(connection)
        return connection
    except DatabaseConfigurationError:
        raise
    except Exception as exc:
        raise DatabaseConfigurationError("PostgreSQL connection failed") from exc


def table_names(conn: DatabaseConnection) -> set[str]:
    """Return application table names without exposing backend catalogs."""
    if conn.backend == "postgresql":
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
        return {str(row["table_name"] if isinstance(row, Mapping) else row[0]) for row in rows}
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _lock_file(handle: int, *, exclusive: bool) -> None:
    """Acquire one portable byte-range lock, shared for live connections."""
    if os.name == "nt":  # pragma: no cover - exercised on the Windows gate
        import msvcrt

        if os.lseek(handle, 0, os.SEEK_END) == 0:
            os.write(handle, b"\0")
            os.fsync(handle)
        os.lseek(handle, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
        msvcrt.locking(handle, mode, 1)
        return
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _release_locked_handle(handle: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - exercised on the Windows gate
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _release_file_lock(lock: _CustodyLock) -> None:
    lock.close()


def _acquire_sqlite_lock(path: Path, *, exclusive: bool) -> _CustodyLock:
    lock_path = path.with_name(f".{path.name}.migration.lock")
    lock_path.touch(mode=0o600, exist_ok=True)
    lock_path.chmod(0o600)
    handle = os.open(lock_path, os.O_RDWR)
    try:
        _lock_file(handle, exclusive=exclusive)
    except Exception:
        os.close(handle)
        raise
    return _CustodyLock(handle)


def _sqlite_schema_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return migrations.current_version(connection)
    finally:
        connection.close()


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_snapshot(
    conn: sqlite3.Connection,
    columns_by_table: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, Any]]]:
    if columns_by_table is None:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations' ORDER BY name"
            )
        ]
        columns_by_table = {
            table: tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table)})"))
            for table in tables
        }
    snapshot: dict[str, dict[str, Any]] = {}
    for table, columns in columns_by_table.items():
        selected = ", ".join(_quote_sqlite_identifier(column) for column in columns)
        rows = [
            tuple(row) for row in conn.execute(f"SELECT {selected} FROM {_quote_sqlite_identifier(table)}").fetchall()
        ]
        encoded = json.dumps(
            sorted(rows, key=repr),
            ensure_ascii=False,
            default=lambda value: value.hex() if isinstance(value, bytes) else str(value),
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot[table] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return dict(columns_by_table), snapshot


def _sqlite_backup(source: sqlite3.Connection, destination: Path) -> None:
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
    destination.chmod(0o600)


def _write_migration_receipt(
    path: Path,
    *,
    from_version: int,
    backup_path: Path,
    snapshot: Mapping[str, Mapping[str, Any]],
) -> Path:
    receipt_path = path.with_name(f"{path.name}.migration-v{from_version}-v{migrations.LATEST_VERSION}.receipt.json")
    temporary = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.tmp")
    payload = (
        json.dumps(
            {
                "schema": "HOSPESSQLiteMigrationReceiptV1",
                "from_version": from_version,
                "to_version": migrations.LATEST_VERSION,
                "database": path.name,
                "backup": backup_path.name,
                "tables": snapshot,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, receipt_path)
    return receipt_path


def _migrate_existing_sqlite(path: Path) -> None:
    custody_lock = _acquire_sqlite_lock(path, exclusive=True)
    try:
        source = sqlite3.connect(path)
        try:
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA foreign_keys = ON")
            from_version = migrations.current_version(source)
            if from_version >= migrations.LATEST_VERSION:
                return
            checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                raise migrations.MigrationError("SQLite migration requires an idle WAL checkpoint")
            columns, before = _sqlite_snapshot(source)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = path.with_name(f"{path.name}.pre-v{from_version}-{stamp}.bak")
            if backup.exists():
                backup = path.with_name(f"{path.name}.pre-v{from_version}-{stamp}-{uuid.uuid4().hex[:8]}.bak")
            shadow = path.with_name(f".{path.name}.migration-{uuid.uuid4().hex}.sqlite3")
            _sqlite_backup(source, backup)
            _sqlite_backup(source, shadow)
        finally:
            source.close()
        shadow_connection = sqlite3.connect(shadow, factory=ManagedConnection)
        try:
            shadow_connection.row_factory = sqlite3.Row
            shadow_connection.execute("PRAGMA foreign_keys = ON")
            migrations.migrate(shadow_connection)
            violations = shadow_connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise migrations.MigrationError("SQLite shadow migration failed foreign-key validation")
            integrity = shadow_connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise migrations.MigrationError("SQLite shadow migration failed integrity check")
            _, after = _sqlite_snapshot(shadow_connection, columns)
            if before != after:
                raise migrations.MigrationError("SQLite shadow migration changed existing rows")
            shadow_connection.close()
            shadow.chmod(0o600)
            Path(f"{path}-wal").unlink(missing_ok=True)
            Path(f"{path}-shm").unlink(missing_ok=True)
            receipt_path = _write_migration_receipt(
                path,
                from_version=from_version,
                backup_path=backup,
                snapshot=after,
            )
            try:
                os.replace(shadow, path)
            except Exception:
                receipt_path.unlink(missing_ok=True)
                raise
        except Exception:
            shadow_connection.close()
            shadow.unlink(missing_ok=True)
            raise
    finally:
        _release_file_lock(custody_lock)


def connect(
    db_path: str | Path | None = None,
    *,
    query_only: bool = False,
    migrate: bool = True,
) -> DatabaseConnection:
    """Open a connection to the store, creating the schema on demand.

    The connection uses ``sqlite3.Row`` so rows behave like dicts, enables
    foreign-key cascades, and creates the parent directory (under ``out/``)
    when the default location is used.
    """
    target = _database_target(db_path)
    if query_only and migrate:
        migrate = False
    if _is_postgres_target(target):
        return _connect_postgres(str(target), query_only=query_only, migrate=migrate)
    path = resolve_db_path(target)
    if query_only and str(path) == ":memory:":
        raise ValueError("query-only stores require an existing database file")
    if query_only:
        if path.is_symlink() or not path.is_file():
            raise ValueError("query-only store must be an existing regular file")
        uri = f"{path.resolve().as_uri()}?mode=ro"
        custody_lock = _acquire_sqlite_lock(path, exclusive=False)
        try:
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, factory=ManagedConnection)
            conn.hold_custody_lock(custody_lock)
        except Exception:
            _release_file_lock(custody_lock)
            raise
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            conn.close()
            raise
        return conn
    if str(path) != ":memory:":
        parent_existed = path.parent.exists()
        # Default path lives under out/; make sure it exists.
        if path.parent == SERVICE_DB.parent:
            ensure_out_dirs()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            path.parent.chmod(0o700)
    if (
        migrate
        and str(path) != ":memory:"
        and path.is_file()
        and path.stat().st_size
        and _sqlite_schema_version(path) < migrations.LATEST_VERSION
    ):
        _migrate_existing_sqlite(path)
    custody_lock = None
    if str(path) != ":memory:":
        custody_lock = _acquire_sqlite_lock(path, exclusive=False)
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False, factory=ManagedConnection)
        if custody_lock is not None:
            conn.hold_custody_lock(custody_lock)
    except Exception:
        if custody_lock is not None:
            _release_file_lock(custody_lock)
        raise
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if migrate:
            migrations.migrate(conn)
        if str(path) != ":memory:":
            for runtime_file in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                if runtime_file.exists():
                    runtime_file.chmod(0o600)
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Row (de)serialization helpers.
# ---------------------------------------------------------------------------


def _encode(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if column in _BOOL_COLUMNS:
        return 1 if value else 0
    return value


def row_to_dict(row: Any | None) -> Optional[dict[str, Any]]:
    """Convert a Row to a plain dict, decoding JSON and bool columns."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        val = row[key]
        if key in _JSON_COLUMNS and val is not None:
            val = json.loads(val)
        elif key in _BOOL_COLUMNS and val is not None:
            val = bool(val)
        out[key] = val
    return out


def insert(conn: DatabaseConnection, table: str, values: dict[str, Any]) -> None:
    """Insert one record. Caller supplies the id and timestamps."""
    columns = list(values.keys())
    placeholders = ", ".join("?" for _ in columns)
    encoded = [_encode(c, values[c]) for c in columns]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        encoded,
    )


def update(conn: DatabaseConnection, table: str, record_id: str, changes: dict[str, Any]) -> None:
    """Update named columns on one record by id."""
    if not changes:
        return
    assignments = ", ".join(f"{c} = ?" for c in changes)
    encoded = [_encode(c, v) for c, v in changes.items()]
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        [*encoded, record_id],
    )


def fetch_one(conn: DatabaseConnection, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
    return row_to_dict(conn.execute(sql, tuple(params)).fetchone())


def fetch_all(conn: DatabaseConnection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [d for d in (row_to_dict(r) for r in conn.execute(sql, tuple(params)).fetchall()) if d]
