#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
POSTGRES_BIN="${HOSPES_POSTGRES_BIN:-$(pg_config --bindir)}"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hospes-postgres.XXXXXX")"
DATA_DIR="$TEMP_ROOT/data"
VENV_DIR="$TEMP_ROOT/venv"
SERVER_LOG="$TEMP_ROOT/postgres.log"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
SERVER_STARTED=0

cleanup() {
  if [[ "$SERVER_STARTED" == "1" ]]; then
    "$POSTGRES_BIN/pg_ctl" -D "$DATA_DIR" -m fast -w stop >/dev/null
  fi
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT

"$POSTGRES_BIN/initdb" -D "$DATA_DIR" -A trust -U postgres --no-locale --encoding=UTF8 >/dev/null
openssl req -new -x509 -days 1 -nodes \
  -out "$DATA_DIR/server.crt" \
  -keyout "$DATA_DIR/server.key" \
  -subj "/CN=localhost" >/dev/null 2>&1
chmod 600 "$DATA_DIR/server.key"
if ! "$POSTGRES_BIN/pg_ctl" -D "$DATA_DIR" \
  -l "$SERVER_LOG" \
  -o "-h 127.0.0.1 -k '$TEMP_ROOT' -p $PORT -c ssl=on" \
  -w start >/dev/null; then
  echo "PostgreSQL bootstrap server failed; server log follows" >&2
  sed -n '1,200p' "$SERVER_LOG" >&2
  exit 1
fi
SERVER_STARTED=1
"$POSTGRES_BIN/createdb" -h 127.0.0.1 -p "$PORT" -U postgres hospes

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install "$ROOT[hosted]" >/dev/null

HOSPES_TEST_DATABASE_URL="postgresql://postgres@127.0.0.1:$PORT/hospes?sslmode=require" \
  "$VENV_DIR/bin/python" - <<'PY'
import os
import hashlib

import jwt

from hospes import jobs, migrations, partnership_projections, store

connection = store.connect(os.environ["HOSPES_TEST_DATABASE_URL"])
assert connection.backend == "postgresql"
assert migrations.current_version(connection) == migrations.LATEST_VERSION
tables = {
    row["table_name"]
    for row in connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()"
    ).fetchall()
}
expected_tables = {
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
}
missing_tables = expected_tables - tables
assert not missing_tables, {"missing": sorted(missing_tables), "actual": sorted(tables)}
assert connection.execute("SELECT COUNT(*) AS count FROM schema_migrations").fetchone()[0] == len(migrations.MIGRATIONS)
timestamp = "2026-08-10T00:00:00+00:00"
assert jwt.__version__
store.insert(connection, "show_registry", {
    "id": "postgres-show",
    "tenant_id": "synthetic-tenant",
    "show_id": "postgres-show",
    "label": "PostgreSQL show",
    "config_ref": "config://synthetic/postgres-show",
    "status": "active",
    "created_at": timestamp,
    "updated_at": timestamp,
})
jobs.enqueue(connection, jobs.JobSpec(
    tenant_id="synthetic-tenant",
    show_id="postgres-show",
    job_type="analytics.fetch",
    payload_ref="job-payload://synthetic/postgres",
    payload_checksum=hashlib.sha256(b"postgres-job").hexdigest(),
    idempotency_key="postgres:analytics:acceptance",
    created_by="postgres-worker",
    execution_kind="scheduled",
    operation="read",
))
lease = jobs.lease_next(connection, worker_id="postgres-worker")
assert lease is not None
completed_job = jobs.complete(
    connection,
    lease,
    jobs.JobExecutionResult("job-result://synthetic/postgres"),
)
assert completed_job["status"] == "succeeded"
assert connection.execute(
    "SELECT COUNT(*) AS count FROM job_attempt_receipts"
).fetchone()[0] == 1
store.insert(connection, "partnerships", {
    "id": "postgres-acceptance",
    "tenant_id": "synthetic-tenant",
    "partnership_key": "postgres_acceptance",
    "label": "PostgreSQL acceptance",
    "purpose": "Exercise the hosted command-center catalog path.",
    "status": "active",
    "created_at": timestamp,
    "updated_at": timestamp,
})
command_center = partnership_projections.command_center(
    connection, "postgres-acceptance", "synthetic-tenant"
)
assert command_center["engine_capabilities"]["candidate_intake"] is True
connection.commit()
assert len(migrations.migrate(connection)) == len(migrations.MIGRATIONS)
connection.close()
PY

echo "PostgreSQL 16 TLS bootstrap acceptance passed"
