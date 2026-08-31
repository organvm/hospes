"""Provider-neutral analytics imports, scheduled reads, trends, and export.

The analytics organ is deliberately a **read** organ. It admits provider
observations three ways — an operator CSV import, a bounded provider pull, and
a scheduled read job — and every one of them lands the same validated record
shape declared by ``spec/analytics.schema.json``.

Three boundaries hold the surface honest:

* **No metric without a provider receipt.** Every persisted row carries
  ``source_receipt_ref``, which points at the ``provider_receipts`` row that
  authorized the read. :func:`fetch` writes those receipts *before* it imports
  anything, so a blocked or unconfigured provider leaves a visible receipt and
  no metrics rather than silently disappearing.
* **Reads only.** :func:`schedule_fetch` enqueues ``analytics.fetch`` as a
  ``scheduled``/``read`` job; :mod:`hospes.jobs` rejects any other execution
  kind or operation for that namespace. Nothing here sends, publishes, or
  mutates an external system.
* **Visible unconfigured state.** A provider without an import path, or one
  whose credential wall is not provisioned, is reported as ``unconfigured`` or
  ``blocked``. It is never substituted with another provider's numbers.

Stored metric values keep their provider spelling and type verbatim; numeric
coercion happens only in the :func:`trends` projection, so the record remains
faithful to the source while the dashboard still charts it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from . import generation, jobs, platform, providers, store
from .paths import CONFIG_DIR, SPEC_DIR


SUPPORTED_PROVIDERS = frozenset(
    {"spotify_creator_csv", "youtube_analytics", "megaphone_chartable_import", "custom_csv_http"}
)

#: The job type scheduled analytics reads run under. ``hospes.jobs`` only
#: admits the ``analytics.``/``research.`` namespaces for scheduled work.
FETCH_JOB_TYPE = "analytics.fetch"

#: Canonical metric names. Counters are summed across providers for one
#: episode; ratios are averaged. Anything else is carried through untouched.
COUNTER_METRICS = ("downloads", "streams", "top_clip_views")
RATIO_METRICS = ("retention_pct", "avg_consumption")
TREND_METRICS = COUNTER_METRICS + RATIO_METRICS

#: Provider column spellings mapped onto the canonical metric names.
METRIC_ALIASES = {
    "retention_%": "retention_pct",
    "retention%": "retention_pct",
    "retention_percent": "retention_pct",
    "retention": "retention_pct",
    "avg_consumption_pct": "avg_consumption",
    "average_consumption": "avg_consumption",
    "top_clips": "top_clip_views",
    "top_clip": "top_clip_views",
}

#: Cells a spreadsheet would evaluate as a formula. Neutralized on export.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_NUMERIC = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_RESERVED_ROW_KEYS = frozenset({"episode_id", "period_start", "period_end"})

_SCHEMA_CACHE: dict[str, Any] = {}


class AnalyticsError(ValueError):
    """Raised when an analytics read violates the schema or its boundaries."""

    def __init__(self, detail: str, status_code: int = 422):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


class AnalyticsAdapter:
    """Small contract shared by authenticated and file-import providers."""

    provider = "custom_csv_http"

    def fetch(self, rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return list(rows)


class SpotifyCreatorCsvAdapter(AnalyticsAdapter):
    provider = "spotify_creator_csv"


class YouTubeAnalyticsAdapter(AnalyticsAdapter):
    provider = "youtube_analytics"


class MegaphoneChartableAdapter(AnalyticsAdapter):
    provider = "megaphone_chartable_import"


# ---------------------------------------------------------------------------
# Schema validation (stdlib only; the shipped JSON Schema is the authority).
# ---------------------------------------------------------------------------


def schema() -> dict[str, Any]:
    """Return the parsed ``spec/analytics.schema.json`` contract."""
    if "analytics" not in _SCHEMA_CACHE:
        path = Path(SPEC_DIR) / "analytics.schema.json"
        _SCHEMA_CACHE["analytics"] = json.loads(path.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE["analytics"]


def _type_matches(value: Any, declared: Any) -> bool:
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        if name == "string" and isinstance(value, str):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "object" and isinstance(value, dict):
            return True
    return False


def _check_metrics(value: Any, contract: Mapping[str, Any]) -> None:
    if not isinstance(value, dict):
        raise AnalyticsError("metrics must be an object")
    named = contract.get("properties", {})
    extra = contract.get("additionalProperties", {})
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise AnalyticsError("metric names must be non-empty text")
        declared = named.get(key, extra).get("type")
        if not _type_matches(item, declared):
            raise AnalyticsError(f"metric {key} must be typed {declared}")


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one normalized record against the shipped analytics schema."""
    contract = schema()
    properties: Mapping[str, Any] = contract["properties"]
    missing = [name for name in contract["required"] if not str(record.get(name, "")).strip()]
    if missing:
        raise AnalyticsError(f"analytics record is missing {', '.join(sorted(missing))}")
    unknown = sorted(set(record) - set(properties))
    if unknown:
        raise AnalyticsError(f"analytics record carries undeclared fields: {', '.join(unknown)}")
    for name, item in record.items():
        declared = properties[name]
        if name == "metrics":
            _check_metrics(item, declared)
            continue
        if not _type_matches(item, declared.get("type")):
            raise AnalyticsError(f"{name} must be typed {declared.get('type')}")
        enum = declared.get("enum")
        if enum is not None and item not in enum:
            raise AnalyticsError(f"{name} must be one of {', '.join(sorted(enum))}")
        pattern = declared.get("pattern")
        if pattern is not None and not re.fullmatch(pattern, item):
            raise AnalyticsError(f"{name} does not match the schema pattern for this field")
        if declared.get("minLength") and len(item) < int(declared["minLength"]):
            raise AnalyticsError(f"{name} is shorter than the schema minimum")
    return dict(record)


