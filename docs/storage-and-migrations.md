# Storage and migration contract

HOSPES keeps the historical store API behind `store.DatabaseConnection`. The local profile still
opens a mode-0600 SQLite file. Hosted and hybrid profiles may set `HOSPES_DATABASE_URL` to a
PostgreSQL URL after installing the `hosted` extra; PostgreSQL must be version 16 or newer and the
URL may omit `sslmode` to accept HOSPES's enforced `require` default, or explicitly use `require`,
`verify-ca`, or `verify-full`. Weaker TLS modes are rejected. The adapter translates HOSPES qmark
bindings at the PostgreSQL boundary while preserving keyed and positional row access, so service
queries do not carry backend-specific syntax.

`HOSPES_DB` remains the local SQLite path override. A URL in `HOSPES_DB` is rejected to keep file
and hosted configuration unambiguous. `HOSPES_DATABASE_URL` takes precedence only when it is set.

## SQLite upgrades

An existing SQLite database is never migrated in place. Under a database-specific migration lock,
HOSPES performs this sequence:

1. acquire exclusive custody against the shared lock held for every live local connection;
2. checkpoint the WAL;
3. capture row counts and SHA-256 digests over every existing table and column;
4. create a mode-0600 online backup and a separate shadow copy;
5. apply ordered migrations only to the shadow;
6. run foreign-key and SQLite integrity checks;
7. recompute the original table/column snapshot and require an exact match;
8. durably stage the private migration receipt; and
9. atomically replace the database with the verified shadow, removing the staged receipt if the
   swap itself fails.

The retained backup is named `<database>.pre-v<old>-<timestamp>.bak`. To roll back, stop every
HOSPES process, preserve the failed database for diagnosis, copy the retained backup to a new path,
and open that path with `migrate=False` for verification before any replacement. Never delete the
backup until the upgraded database has passed an operator drill.

## Migration 9

Migration 9 adds composite tenant/show foreign-key targets and the hosted-runtime registries for
tenant encryption keys, authenticated identity mappings, assignments, background jobs, immutable
delivery receipts, and short-lived portal sessions. It also adds missing correlation, subject,
authorization-preview, actor, attempt, and checksum fields to existing audit/receipt tables.

SQLite and PostgreSQL share the same ordered migration ledger. PostgreSQL serializes migration
application with a transaction-scoped advisory lock. Verify both rails with:

```bash
python -m pytest tests/test_migrations.py tests/test_database_backends.py -q
bash scripts/verify-postgres-bootstrap.sh
```

The PostgreSQL acceptance command creates an ephemeral PostgreSQL 16 cluster with a temporary TLS
certificate, installs the hosted wheel into an isolated environment, verifies the complete current
migration ledger, and removes the entire cluster on exit.

## Migration 10

Migration 10 extends each tenant key record with wrapped DEK ciphertext, nonce, and authenticated
scope checksum. It adds composite-scoped `private_field_values` and `artifact_objects` tables,
including the one-active-key-per-tenant rule, field category allowlist, tenant/show foreign keys,
key-version foreign keys, encrypted metadata ownership, checksum custody, and staging/ready/failed
artifact states. The cryptographic and backend contract is documented in
[`encryption-and-artifact-custody.md`](encryption-and-artifact-custody.md).

## Migration 11

Migration 11 turns the background-job registry into an executable, policy-bound queue. It adds
execution kind, read/prepare operation, payload checksum, policy version, attributable creator,
correlation, lease timing, result, and completion fields. The new composite-scoped
`job_attempt_receipts` table records one immutable outcome per attempt, including the worker,
payload checksum, cost, timing, and opaque result or error reference.

The same migration adds attributable provisioning/revocation fields to identity mappings and
global provider-subject uniqueness for tenant-wide and exact-show scopes. This prevents concurrent
provisioning from creating an ambiguous external subject boundary.

PostgreSQL workers claim work with row locks and `SKIP LOCKED`; SQLite uses a serialized immediate
transaction. Lease tokens are returned once to the worker and only their SHA-256 digests are
persisted. Scheduled jobs are restricted to analytics and research reads. The authentication and
queue contract is documented in [`authentication-and-jobs.md`](authentication-and-jobs.md).
