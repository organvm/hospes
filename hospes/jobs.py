"""Tenant/show-scoped durable leased jobs with immutable attempt receipts."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from . import generation, store

JOB_TYPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}$")
OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,159}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$")
OPAQUE_REFERENCE = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s]{3,512}$")
CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
EXECUTION_KINDS = frozenset({"operator", "scheduled", "internal"})
OPERATIONS = frozenset({"read", "prepare"})
SCHEDULED_JOB_PREFIXES = ("analytics.", "research.")
POLICY_VERSION = "jobs-v1"
MAX_COST_LIMIT_MINOR = 2500


class JobError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class JobValidationError(JobError):
    def __init__(self, detail: str):
        super().__init__(422, detail)


class JobConflict(JobError):
    def __init__(self, detail: str):
        super().__init__(409, detail)


@dataclass(frozen=True)
class JobSpec:
    tenant_id: str
    show_id: str
    job_type: str
    payload_ref: str
    payload_checksum: str
    idempotency_key: str
    created_by: str
    execution_kind: str = "operator"
    operation: str = "read"
    priority: int = 100
    max_attempts: int = 3
    timeout_seconds: int = 300
    cost_limit_minor: int = 0
    not_before: datetime | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class JobLease:
    job_id: str
    tenant_id: str
    show_id: str
    job_type: str
    payload_ref: str
    payload_checksum: str
    execution_kind: str
    operation: str
    attempt: int
    max_attempts: int
    timeout_seconds: int
    cost_limit_minor: int
    cost_spent_minor: int
    worker_id: str
    lease_token: str
    lease_expires_at: str


@dataclass(frozen=True)
class JobExecutionResult:
    result_ref: str
    cost_minor: int = 0


JobHandler = Callable[[JobLease], JobExecutionResult]


def _now(value: datetime | None = None) -> datetime:
    selected = value or generation.now()
    if selected.tzinfo is None:
        raise JobValidationError("job timestamps must include a timezone")
    return selected.astimezone(UTC)


def _parse_timestamp(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise JobConflict(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise JobConflict(f"{label} is invalid")
    return parsed.astimezone(UTC)


def _require_reference(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not OPAQUE_REFERENCE.fullmatch(normalized):
        raise JobValidationError(f"{label} must be an opaque reference")
    return normalized


def _validate_spec(spec: JobSpec) -> JobSpec:
    for value, label in (
        (spec.tenant_id, "tenant_id"),
        (spec.show_id, "show_id"),
        (spec.created_by, "created_by"),
    ):
        if not OPAQUE_ID.fullmatch(str(value)):
            raise JobValidationError(f"{label} must be an opaque identifier")
    if not JOB_TYPE.fullmatch(str(spec.job_type)):
        raise JobValidationError("job_type is invalid")
    _require_reference(spec.payload_ref, "payload_ref")
    if not CHECKSUM.fullmatch(str(spec.payload_checksum)):
        raise JobValidationError("payload_checksum must be a SHA-256 digest")
    if not IDEMPOTENCY_KEY.fullmatch(str(spec.idempotency_key)):
        raise JobValidationError("idempotency_key is invalid")
    if spec.execution_kind not in EXECUTION_KINDS:
        raise JobValidationError("execution_kind is invalid")
    if spec.operation not in OPERATIONS:
        raise JobValidationError("background jobs may only read or prepare")
    if spec.execution_kind == "scheduled" and not spec.job_type.startswith(
        SCHEDULED_JOB_PREFIXES
    ):
        raise JobValidationError(
            "scheduled jobs are restricted to analytics and research reads"
        )
    if spec.execution_kind == "scheduled" and spec.operation != "read":
        raise JobValidationError("scheduled jobs may only read external sources")
    for value, lower, upper, label in (
        (spec.priority, 0, 1000, "priority"),
        (spec.max_attempts, 1, 10, "max_attempts"),
        (spec.timeout_seconds, 1, 3600, "timeout_seconds"),
        (spec.cost_limit_minor, 0, MAX_COST_LIMIT_MINOR, "cost_limit_minor"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise JobValidationError(f"{label} is outside its policy bound")
    if spec.not_before is not None:
        _now(spec.not_before)
    if spec.correlation_id is not None and not OPAQUE_ID.fullmatch(spec.correlation_id):
        raise JobValidationError("correlation_id must be an opaque identifier")
    return spec


def enqueue(
    conn: store.DatabaseConnection,
    spec: JobSpec,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    spec = _validate_spec(spec)
    timestamp = _now(now).isoformat()
    show = store.fetch_one(
        conn,
        "SELECT id FROM show_registry WHERE tenant_id = ? AND show_id = ?",
        (spec.tenant_id, spec.show_id),
    )
    if show is None:
        raise JobValidationError("job scope is not a registered show")
    existing = store.fetch_one(
        conn,
        "SELECT * FROM background_jobs WHERE tenant_id = ? AND show_id = ? "
        "AND job_type = ? AND idempotency_key = ?",
        (spec.tenant_id, spec.show_id, spec.job_type, spec.idempotency_key),
    )
    immutable = {
        "payload_ref": spec.payload_ref,
        "payload_checksum": spec.payload_checksum,
        "execution_kind": spec.execution_kind,
        "operation": spec.operation,
        "max_attempts": spec.max_attempts,
        "timeout_seconds": spec.timeout_seconds,
        "cost_limit_minor": spec.cost_limit_minor,
    }
    if existing is not None:
        if any(existing[key] != value for key, value in immutable.items()):
            raise JobConflict("idempotency key is already bound to another job payload")
        return existing
    record = {
        "id": generation.new_id("background_job"),
        "tenant_id": spec.tenant_id,
        "show_id": spec.show_id,
        "job_type": spec.job_type,
        "status": "queued",
        "payload_ref": spec.payload_ref,
        "payload_checksum": spec.payload_checksum,
        "idempotency_key": spec.idempotency_key,
        "execution_kind": spec.execution_kind,
        "operation": spec.operation,
        "priority": spec.priority,
        "attempts": 0,
        "max_attempts": spec.max_attempts,
        "leased_by": None,
        "lease_token_hash": None,
        "lease_expires_at": None,
        "lease_started_at": None,
        "last_heartbeat_at": None,
        "timeout_seconds": spec.timeout_seconds,
        "cost_limit_minor": spec.cost_limit_minor,
        "cost_spent_minor": 0,
        "not_before": _now(spec.not_before).isoformat() if spec.not_before else None,
        "last_error_ref": None,
        "result_ref": None,
        "policy_version": POLICY_VERSION,
        "created_by": spec.created_by,
        "correlation_id": spec.correlation_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "completed_at": None,
    }
    store.insert(conn, "background_jobs", record)
    conn.commit()
    return record


def _begin(conn: store.DatabaseConnection) -> None:
    conn.commit()
    conn.execute("BEGIN" if conn.backend == "postgresql" else "BEGIN IMMEDIATE")


def _insert_attempt_receipt(
    conn: store.DatabaseConnection,
    row: Mapping[str, Any],
    *,
    outcome: str,
    completed_at: str,
    result_ref: str | None = None,
    error_ref: str | None = None,
    cost_minor: int = 0,
) -> None:
    existing = store.fetch_one(
        conn,
        "SELECT id FROM job_attempt_receipts WHERE tenant_id = ? AND show_id = ? "
        "AND job_id = ? AND attempt = ?",
        (row["tenant_id"], row["show_id"], row["id"], row["attempts"]),
    )
    if existing is not None:
        return
    store.insert(
        conn,
        "job_attempt_receipts",
        {
            "id": generation.new_id("job_attempt_receipt"),
            "tenant_id": row["tenant_id"],
            "show_id": row["show_id"],
            "job_id": row["id"],
            "attempt": row["attempts"],
            "worker_id": row["leased_by"] or "expired_worker",
            "outcome": outcome,
            "result_ref": result_ref,
            "error_ref": error_ref,
            "cost_minor": cost_minor,
            "payload_checksum": row["payload_checksum"],
            "started_at": row["lease_started_at"] or row["updated_at"],
            "completed_at": completed_at,
        },
    )


def _reap_expired(conn: store.DatabaseConnection, current: datetime) -> None:
    lock_clause = " FOR UPDATE SKIP LOCKED" if conn.backend == "postgresql" else ""
    expired = store.fetch_all(
        conn,
        "SELECT * FROM background_jobs WHERE status = 'leased' "
        "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?"
        f"{lock_clause}",
        (current.isoformat(),),
    )
    for row in expired:
        terminal = int(row["attempts"]) >= int(row["max_attempts"])
        _insert_attempt_receipt(
            conn,
            row,
            outcome="timeout",
            error_ref="error://jobs/lease-timeout",
            completed_at=current.isoformat(),
        )
        conn.execute(
            "UPDATE background_jobs SET status = ?, leased_by = NULL, "
            "lease_token_hash = NULL, lease_expires_at = NULL, "
            "last_error_ref = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (
                "failed" if terminal else "queued",
                "error://jobs/lease-timeout",
                current.isoformat() if terminal else None,
                current.isoformat(),
                row["id"],
            ),
        )


def lease_next(
    conn: store.DatabaseConnection,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    job_types: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> JobLease | None:
    if not OPAQUE_ID.fullmatch(str(worker_id)):
        raise JobValidationError("worker_id must be an opaque identifier")
    if isinstance(lease_seconds, bool) or not 5 <= lease_seconds <= 300:
        raise JobValidationError("lease_seconds must be between 5 and 300")
    if job_types is not None and (
        not job_types or any(not JOB_TYPE.fullmatch(item) for item in job_types)
    ):
        raise JobValidationError("job_types contains an invalid job type")
    current = _now(now)
    try:
        _begin(conn)
        _reap_expired(conn, current)
        params: list[Any] = [current.isoformat()]
        type_clause = ""
        if job_types:
            type_clause = " AND job_type IN (" + ",".join("?" for _ in job_types) + ")"
            params.extend(job_types)
        lock_clause = " FOR UPDATE SKIP LOCKED" if conn.backend == "postgresql" else ""
        row = store.fetch_one(
            conn,
            "SELECT * FROM background_jobs WHERE status = 'queued' "
            "AND attempts < max_attempts AND (not_before IS NULL OR not_before <= ?)"
            f"{type_clause} ORDER BY priority ASC, created_at ASC, id ASC LIMIT 1"
            f"{lock_clause}",
            params,
        )
        if row is None:
            conn.commit()
            return None
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        timeout_seconds = int(row["timeout_seconds"])
        effective_lease = min(lease_seconds, timeout_seconds)
        expiry = current + timedelta(seconds=effective_lease)
        conn.execute(
            "UPDATE background_jobs SET status = 'leased', attempts = attempts + 1, "
            "leased_by = ?, lease_token_hash = ?, lease_started_at = ?, "
            "last_heartbeat_at = ?, lease_expires_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (
                worker_id,
                token_hash,
                current.isoformat(),
                current.isoformat(),
                expiry.isoformat(),
                current.isoformat(),
                row["id"],
            ),
        )
        leased = store.fetch_one(conn, "SELECT * FROM background_jobs WHERE id = ?", (row["id"],))
        if leased is None or leased["status"] != "leased" or leased["leased_by"] != worker_id:
            raise JobConflict("job lease was claimed concurrently")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return JobLease(
        job_id=str(leased["id"]),
        tenant_id=str(leased["tenant_id"]),
        show_id=str(leased["show_id"]),
        job_type=str(leased["job_type"]),
        payload_ref=str(leased["payload_ref"]),
        payload_checksum=str(leased["payload_checksum"]),
        execution_kind=str(leased["execution_kind"]),
        operation=str(leased["operation"]),
        attempt=int(leased["attempts"]),
        max_attempts=int(leased["max_attempts"]),
        timeout_seconds=int(leased["timeout_seconds"]),
        cost_limit_minor=int(leased["cost_limit_minor"]),
        cost_spent_minor=int(leased["cost_spent_minor"]),
        worker_id=worker_id,
        lease_token=raw_token,
        lease_expires_at=expiry.isoformat(),
    )


def _leased_row(
    conn: store.DatabaseConnection,
    lease: JobLease,
    *,
    now: datetime,
) -> dict[str, Any]:
    row = store.fetch_one(
        conn,
        "SELECT * FROM background_jobs WHERE id = ? AND tenant_id = ? AND show_id = ?",
        (lease.job_id, lease.tenant_id, lease.show_id),
    )
    expected_hash = hashlib.sha256(lease.lease_token.encode("ascii")).hexdigest()
    if (
        row is None
        or row["status"] != "leased"
        or row["leased_by"] != lease.worker_id
        or row["lease_token_hash"] is None
        or not secrets.compare_digest(str(row["lease_token_hash"]), expected_hash)
        or int(row["attempts"]) != lease.attempt
    ):
        raise JobConflict("job lease is invalid")
    if _parse_timestamp(row["lease_expires_at"], "job lease expiry") <= now:
        raise JobConflict("job lease has expired")
    return row


def heartbeat(
    conn: store.DatabaseConnection,
    lease: JobLease,
    *,
    extension_seconds: int = 30,
    now: datetime | None = None,
) -> str:
    if isinstance(extension_seconds, bool) or not 5 <= extension_seconds <= 300:
        raise JobValidationError("heartbeat extension must be between 5 and 300 seconds")
    current = _now(now)
    try:
        _begin(conn)
        row = _leased_row(conn, lease, now=current)
        started = _parse_timestamp(row["lease_started_at"], "job lease start")
        deadline = started + timedelta(seconds=int(row["timeout_seconds"]))
        expiry = min(current + timedelta(seconds=extension_seconds), deadline)
        if expiry <= current:
            raise JobConflict("job timeout has elapsed")
        conn.execute(
            "UPDATE background_jobs SET last_heartbeat_at = ?, lease_expires_at = ?, "
            "updated_at = ? WHERE id = ?",
            (current.isoformat(), expiry.isoformat(), current.isoformat(), row["id"]),
        )
        conn.commit()
        return expiry.isoformat()
    except Exception:
        conn.rollback()
        raise


def _validate_cost(row: Mapping[str, Any], cost_minor: int) -> int:
    if isinstance(cost_minor, bool) or not isinstance(cost_minor, int) or cost_minor < 0:
        raise JobValidationError("job cost must be a non-negative integer")
    total = int(row["cost_spent_minor"]) + cost_minor
    limit = int(row["cost_limit_minor"])
    if total > limit:
        raise JobConflict("job cost limit would be exceeded")
    return total


def complete(
    conn: store.DatabaseConnection,
    lease: JobLease,
    result: JobExecutionResult,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    result_ref = _require_reference(result.result_ref, "result_ref")
    current = _now(now)
    try:
        _begin(conn)
        row = _leased_row(conn, lease, now=current)
        total_cost = _validate_cost(row, result.cost_minor)
        _insert_attempt_receipt(
            conn,
            row,
            outcome="succeeded",
            result_ref=result_ref,
            cost_minor=result.cost_minor,
            completed_at=current.isoformat(),
        )
        conn.execute(
            "UPDATE background_jobs SET status = 'succeeded', result_ref = ?, "
            "cost_spent_minor = ?, leased_by = NULL, lease_token_hash = NULL, "
            "lease_expires_at = NULL, completed_at = ?, updated_at = ? WHERE id = ?",
            (result_ref, total_cost, current.isoformat(), current.isoformat(), row["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return store.fetch_one(conn, "SELECT * FROM background_jobs WHERE id = ?", (lease.job_id,))


def fail(
    conn: store.DatabaseConnection,
    lease: JobLease,
    *,
    error_ref: str,
    retryable: bool,
    cost_minor: int = 0,
    retry_after_seconds: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_error = _require_reference(error_ref, "error_ref")
    if not isinstance(retryable, bool):
        raise JobValidationError("retryable must be a boolean")
    if isinstance(retry_after_seconds, bool) or not 0 <= retry_after_seconds <= 3600:
        raise JobValidationError("retry_after_seconds is outside its policy bound")
    current = _now(now)
    try:
        _begin(conn)
        row = _leased_row(conn, lease, now=current)
        total_cost = _validate_cost(row, cost_minor)
        will_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
        outcome = "retry" if will_retry else "failed"
        _insert_attempt_receipt(
            conn,
            row,
            outcome=outcome,
            error_ref=normalized_error,
            cost_minor=cost_minor,
            completed_at=current.isoformat(),
        )
        conn.execute(
            "UPDATE background_jobs SET status = ?, cost_spent_minor = ?, "
            "last_error_ref = ?, not_before = ?, leased_by = NULL, "
            "lease_token_hash = NULL, lease_expires_at = NULL, completed_at = ?, "
            "updated_at = ? WHERE id = ?",
            (
                "queued" if will_retry else "failed",
                total_cost,
                normalized_error,
                (current + timedelta(seconds=retry_after_seconds)).isoformat()
                if will_retry
                else None,
                None if will_retry else current.isoformat(),
                current.isoformat(),
                row["id"],
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return store.fetch_one(conn, "SELECT * FROM background_jobs WHERE id = ?", (lease.job_id,))


def run_once(
    conn: store.DatabaseConnection,
    *,
    worker_id: str,
    handlers: Mapping[str, JobHandler],
    lease_seconds: int = 60,
) -> dict[str, Any] | None:
    if not handlers:
        return None
    lease = lease_next(
        conn,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        job_types=tuple(sorted(handlers)) if handlers else None,
    )
    if lease is None:
        return None
    handler = handlers.get(lease.job_type)
    if handler is None:
        return fail(
            conn,
            lease,
            error_ref="error://jobs/unsupported-type",
            retryable=False,
        )
    try:
        result = handler(lease)
        if not isinstance(result, JobExecutionResult):
            raise TypeError("job handler returned an invalid result")
    except Exception:
        return fail(
            conn,
            lease,
            error_ref="error://jobs/handler-failure",
            retryable=True,
        )
    try:
        return complete(conn, lease, result)
    except JobError:
        return fail(
            conn,
            lease,
            error_ref="error://jobs/result-rejected",
            retryable=False,
        )


def run_available(
    conn: store.DatabaseConnection,
    *,
    worker_id: str,
    handlers: Mapping[str, JobHandler],
    max_jobs: int = 1,
    lease_seconds: int = 60,
) -> list[dict[str, Any]]:
    if isinstance(max_jobs, bool) or not 1 <= max_jobs <= 100:
        raise JobValidationError("max_jobs must be between 1 and 100")
    results: list[dict[str, Any]] = []
    for _index in range(max_jobs):
        result = run_once(
            conn,
            worker_id=worker_id,
            handlers=handlers,
            lease_seconds=lease_seconds,
        )
        if result is None:
            break
        results.append(result)
    return results


def list_jobs(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    status: str | None = None,
) -> list[dict[str, Any]]:
    for value, label in ((tenant_id, "tenant_id"), (show_id, "show_id")):
        if not OPAQUE_ID.fullmatch(str(value)):
            raise JobValidationError(f"{label} must be an opaque identifier")
    params: list[Any] = [tenant_id, show_id]
    clause = ""
    if status is not None:
        if status not in {"queued", "leased", "succeeded", "failed", "cancelled"}:
            raise JobValidationError("job status is invalid")
        clause = " AND status = ?"
        params.append(status)
    return store.fetch_all(
        conn,
        "SELECT * FROM background_jobs WHERE tenant_id = ? AND show_id = ?"
        f"{clause} ORDER BY created_at, id",
        params,
    )


__all__ = [
    "EXECUTION_KINDS",
    "JobConflict",
    "JobError",
    "JobExecutionResult",
    "JobHandler",
    "JobLease",
    "JobSpec",
    "JobValidationError",
    "POLICY_VERSION",
    "SCHEDULED_JOB_PREFIXES",
    "complete",
    "enqueue",
    "fail",
    "heartbeat",
    "lease_next",
    "list_jobs",
    "run_available",
    "run_once",
]