# ---------------------------------------------------------------------------
# Normalization and import.
# ---------------------------------------------------------------------------


def canonical_metric_name(name: str) -> str:
    """Map a provider column spelling onto its canonical metric name."""
    key = str(name).strip()
    return METRIC_ALIASES.get(key.lower(), key)


def normalize_row(
    row: Mapping[str, Any], *, provider: str, source_receipt_ref: str, now: datetime | None = None
) -> dict[str, Any]:
    """Return one schema-valid analytics record from a provider row."""
    if provider not in SUPPORTED_PROVIDERS:
        raise AnalyticsError(f"unsupported analytics provider: {provider}")
    required = ("episode_id", "period_start", "period_end")
    if any(not str(row.get(key, "")).strip() for key in required):
        raise AnalyticsError("analytics rows require episode_id, period_start, and period_end")
    try:
        period_start = date.fromisoformat(str(row["period_start"]).strip())
        period_end = date.fromisoformat(str(row["period_end"]).strip())
    except ValueError as exc:
        raise AnalyticsError("analytics periods must use ISO 8601 dates") from exc
    if period_start > period_end:
        raise AnalyticsError("analytics period_start cannot be after period_end")
    metrics: dict[str, Any] = {}
    for key, value in row.items():
        if key in _RESERVED_ROW_KEYS or key is None or str(value).strip() == "":
            continue
        metrics[canonical_metric_name(key)] = value
    record = {
        "provider": provider,
        "episode_id": str(row["episode_id"]).strip(),
        "metrics": metrics,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "fetched_at": (now or generation.now()).astimezone(timezone.utc).isoformat(),
        "source_receipt_ref": str(source_receipt_ref).strip(),
    }
    return validate_record(record)


