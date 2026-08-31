"""Bounded research jobs: cost-capped retrieval, cited briefs, human review.

A research job is deliberately unglamorous. It runs one bounded query through
one named provider, keeps only sources the operator's allowlist already
permits, prices the call before it is made, and stops. Nothing it produces is
usable downstream until a human reviews it and locks it, and the locked content
is frozen against its own checksum so "reviewed" keeps meaning the thing that
was reviewed.

Retrieval never happens in this module. Providers are transport-injected
adapters in :mod:`hospes.integration_adapters`; the default transport refuses
to execute, so an estate without a credential wall records a visible blocked
provider receipt instead of an unbounded live fetch.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import yaml

from . import generation, integration_adapters, jobs, platform, providers, store
from .paths import CONFIG_DIR

JOB_TYPE = "research.provider_search"
MANUAL_PROVIDER = "manual_citations"
RETRIEVAL_PROVIDERS = frozenset({"brave", "serpapi", "searxng", "allowlisted_crawler"})
AUTHORIZED_RESEARCH_ROLES = frozenset({"producer", "editorial_owner"})
DEFAULT_MAX_COST_MINOR = 500
DEFAULT_MAX_RESULTS = 5
MAX_SEGMENT_CANDIDATES = 5
MAX_COUNTERARGUMENTS = 20
MAX_TEXT = 400
CAPABILITY = "research"


def _now(value: datetime | None = None) -> str:
    return (value or generation.now()).astimezone(timezone.utc).isoformat()


def _policy() -> dict[str, Any]:
    path = CONFIG_DIR / "research.yaml"
    if not path.exists():
        return {"max_cost_minor": DEFAULT_MAX_COST_MINOR, "requires_human_review": True}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return value if isinstance(value, dict) else {}


def _allowed_sources(policy: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    resolved = policy if policy is not None else _policy()
    sources = resolved.get("allowed_sources", [])
    if not isinstance(sources, list):
        return ()
    return tuple(str(item) for item in sources)


def _provider_settings(policy: Mapping[str, Any], provider: str) -> dict[str, Any]:
    declared = policy.get("provider_settings")
    settings: dict[str, Any] = {}
    if isinstance(declared, Mapping) and isinstance(declared.get(provider), Mapping):
        settings.update(dict(declared[provider]))
    costs = policy.get("provider_costs_minor")
    if isinstance(costs, Mapping) and provider in costs:
        settings["cost_per_query_minor"] = costs[provider]
    return settings


def _authorize(actor_role: Any, action: str) -> str:
    role = str(actor_role or "").strip()
    if role not in AUTHORIZED_RESEARCH_ROLES:
        raise platform.PlatformError(f"operator role cannot {action} bounded research", 403)
    return role


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return integration_adapters.ResearchSearchAdapter.clean_text(value, limit)


def _checksum(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_checksum(row: Mapping[str, Any]) -> str:
    """Freeze every reviewable field of a research job into one digest."""
    return _checksum(
        {
            "provider": row.get("provider"),
            "brief": row.get("brief") or {},
            "citations": row.get("citations") or [],
            "counterarguments": row.get("counterarguments") or [],
            "evidence": row.get("evidence") or [],
            "segment_candidates": row.get("segment_candidates") or [],
            "risk_flags": row.get("risk_flags") or [],
            "cost_minor": int(row.get("cost_minor") or 0),
        }
    )


def _assert_unmutated(row: Mapping[str, Any]) -> None:
    recorded = row.get("content_checksum")
    if recorded and str(recorded) != content_checksum(row):
        raise platform.PlatformError(
            "research content was mutated after it was frozen for review", 409
        )


def _build_query(
    policy: Mapping[str, Any], query_text: Any, seeds: Sequence[Any]
) -> integration_adapters.ResearchQuery:
    text = _text(query_text, 200)
    if not text:
        raise platform.PlatformError("a provider research job requires a bounded query")
    limit = policy.get("max_results", DEFAULT_MAX_RESULTS)
    try:
        return integration_adapters.ResearchQuery(
            text=text,
            limit=int(limit),
            seeds=tuple(str(item) for item in seeds),
            allowed_sources=_allowed_sources(policy),
        )
    except (TypeError, ValueError) as exc:
        raise platform.PlatformError("research result limit is not an integer") from exc
    except providers.ProviderError as exc:
        raise platform.PlatformError(exc.detail, exc.status_code) from exc


def _adapter(
    policy: Mapping[str, Any], provider: str, transport: Any | None
) -> integration_adapters.ResearchSearchAdapter:
    try:
        return integration_adapters.research_adapter(
            provider, transport=transport, settings=_provider_settings(policy, provider)
        )
    except providers.ProviderError as exc:
        raise platform.PlatformError(exc.detail, exc.status_code) from exc


def _scoped_job(conn: Any, *, job_id: str, tenant_id: str, show_id: str) -> dict[str, Any]:
    tenant, show = platform._scope(tenant_id, show_id)
    row = store.fetch_one(
        conn,
        "SELECT * FROM research_jobs WHERE id = ? AND tenant_id = ? AND show_id = ?",
        (platform._ref(job_id, "job_id"), tenant, show),
    )
    if not row:
        raise platform.PlatformError("research job not found", 404)
    return row


def _citation_allowed(url: str, allowed_sources: Iterable[str]) -> bool:
    return integration_adapters.source_allowed(url, allowed_sources)


def _provider_receipt(
    conn: Any,
    row: Mapping[str, Any],
    *,
    provider: str,
    status: str,
    details: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    receipt = {
        "id": generation.new_id("provider_receipt"),
        "tenant_id": row["tenant_id"],
        "show_id": row["show_id"],
        "capability": CAPABILITY,
        "provider": provider,
        "status": status,
        "receipt_ref": f"provider:{CAPABILITY}:{provider}:{row['id']}",
        "details": dict(details),
        "created_at": _now(now),
    }
    store.insert(conn, "provider_receipts", receipt)
    return receipt


def start_job(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    actor_role: str,
    provider: str = MANUAL_PROVIDER,
    max_cost_minor: int | None = None,
    query: str | None = None,
    seeds: Sequence[str] | None = None,
    requested_by: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Open one bounded research job and, for a retrieval provider, queue it."""
    _authorize(actor_role, "start")
    policy = _policy()
    policy_ceiling = int(policy.get("max_cost_minor", DEFAULT_MAX_COST_MINOR))
    ceiling = int(max_cost_minor if max_cost_minor is not None else policy_ceiling)
    if ceiling < 0:
        raise platform.PlatformError("research max cost cannot be negative")
    if ceiling > policy_ceiling:
        raise platform.PlatformError("research max cost exceeds the configured policy ceiling")
    allowed_providers = policy.get("providers", [])
    if isinstance(allowed_providers, list) and provider not in allowed_providers:
        raise platform.PlatformError("research provider is not allowed by policy")
    tenant, show = platform._scope(tenant_id, show_id)
    normalized_provider = platform._ref(provider, "provider")
    seed_list = [str(item) for item in (seeds or ())]
    retrieval = normalized_provider in RETRIEVAL_PROVIDERS
    request = _build_query(policy, query, seed_list) if retrieval else None
    if request is not None:
        projected = _adapter(policy, normalized_provider, None).cost_for(request)
        if projected > ceiling:
            raise platform.PlatformError(
                "research job would exceed its configured cost limit", 409
            )
    timestamp = _now(now)
    row = {
        "id": generation.new_id("research_job"),
        "tenant_id": tenant,
        "show_id": show,
        "guest_id": platform._guest(guest_id),
        "status": "queued" if retrieval else "researching",
        "provider": normalized_provider,
        "query_text": request.text if request is not None else None,
        "seeds": list(request.seeds) if request is not None else [],
        "brief": {},
        "citations": [],
        "counterarguments": [],
        "evidence": [],
        "segment_candidates": [],
        "risk_flags": [],
        "cost_minor": 0,
        "max_cost_minor": ceiling,
        "background_job_id": None,
        "provider_receipt_ref": None,
        "content_checksum": None,
        "failure_reason": None,
        "requested_by": platform._ref(requested_by, "requested_by") if requested_by else None,
        "review_receipt_ref": None,
        "lock_receipt_ref": None,
        "reviewed_at": None,
        "locked_at": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    store.insert(conn, "research_jobs", row)
    conn.commit()
    if request is None:
        return row
    payload = {
        "provider": normalized_provider,
        "guest_id": row["guest_id"],
        "query": request.text,
        "seeds": list(request.seeds),
        "limit": request.limit,
    }
    try:
        enqueued = jobs.enqueue(
            conn,
            jobs.JobSpec(
                tenant_id=tenant,
                show_id=show,
                job_type=JOB_TYPE,
                payload_ref=f"research://{row['id']}",
                payload_checksum=_checksum(payload),
                idempotency_key=f"research:{row['id']}",
                created_by=row["requested_by"] or "research_agent",
                execution_kind="operator",
                operation="read",
                max_attempts=1,
                timeout_seconds=120,
                cost_limit_minor=ceiling,
            ),
            now=now,
        )
    except jobs.JobError as exc:
        conn.execute("DELETE FROM research_jobs WHERE id = ?", (row["id"],))
        conn.commit()
        raise platform.PlatformError(exc.detail, exc.status_code) from exc
    store.update(
        conn,
        "research_jobs",
        row["id"],
        {"background_job_id": enqueued["id"], "updated_at": timestamp},
    )
    conn.commit()
    return _scoped_job(conn, job_id=row["id"], tenant_id=tenant, show_id=show)


def _fail(
    conn: Any,
    row: Mapping[str, Any],
    reason: str,
    *,
    provider: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    receipt = _provider_receipt(
        conn,
        row,
        provider=provider,
        status="blocked",
        details={"job_id": row["id"], "reason": _text(reason)},
        now=now,
    )
    store.update(
        conn,
        "research_jobs",
        row["id"],
        {
            "status": "failed",
            "failure_reason": _text(reason),
            "provider_receipt_ref": receipt["id"],
            "updated_at": _now(now),
        },
    )
    conn.commit()
    return receipt


def _segment_candidates(citations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "title": _text(citation["title"], 160),
            "objective": "Stress-test the cited claim against a concrete counterexample.",
            "citation_index": index,
            "origin": "retrieval",
        }
        for index, citation in enumerate(citations[:MAX_SEGMENT_CANDIDATES])
    ]


def execute_job(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    job_id: str,
    transport: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one queued provider search and freeze its cited, attributable brief."""
    row = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    if row["status"] != "queued":
        raise platform.PlatformError("only a queued research job can run a provider search", 409)
    policy = _policy()
    provider = str(row["provider"])
    request = _build_query(policy, row.get("query_text"), row.get("seeds") or [])
    adapter = _adapter(policy, provider, transport)
    projected = adapter.cost_for(request)
    if projected > int(row["max_cost_minor"]):
        _fail(conn, row, "research job would exceed its configured cost limit", provider=provider, now=now)
        raise platform.PlatformError("research job would exceed its configured cost limit", 409)
    timestamp = _now(now)
    store.update(conn, "research_jobs", row["id"], {"status": "researching", "updated_at": timestamp})
    conn.commit()
    try:
        findings = adapter.search(request)
    except providers.ProviderError as exc:
        _fail(conn, row, exc.detail, provider=provider, now=now)
        raise platform.PlatformError(exc.detail, exc.status_code) from exc
    allowed = _allowed_sources(policy)
    citations: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    risk_flags: list[dict[str, Any]] = []
    for finding in findings:
        item = finding.as_json()
        label = integration_adapters.source_label(item["url"])
        if not _citation_allowed(item["url"], allowed):
            risk_flags.append(
                {
                    "kind": "source_not_allowlisted",
                    "provider": item["provider"],
                    "source": label,
                    "rank": item["rank"],
                }
            )
            continue
        index = len(citations)
        citations.append(
            {
                "title": item["title"],
                "url": item["url"],
                "provider": item["provider"],
                "snippet": item["snippet"],
                "published_at": item["published_at"],
                "retrieved_at": timestamp,
                "rank": item["rank"],
            }
        )
        evidence.append(
            {
                "kind": "retrieval",
                "provider": item["provider"],
                "source": label,
                "url": item["url"],
                "citation_index": index,
                "retrieved_at": timestamp,
            }
        )
    if not citations:
        _fail(
            conn,
            row,
            "research job returned no allowlisted, citable source",
            provider=provider,
            now=now,
        )
        raise platform.PlatformError("research job returned no allowlisted, citable source", 422)
    brief = {
        "guest_id": row["guest_id"],
        "provider": provider,
        "query": request.text,
        "generated_at": timestamp,
        "candidate_claims": [
            {
                "index": index,
                "claim": citation["title"],
                "context": citation["snippet"],
                "citation_index": index,
                "source": integration_adapters.source_label(citation["url"]),
                "verified": False,
            }
            for index, citation in enumerate(citations)
        ],
    }
    receipt = _provider_receipt(
        conn,
        row,
        provider=provider,
        status="ready",
        details={
            "job_id": row["id"],
            "endpoint": adapter.resolved_endpoint(),
            "cost_minor": projected,
            "returned": len(findings),
            "cited": len(citations),
            "refused": len(risk_flags),
        },
        now=now,
    )
    changes = {
        "status": "ready_for_review",
        "brief": brief,
        "citations": citations,
        "evidence": evidence,
        "segment_candidates": _segment_candidates(citations),
        "risk_flags": risk_flags,
        "cost_minor": projected,
        "provider_receipt_ref": receipt["id"],
        "failure_reason": None,
        "updated_at": timestamp,
    }
    store.update(conn, "research_jobs", row["id"], changes)
    conn.commit()
    frozen = _scoped_job(conn, job_id=row["id"], tenant_id=tenant_id, show_id=show_id)
    store.update(
        conn, "research_jobs", row["id"], {"content_checksum": content_checksum(frozen)}
    )
    conn.commit()
    return _scoped_job(conn, job_id=row["id"], tenant_id=tenant_id, show_id=show_id)


def job_handler(conn: Any, *, transport: Any | None = None) -> jobs.JobHandler:
    """Adapt :func:`execute_job` to the durable leased-job substrate."""

    def handler(lease: jobs.JobLease) -> jobs.JobExecutionResult:
        job_id = str(lease.payload_ref).split("://", 1)[-1]
        row = execute_job(
            conn,
            tenant_id=lease.tenant_id,
            show_id=lease.show_id,
            job_id=job_id,
            transport=transport,
        )
        return jobs.JobExecutionResult(
            result_ref=f"research-brief://{job_id}", cost_minor=int(row["cost_minor"])
        )

    return handler


def run_job(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    job_id: str,
    worker_id: str,
    actor_role: str,
    transport: Any | None = None,
    max_jobs: int = 5,
) -> dict[str, Any]:
    """Drain leased research work until this job leaves the queue."""
    _authorize(actor_role, "run")
    row = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    if row["status"] != "queued":
        return row
    handlers = {JOB_TYPE: job_handler(conn, transport=transport)}
    try:
        for _attempt in range(max(1, int(max_jobs))):
            if jobs.run_once(conn, worker_id=worker_id, handlers=handlers) is None:
                break
            row = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
            if row["status"] != "queued":
                break
    except jobs.JobError as exc:
        raise platform.PlatformError(exc.detail, exc.status_code) from exc
    return _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)


def complete_job(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    job_id: str,
    actor_role: str,
    brief: Mapping[str, Any],
    citations: Sequence[Mapping[str, Any]],
    counterarguments: Sequence[str],
    cost_minor: int,
    evidence: Sequence[Mapping[str, Any]] | None = None,
    segment_candidates: Sequence[Mapping[str, Any]] | None = None,
    risk_flags: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record an operator-authored brief for a manual research job."""
    _authorize(actor_role, "complete")
    row = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    if row["status"] != "researching":
        raise platform.PlatformError("only a researching job can be completed", 409)
    if int(cost_minor) > int(row["max_cost_minor"]):
        raise platform.PlatformError("research job exceeded its configured cost limit", 409)
    if not citations or any(not item.get("url") or not item.get("title") for item in citations):
        raise platform.PlatformError("research brief requires titled citations")
    allowed_sources = _policy().get("allowed_sources", [])
    if not isinstance(allowed_sources, list) or any(
        not _citation_allowed(str(item["url"]), [str(source) for source in allowed_sources])
        for item in citations
    ):
        raise platform.PlatformError("research citation source is not allowed by policy")
    timestamp = _now(now)
    recorded_citations = [dict(item) for item in citations]
    changes = {
        "status": "ready_for_review",
        "brief": dict(brief),
        "citations": recorded_citations,
        "counterarguments": [_text(item) for item in counterarguments],
        "evidence": [dict(item) for item in (evidence or ())]
        or [
            {
                "kind": "operator_citation",
                "provider": row["provider"],
                "source": integration_adapters.source_label(item.get("url")),
                "url": str(item.get("url")),
                "citation_index": index,
                "retrieved_at": timestamp,
            }
            for index, item in enumerate(recorded_citations)
        ],
        "segment_candidates": [dict(item) for item in (segment_candidates or ())]
        or _segment_candidates(recorded_citations),
        "risk_flags": [dict(item) for item in (risk_flags or ())],
        "cost_minor": int(cost_minor),
        "updated_at": timestamp,
    }
    store.update(conn, "research_jobs", job_id, changes)
    conn.commit()
    frozen = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    store.update(conn, "research_jobs", job_id, {"content_checksum": content_checksum(frozen)})
    conn.commit()
    return _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)


def annotate_job(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    job_id: str,
    actor_role: str,
    verified_claim_indexes: Sequence[int] | None = None,
    counterarguments: Sequence[str] | None = None,
    segment_candidates: Sequence[Mapping[str, Any]] | None = None,
    risk_flags: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply the human editorial pass a retrieved brief cannot perform itself."""
    _authorize(actor_role, "annotate")
    row = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    if row["status"] != "ready_for_review":
        raise platform.PlatformError("only a ready research brief can be annotated", 409)
    _assert_unmutated(row)
    brief = dict(row.get("brief") or {})
    claims = [dict(item) for item in (brief.get("candidate_claims") or [])]
    for raw_index in verified_claim_indexes or ():
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise platform.PlatformError("verified claim indexes must be integers")
        if not 0 <= raw_index < len(claims):
            raise platform.PlatformError("verified claim index is outside the brief")
        claims[raw_index]["verified"] = True
    if claims:
        brief["candidate_claims"] = claims
    existing_counters = list(row.get("counterarguments") or [])
    added_counters = [_text(item) for item in (counterarguments or ()) if _text(item)]
    merged_counters = existing_counters + added_counters
    if len(merged_counters) > MAX_COUNTERARGUMENTS:
        raise platform.PlatformError(
            f"a research brief may carry at most {MAX_COUNTERARGUMENTS} counterarguments"
        )
    merged_segments = list(row.get("segment_candidates") or []) + [
        {**dict(item), "origin": "operator"} for item in (segment_candidates or ())
    ]
    merged_risks = list(row.get("risk_flags") or []) + [
        {**dict(item), "origin": "operator"} for item in (risk_flags or ())
    ]
    store.update(
        conn,
        "research_jobs",
        job_id,
        {
            "brief": brief,
            "counterarguments": merged_counters,
            "segment_candidates": merged_segments,
            "risk_flags": merged_risks,
            "updated_at": _now(now),
        },
    )
    conn.commit()
    annotated = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    store.update(conn, "research_jobs", job_id, {"content_checksum": content_checksum(annotated)})
    conn.commit()
    return _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)


def _review_ready(row: Mapping[str, Any], policy: Mapping[str, Any]) -> None:
    if not (row.get("citations") or []):
        raise platform.PlatformError("research review requires at least one citation")
    if policy.get("requires_counterargument", True) and not (row.get("counterarguments") or []):
        raise platform.PlatformError(
            "research review requires at least one recorded counterargument"
        )
    claims = (row.get("brief") or {}).get("candidate_claims") or []
    if claims and not any(item.get("verified") for item in claims):
        raise platform.PlatformError(
            "research review requires at least one operator-verified claim"
        )


def review_job(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    job_id: str,
    actor_role: str,
    reviewer_id: str,
    review_ref: str,
    approved: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the human decision that makes a brief usable, or refuses it."""
    _authorize(actor_role, "review")
    row = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    if row["status"] != "ready_for_review":
        raise platform.PlatformError("only a ready research brief can be reviewed", 409)
    if not isinstance(approved, bool):
        raise platform.PlatformError("approved must be a boolean")
    _assert_unmutated(row)
    if approved:
        _review_ready(row, _policy())
    timestamp = _now(now)
    checksum = content_checksum(row)
    receipt = {
        "id": generation.new_id("provider_receipt"),
        "tenant_id": row["tenant_id"],
        "show_id": row["show_id"],
        "capability": "research_review",
        "provider": "human",
        "status": "ready" if approved else "blocked",
        "receipt_ref": platform._ref(review_ref, "review_ref"),
        "details": {
            "job_id": job_id,
            "reviewer_id": platform._ref(reviewer_id, "reviewer_id"),
            "reviewer_role": _authorize(actor_role, "review"),
            "approved": approved,
            "content_checksum": checksum,
            "citations": len(row.get("citations") or []),
            "counterarguments": len(row.get("counterarguments") or []),
        },
        "created_at": timestamp,
    }
    store.insert(conn, "provider_receipts", receipt)
    store.update(
        conn,
        "research_jobs",
        job_id,
        {
            "status": "reviewed" if approved else "rejected",
            "review_receipt_ref": receipt["id"],
            "content_checksum": checksum,
            "reviewed_at": timestamp,
            "updated_at": timestamp,
        },
    )
    conn.commit()
    return _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)


def lock_job(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    job_id: str,
    actor_role: str,
    lock_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Freeze reviewed research for downstream use; never before review."""
    _authorize(actor_role, "lock")
    row = _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)
    if row["status"] != "reviewed":
        raise platform.PlatformError("human review is required before research lock", 403)
    _assert_unmutated(row)
    timestamp = _now(now)
    store.update(
        conn,
        "research_jobs",
        job_id,
        {
            "status": "locked",
            "lock_receipt_ref": platform._ref(lock_ref, "lock_ref"),
            "locked_at": timestamp,
            "updated_at": timestamp,
        },
    )
    conn.commit()
    return _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)


def get_job(conn: Any, *, tenant_id: str, show_id: str, job_id: str) -> dict[str, Any]:
    return _scoped_job(conn, job_id=job_id, tenant_id=tenant_id, show_id=show_id)


def list_jobs(
    conn: Any,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the scoped research queue the dashboard renders."""
    tenant, show = platform._scope(tenant_id, show_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise platform.PlatformError("limit must be between 1 and 200")
    clauses = ["tenant_id = ?", "show_id = ?"]
    params: list[Any] = [tenant, show]
    if guest_id is not None:
        clauses.append("guest_id = ?")
        params.append(platform._guest(guest_id))
    if status is not None:
        if status not in {
            "queued",
            "researching",
            "ready_for_review",
            "reviewed",
            "rejected",
            "locked",
            "failed",
        }:
            raise platform.PlatformError("research status is invalid")
        clauses.append("status = ?")
        params.append(status)
    params.append(limit)
    return store.fetch_all(
        conn,
        "SELECT * FROM research_jobs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    )


__all__ = [
    "AUTHORIZED_RESEARCH_ROLES", "CAPABILITY", "JOB_TYPE", "MANUAL_PROVIDER",
    "RETRIEVAL_PROVIDERS", "annotate_job", "complete_job", "content_checksum",
    "execute_job", "get_job", "job_handler", "list_jobs", "lock_job",
    "review_job", "run_job", "start_job",
]