def import_rows(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    provider: str,
    rows: Iterable[Mapping[str, Any]],
    source_receipt_ref: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Idempotently persist normalized provider rows for one tenant/show."""
    tenant, show = platform._scope(tenant_id, show_id)
    written: list[dict[str, Any]] = []
    for row in rows:
        normalized = normalize_row(row, provider=provider, source_receipt_ref=source_receipt_ref, now=now)
        existing = store.fetch_one(
            conn,
            "SELECT * FROM analytics_metrics WHERE tenant_id = ? AND show_id = ? AND provider = ? "
            "AND episode_id = ? AND period_start = ? AND period_end = ?",
            (
                tenant,
                show,
                normalized["provider"],
                normalized["episode_id"],
                normalized["period_start"],
                normalized["period_end"],
            ),
        )
        if existing:
            written.append(existing)
            continue
        record = {"id": generation.new_id("analytics"), "tenant_id": tenant, "show_id": show, **normalized}
        store.insert(conn, "analytics_metrics", record)
        written.append(record)
    conn.commit()
    return written


def import_csv(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    provider: str,
    path: str | Path,
    source_receipt_ref: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Import a provider CSV export the operator placed on disk."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return import_rows(
            conn,
            tenant_id=tenant_id,
            show_id=show_id,
            provider=provider,
            rows=csv.DictReader(handle),
            source_receipt_ref=source_receipt_ref,
            now=now,
        )


# ---------------------------------------------------------------------------
# Provider policy, receipts, and the bounded pull.
# ---------------------------------------------------------------------------


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Return the tracked ``config/analytics.yaml`` policy (never a secret)."""
    resolved = Path(path) if path is not None else Path(CONFIG_DIR) / "analytics.yaml"
    if not resolved.exists():
        return {"version": 1, "providers": {}}
    value = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise AnalyticsError("config/analytics.yaml must be a mapping")
    return value


def provider_import_path(policy: Mapping[str, Any], provider: str) -> str | None:
    """Return the operator-declared export path for one provider, if any."""
    entry = (policy.get("providers") or {}).get(provider) or {}
    if not isinstance(entry, Mapping):
        raise AnalyticsError(f"analytics provider entry for {provider} must be a mapping")
    declared = entry.get("import_path")
    text = str(declared).strip() if declared is not None else ""
    return text or None


def _receipt_reference(receipt: Mapping[str, Any]) -> str:
    return f"receipt://analytics/{receipt['provider']}/{receipt['id']}"


def fetch(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    provider: str | None = None,
    source_path: str | Path | None = None,
    registry: providers.ProviderRegistry | None = None,
    policy: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify every analytics provider, receipt it, and import what is readable.

    Nothing here reaches the network: a configured adapter prepares a bounded
    read envelope, and the observations themselves come from the operator's own
    provider export. A provider whose credential wall is unprovisioned stays
    ``blocked``; one without a declared export path stays ``unconfigured``.
    """
    tenant, show = platform._scope(tenant_id, show_id)
    if provider is not None and provider not in SUPPORTED_PROVIDERS:
        raise AnalyticsError(f"unsupported analytics provider: {provider}")
    if source_path is not None and provider is None:
        raise AnalyticsError("an explicit analytics source path requires an explicit provider")
    selected_registry = registry or providers.ProviderRegistry()
    active_policy = policy if policy is not None else load_policy()
    receipts = selected_registry.record_verification(
        conn, tenant_id=tenant, show_id=show, capability="analytics", now=now
    )
    results: list[dict[str, Any]] = []
    imported = 0
    for receipt in receipts:
        name = str(receipt["provider"])
        if provider is not None and name != provider:
            continue
        outcome: dict[str, Any] = {
            "provider": name,
            "receipt_id": receipt["id"],
            "receipt_ref": _receipt_reference(receipt),
            "status": "blocked",
            "imported": 0,
            "reason": str(receipt.get("details", {}).get("reason", "provider is not ready")),
        }
        if receipt["status"] != "ready" or name not in SUPPORTED_PROVIDERS:
            if name not in SUPPORTED_PROVIDERS and receipt["status"] == "ready":
                outcome["reason"] = "provider is not an implemented analytics adapter"
            results.append(outcome)
            continue
        adapter = selected_registry.choose("analytics", preferred=(name,))
        envelope = adapter.execute("fetch", {"capability": "analytics", "tenant_id": tenant, "show_id": show})
        outcome["mode"] = str(getattr(adapter, "mode", "unknown"))
        outcome["prepared"] = str(envelope.get("status", ""))
        declared = str(source_path) if source_path is not None else provider_import_path(active_policy, name)
        if not declared:
            outcome["status"] = "unconfigured"
            outcome["reason"] = "no import_path is declared for this provider in config/analytics.yaml"
            results.append(outcome)
            continue
        export = Path(declared)
        if not export.is_file():
            outcome["status"] = "unavailable"
            outcome["reason"] = "the declared provider export is not present on disk"
            results.append(outcome)
            continue
        rows = import_csv(
            conn,
            tenant_id=tenant,
            show_id=show,
            provider=name,
            path=export,
            source_receipt_ref=outcome["receipt_ref"],
            now=now,
        )
        outcome["status"] = "imported"
        outcome["imported"] = len(rows)
        outcome["reason"] = "provider export imported"
        imported += len(rows)
        results.append(outcome)
    if provider is not None and not results:
        raise AnalyticsError(f"{provider} is not a configured analytics provider", 404)
    return {
        "tenant_id": tenant,
        "show_id": show,
        "fetched_at": (now or generation.now()).astimezone(timezone.utc).isoformat(),
        "imported": imported,
        "providers": results,
    }


def list_receipts(conn: Any, *, tenant_id: str, show_id: str) -> list[dict[str, Any]]:
    """Return the analytics provider receipts recorded for one tenant/show."""
    tenant, show = platform._scope(tenant_id, show_id)
    return store.fetch_all(
        conn,
        "SELECT * FROM provider_receipts WHERE tenant_id = ? AND show_id = ? AND capability = 'analytics' "
        "ORDER BY created_at DESC, id",
        (tenant, show),
    )


# ---------------------------------------------------------------------------
# Scheduled reads.
# ---------------------------------------------------------------------------


def _schedule_payload(tenant: str, show: str, provider: str | None) -> tuple[str, str]:
    body = json.dumps(
        {
            "capability": "analytics",
            "operation": "read",
            "tenant_id": tenant,
            "show_id": show,
            "provider": provider or "all",
        },
        sort_keys=True,
    ).encode("utf-8")
    return f"analytics://{tenant}/{show}/{provider or 'all'}", hashlib.sha256(body).hexdigest()


def schedule_fetch(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    created_by: str,
    provider: str | None = None,
    period: str | None = None,
    not_before: datetime | None = None,
    cost_limit_minor: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Enqueue the read-only scheduled analytics pull for one tenant/show.

    The job is always ``execution_kind='scheduled'`` and ``operation='read'``;
    :mod:`hospes.jobs` rejects anything else in this namespace, so a scheduled
    analytics job can never acquire send or publish authority.
    """
    tenant, show = platform._scope(tenant_id, show_id)
    if provider is not None and provider not in SUPPORTED_PROVIDERS:
        raise AnalyticsError(f"unsupported analytics provider: {provider}")
    payload_ref, checksum = _schedule_payload(tenant, show, provider)
    window = period or (now or generation.now()).astimezone(timezone.utc).date().isoformat()
    spec = jobs.JobSpec(
        tenant_id=tenant,
        show_id=show,
        job_type=FETCH_JOB_TYPE,
        payload_ref=payload_ref,
        payload_checksum=checksum,
        idempotency_key=f"analytics:{show}:{provider or 'all'}:{window}",
        created_by=created_by,
        execution_kind="scheduled",
        operation="read",
        cost_limit_minor=cost_limit_minor,
        not_before=not_before,
    )
    return jobs.enqueue(conn, spec, now=now)


def job_handler(
    conn: Any,
    *,
    registry: providers.ProviderRegistry | None = None,
    policy: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> jobs.JobHandler:
    """Return the worker handler for ``analytics.fetch`` scheduled reads."""

    def handle(lease: jobs.JobLease) -> jobs.JobExecutionResult:
        result = fetch(
            conn,
            tenant_id=lease.tenant_id,
            show_id=lease.show_id,
            registry=registry,
            policy=policy,
            now=now,
        )
        digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest()
        return jobs.JobExecutionResult(result_ref=f"analytics://{lease.tenant_id}/{lease.show_id}/fetch/{digest}")

    return handle


# ---------------------------------------------------------------------------
# Reporting: listing, trends, and export.
# ---------------------------------------------------------------------------


def list_metrics(conn: Any, *, tenant_id: str, show_id: str, episode_id: str | None = None) -> list[dict[str, Any]]:
    """Return stored analytics observations, newest period first."""
    tenant, show = platform._scope(tenant_id, show_id)
    params: list[Any] = [tenant, show]
    clause = ""
    if episode_id:
        clause = " AND episode_id = ?"
        params.append(str(episode_id).strip())
    return store.fetch_all(
        conn,
        f"SELECT * FROM analytics_metrics WHERE tenant_id = ? AND show_id = ?{clause} "
        "ORDER BY period_end DESC, episode_id",
        params,
    )


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").rstrip("%")
    if not _NUMERIC.fullmatch(text):
        return None
    return float(text)


def _round(value: float) -> float | int:
    rounded = round(value, 2)
    return int(rounded) if rounded == int(rounded) else rounded


def trends(conn: Any, *, tenant_id: str, show_id: str, limit: int = 12) -> dict[str, Any]:
    """Project the last ``limit`` episodes as chartable per-episode series.

    Counters (downloads, streams, top clip views) are summed across the
    providers that reported the episode; ratios (retention, average
    consumption) are averaged over the providers that reported them. Episodes
    are returned oldest first so a chart reads left to right.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise AnalyticsError("trend limit must be between 1 and 100")
    rows = list_metrics(conn, tenant_id=tenant_id, show_id=show_id)
    episodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        episode = str(row["episode_id"])
        bucket = episodes.setdefault(
            episode,
            {
                "episode_id": episode,
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "providers": [],
                "values": {},
            },
        )
        if str(row["period_end"]) > str(bucket["period_end"]):
            bucket["period_end"] = row["period_end"]
            bucket["period_start"] = row["period_start"]
        if row["provider"] not in bucket["providers"]:
            bucket["providers"].append(row["provider"])
        for name, value in (row.get("metrics") or {}).items():
            number = _numeric(value)
            if number is None:
                continue
            bucket["values"].setdefault(name, []).append(number)
    ordered = sorted(episodes.values(), key=lambda item: (str(item["period_end"]), str(item["episode_id"])))
    selected = ordered[-limit:]
    points: list[dict[str, Any]] = []
    for bucket in selected:
        point: dict[str, Any] = {
            "episode_id": bucket["episode_id"],
            "period_start": bucket["period_start"],
            "period_end": bucket["period_end"],
            "providers": sorted(bucket["providers"]),
        }
        for name, values in bucket["values"].items():
            if name in RATIO_METRICS:
                point[name] = _round(sum(values) / len(values))
            elif name in COUNTER_METRICS:
                point[name] = _round(sum(values))
            else:
                point[name] = _round(sum(values))
        points.append(point)
    series: dict[str, list[float | int | None]] = {}
    totals: dict[str, float | int] = {}
    for name in TREND_METRICS:
        column = [point.get(name) for point in points]
        if all(value is None for value in column):
            continue
        series[name] = column
        present = [value for value in column if value is not None]
        totals[name] = _round(sum(present)) if name in COUNTER_METRICS else _round(sum(present) / len(present))
    return {
        "tenant_id": platform._ref(tenant_id, "tenant_id"),
        "show_id": platform._ref(show_id, "show_id"),
        "limit": limit,
        "episodes": points,
        "series": series,
        "totals": totals,
        "episodes_available": len(ordered),
    }


def _csv_safe(value: Any) -> str:
    """Neutralize a spreadsheet formula while leaving real numbers intact."""
    text = "" if value is None else str(value)
    if not text or _NUMERIC.fullmatch(text.strip()):
        return text
    return f"'{text}" if text[0] in _CSV_FORMULA_PREFIXES else text


def export_csv(conn: Any, *, tenant_id: str, show_id: str, episode_id: str | None = None) -> str:
    """Render stored analytics as reporting CSV with canonical metric columns."""
    rows = list_metrics(conn, tenant_id=tenant_id, show_id=show_id, episode_id=episode_id)
    output = io.StringIO()
    fields = [
        "provider",
        "episode_id",
        "period_start",
        "period_end",
        "fetched_at",
        *TREND_METRICS,
        "top_clip_ref",
        "metrics",
        "source_receipt_ref",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        metrics = row.get("metrics") or {}
        record = {
            field: _csv_safe(row.get(field, ""))
            for field in ("provider", "episode_id", "period_start", "period_end", "fetched_at", "source_receipt_ref")
        }
        for name in (*TREND_METRICS, "top_clip_ref"):
            record[name] = _csv_safe(metrics.get(name, ""))
        record["metrics"] = _csv_safe(json.dumps(metrics, sort_keys=True))
        writer.writerow(record)
    return output.getvalue()


def verify_provider(
    registry: providers.ProviderRegistry, capability: str = "analytics"
) -> list[providers.ProviderResult]:
    return registry.verify(capability)


__all__ = [
    "AnalyticsAdapter",
    "AnalyticsError",
    "COUNTER_METRICS",
    "FETCH_JOB_TYPE",
    "MegaphoneChartableAdapter",
    "METRIC_ALIASES",
    "RATIO_METRICS",
    "SUPPORTED_PROVIDERS",
    "SpotifyCreatorCsvAdapter",
    "TREND_METRICS",
    "YouTubeAnalyticsAdapter",
    "canonical_metric_name",
    "export_csv",
    "fetch",
    "import_csv",
    "import_rows",
    "job_handler",
    "list_metrics",
    "list_receipts",
    "load_policy",
    "normalize_row",
    "provider_import_path",
    "schedule_fetch",
    "schema",
    "trends",
    "validate_record",
    "verify_provider",
]
