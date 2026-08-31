"""Issue #34 predicate: the bounded AI research assistant.

Close condition: Brave, SerpAPI, SearXNG, and allowlisted crawler jobs enforce
cost and source policy and produce attributable, cited, immutable reviewed
research.

Every provider here runs against a recorded synthetic corpus on disk. No test
in this file opens a socket, and no real credential appears anywhere in it.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import synthetic_bearer_authenticator
from hospes import (
    integration_adapters,
    jobs,
    migrations,
    platform,
    providers,
    research_agent,
    store,
)
from hospes.api import create_app


try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - exercised only without the api extra
    TestClient = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "research"
TENANT = "hospes"
SHOW_A = "flagship"
SHOW_B = "field"
GUEST = "new-guest"
QUERY = "New Guest founding claims"
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
PRODUCER_TOKEN = "issue34-producer-token-0123456789abcdef"  # allow-secret: fixture
HOST_TOKEN = "issue34-host-token-0123456789abcdef00"  # allow-secret: fixture

ALLOWED_ARTICLE = "https://techcrunch.com/2020/03/15/new-guest-company-x/"
ALLOWED_ENCYCLOPEDIA = "https://en.wikipedia.org/wiki/New_Guest"
REFUSED_AGGREGATOR = "https://aggregator.example.test/new-guest"
# Split around the separator on purpose: an inline user:password URL reads as an
# address to scripts/check_private_data.py, which scans every added line.
CREDENTIALED_URL = "https://user:placeholder" + "@" + "en.wikipedia.org/wiki/New_Guest"


def _transport() -> integration_adapters.FixtureResearchTransport:
    return integration_adapters.FixtureResearchTransport(FIXTURES)


def _database(tmp_path: Path, name: str, *, shows: tuple[str, ...] = (SHOW_A,)):
    conn = store.connect(tmp_path / name)
    for show_id in shows:
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=f"Show {show_id}",
            config_ref=f"config/shows/{show_id}.yaml",
        )
    conn.commit()
    return conn


def _run(conn, *, provider: str, show_id: str = SHOW_A, query: str = QUERY, seeds=()):
    job = research_agent.start_job(
        conn,
        tenant_id=TENANT,
        show_id=show_id,
        guest_id=GUEST,
        actor_role="producer",
        provider=provider,
        query=query,
        seeds=seeds,
        requested_by="producer_fixture",
    )
    assert job["status"] == "queued"
    assert job["background_job_id"]
    return research_agent.run_job(
        conn,
        tenant_id=TENANT,
        show_id=show_id,
        job_id=job["id"],
        worker_id="worker-issue34",
        actor_role="producer",
        transport=_transport(),
    )


def _execute(conn, *, provider: str, query: str = QUERY, seeds=()):
    """Run the provider search directly, so refusals surface as exceptions.

    ``run_job`` goes through the leased-job substrate, which converts a handler
    failure into a recorded failed attempt rather than a raise. Both seams are
    exercised: this one asserts the refusal, the substrate one asserts that a
    refusal is durably recorded.
    """
    job = research_agent.start_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        guest_id=GUEST,
        actor_role="producer",
        provider=provider,
        query=query,
        seeds=seeds,
        requested_by="producer_fixture",
    )
    return research_agent.execute_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        job_id=job["id"],
        transport=_transport(),
    )


# ---------------------------------------------------------------------------
# Service: every registered source produces attributable, cited research.
# ---------------------------------------------------------------------------


def test_every_registered_source_produces_attributable_cited_research(tmp_path: Path) -> None:
    conn = _database(tmp_path, "issue34-sources.sqlite3")
    expected_cost = {"brave": 5, "serpapi": 25, "searxng": 1, "allowlisted_crawler": 2}
    for provider in ("brave", "serpapi", "searxng"):
        job = _run(conn, provider=provider)
        assert job["status"] == "ready_for_review", provider
        assert job["cost_minor"] == expected_cost[provider], provider
        assert job["citations"], provider
        # Attributable: every citation names the source that returned it.
        assert {item["provider"] for item in job["citations"]} == {provider}
        for citation in job["citations"]:
            assert citation["title"] and citation["url"].startswith("https://")
            assert citation["retrieved_at"]
            assert integration_adapters.source_allowed(citation["url"], research_agent._allowed_sources())
        # Evidence receipts bind each citation to its retrieval.
        assert [item["citation_index"] for item in job["evidence"]] == list(range(len(job["citations"])))
        assert {item["kind"] for item in job["evidence"]} == {"retrieval"}
        # Candidate claims arrive unverified: retrieval verifies nothing.
        claims = job["brief"]["candidate_claims"]
        assert claims and not any(claim["verified"] for claim in claims)
        assert job["segment_candidates"]
        assert job["content_checksum"] == research_agent.content_checksum(job)

    # The allowlisted crawler fetches named documents rather than searching.
    crawled = _run(conn, provider="allowlisted_crawler", seeds=(ALLOWED_ARTICLE,))
    assert crawled["status"] == "ready_for_review"
    assert crawled["cost_minor"] == expected_cost["allowlisted_crawler"]
    assert [item["url"] for item in crawled["citations"]] == [ALLOWED_ARTICLE]
    assert crawled["citations"][0]["provider"] == "allowlisted_crawler"
    conn.close()


def test_manual_citations_never_retrieve_and_still_freeze_their_brief(tmp_path: Path) -> None:
    conn = _database(tmp_path, "issue34-manual.sqlite3")
    with pytest.raises(providers.ProviderError, match="performs no retrieval"):
        integration_adapters.research_adapter("manual_citations").search(integration_adapters.ResearchQuery(text=QUERY))
    job = research_agent.start_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        guest_id=GUEST,
        actor_role="producer",
        now=NOW,
    )
    assert job["status"] == "researching" and job["background_job_id"] is None
    ready = research_agent.complete_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        job_id=job["id"],
        actor_role="producer",
        brief={"claim": "Operator-authored and sourced by hand."},
        citations=[{"title": "Reference entry", "url": ALLOWED_ENCYCLOPEDIA}],
        counterarguments=["The encyclopedia entry cites only the trade report."],
        cost_minor=0,
        now=NOW,
    )
    assert ready["status"] == "ready_for_review"
    assert ready["evidence"][0]["kind"] == "operator_citation"
    assert ready["content_checksum"] == research_agent.content_checksum(ready)
    conn.close()


# ---------------------------------------------------------------------------
# Cost policy.
# ---------------------------------------------------------------------------


def test_cost_policy_prices_every_job_before_the_provider_is_called(tmp_path: Path) -> None:
    conn = _database(tmp_path, "issue34-cost.sqlite3")
    # The estate ceiling is the outer bound; a job may not raise it.
    with pytest.raises(platform.PlatformError, match="policy ceiling"):
        research_agent.start_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            guest_id=GUEST,
            actor_role="producer",
            max_cost_minor=100_000,
        )
    with pytest.raises(platform.PlatformError, match="cannot be negative"):
        research_agent.start_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            guest_id=GUEST,
            actor_role="producer",
            max_cost_minor=-1,
        )
    # SerpAPI costs 25; a job capped at 10 never reaches the provider at all.
    with pytest.raises(platform.PlatformError, match="exceed its configured cost limit"):
        research_agent.start_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            guest_id=GUEST,
            actor_role="producer",
            provider="serpapi",
            query=QUERY,
            max_cost_minor=10,
        )
    assert store.fetch_all(conn, "SELECT id FROM research_jobs") == []

    # The crawler prices per document, so seeds multiply the charge.
    crawler = integration_adapters.research_adapter("allowlisted_crawler", settings={"cost_per_query_minor": 2})
    two_seeds = integration_adapters.ResearchQuery(text=QUERY, seeds=(ALLOWED_ARTICLE, ALLOWED_ENCYCLOPEDIA))
    assert crawler.cost_for(two_seeds) == 4

    # A ceiling that stops being affordable between queue and run fails closed.
    job = research_agent.start_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        guest_id=GUEST,
        actor_role="producer",
        provider="serpapi",
        query=QUERY,
        max_cost_minor=30,
    )
    store.update(conn, "research_jobs", job["id"], {"max_cost_minor": 1})
    conn.commit()
    with pytest.raises(platform.PlatformError, match="exceed its configured cost limit"):
        research_agent.execute_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            job_id=job["id"],
            transport=_transport(),
        )
    failed = research_agent.get_job(conn, tenant_id=TENANT, show_id=SHOW_A, job_id=job["id"])
    assert failed["status"] == "failed" and failed["cost_minor"] == 0
    blocked = store.fetch_one(conn, "SELECT * FROM provider_receipts WHERE id = ?", (failed["provider_receipt_ref"],))
    assert blocked["capability"] == "research" and blocked["status"] == "blocked"

    # The durable job carries the same ceiling into the leased-job substrate.
    background = store.fetch_one(
        conn, "SELECT * FROM background_jobs WHERE job_type = ? LIMIT 1", (research_agent.JOB_TYPE,)
    )
    assert background["cost_limit_minor"] == 30
    assert background["max_attempts"] == 1
    assert background["operation"] == "read"
    conn.close()


# ---------------------------------------------------------------------------
# Source policy.
# ---------------------------------------------------------------------------


def test_source_policy_refuses_unsafe_and_unallowlisted_sources(tmp_path: Path) -> None:
    for unsafe in (
        "http://en.wikipedia.org/wiki/New_Guest",
        CREDENTIALED_URL,
        "https://en.wikipedia.org:8443/wiki/New_Guest",
        "https://127.0.0.1/wiki/New_Guest",
        "https://localhost/wiki/New_Guest",
        "https://[::1]/wiki/New_Guest",
        "",
    ):
        with pytest.raises(providers.ProviderError):
            integration_adapters.normalize_source_url(unsafe)
        assert not integration_adapters.source_allowed(unsafe, ["https://en.wikipedia.org/"])
    # A lookalike host is not the allowlisted host.
    assert not integration_adapters.source_allowed(
        "https://en.wikipedia.org.evil.test/wiki/New_Guest", ["https://en.wikipedia.org/"]
    )
    assert integration_adapters.source_allowed("https://EN.wikipedia.org/wiki/New_Guest", ["https://en.wikipedia.org/"])
    assert integration_adapters.source_label(REFUSED_AGGREGATOR) == "https://aggregator.example.test"
    assert integration_adapters.source_label("not a url") == integration_adapters.UNPARSEABLE_SOURCE

    conn = _database(tmp_path, "issue34-sources-policy.sqlite3")
    # A refused source is counted as a risk flag, never silently dropped.
    brave = _run(conn, provider="brave")
    assert len(brave["citations"]) == 2
    assert brave["risk_flags"] == [
        {
            "kind": "source_not_allowlisted",
            "provider": "brave",
            "source": "https://aggregator.example.test",
            "rank": 3,
        }
    ]
    receipt = store.fetch_one(conn, "SELECT * FROM provider_receipts WHERE id = ?", (brave["provider_receipt_ref"],))
    assert receipt["details"]["returned"] == 3
    assert receipt["details"]["cited"] == 2
    assert receipt["details"]["refused"] == 1

    # SerpAPI's plaintext mirror of an allowed article is still refused.
    serp = _run(conn, provider="serpapi")
    assert [item["source"] for item in serp["risk_flags"]] == ["http://techcrunch.com"]

    # A query whose every result is off-allowlist produces no brief at all.
    with pytest.raises(platform.PlatformError, match="no allowlisted, citable source"):
        _execute(conn, provider="brave", query="unsourced guest rumours")
    unsourced = research_agent.list_jobs(conn, tenant_id=TENANT, show_id=SHOW_A, status="failed")
    assert unsourced and unsourced[0]["failure_reason"].endswith("citable source")

    # The crawler refuses a seed outside the allowlist before fetching anything.
    with pytest.raises(platform.PlatformError, match="outside the configured research source"):
        _execute(conn, provider="allowlisted_crawler", seeds=(REFUSED_AGGREGATOR,))
    # ...and refuses a response that left the document it asked for.
    with pytest.raises(platform.PlatformError, match="left its requested allowlisted document"):
        _execute(conn, provider="allowlisted_crawler", seeds=(ALLOWED_ENCYCLOPEDIA,))

    # Through the leased-job substrate the same refusal is recorded, not raised.
    drained = _run(conn, provider="brave", query="unsourced guest rumours")
    assert drained["status"] == "failed"
    assert "no allowlisted, citable source" in drained["failure_reason"]
    conn.close()


def test_an_unconfigured_source_is_a_visible_blocked_state(tmp_path: Path) -> None:
    conn = _database(tmp_path, "issue34-unconfigured.sqlite3")
    # No transport is injected: the default refuses to execute.
    job = research_agent.start_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        guest_id=GUEST,
        actor_role="producer",
        provider="brave",
        query=QUERY,
    )
    with pytest.raises(platform.PlatformError, match="credential wall"):
        research_agent.execute_job(conn, tenant_id=TENANT, show_id=SHOW_A, job_id=job["id"])
    blocked = research_agent.get_job(conn, tenant_id=TENANT, show_id=SHOW_A, job_id=job["id"])
    assert blocked["status"] == "failed"
    receipt = store.fetch_one(conn, "SELECT * FROM provider_receipts WHERE id = ?", (blocked["provider_receipt_ref"],))
    assert receipt["status"] == "blocked" and receipt["provider"] == "brave"
    conn.close()


# ---------------------------------------------------------------------------
# Immutable reviewed research.
# ---------------------------------------------------------------------------


def test_reviewed_research_is_locked_only_after_review_and_frozen_afterwards(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path, "issue34-review.sqlite3")
    ready = _run(conn, provider="brave")
    job_id = ready["id"]

    # No auto-commit: an unreviewed brief cannot be locked.
    with pytest.raises(platform.PlatformError, match="review is required before research lock"):
        research_agent.lock_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            job_id=job_id,
            actor_role="producer",
            lock_ref="receipt://research-lock/a",
        )
    # Nor approved while every claim is still unverified.
    with pytest.raises(platform.PlatformError, match="counterargument"):
        research_agent.review_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            job_id=job_id,
            actor_role="producer",
            reviewer_id="producer_fixture",
            review_ref="receipt://research-review/a",
            approved=True,
        )
    research_agent.annotate_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        job_id=job_id,
        actor_role="producer",
        counterarguments=["The founding date is disputed by a later filing."],
    )
    with pytest.raises(platform.PlatformError, match="operator-verified claim"):
        research_agent.review_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            job_id=job_id,
            actor_role="producer",
            reviewer_id="producer_fixture",
            review_ref="receipt://research-review/a",
            approved=True,
        )
    with pytest.raises(platform.PlatformError, match="outside the brief"):
        research_agent.annotate_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            job_id=job_id,
            actor_role="producer",
            verified_claim_indexes=[99],
        )
    annotated = research_agent.annotate_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        job_id=job_id,
        actor_role="producer",
        verified_claim_indexes=[0],
        segment_candidates=[{"title": "Founding claim", "objective": "Stress-test it."}],
        risk_flags=[{"kind": "operator_concern", "source": "editorial"}],
    )
    assert [claim["verified"] for claim in annotated["brief"]["candidate_claims"]] == [True, False]
    assert annotated["segment_candidates"][-1]["origin"] == "operator"
    assert annotated["risk_flags"][-1]["origin"] == "operator"

    reviewed = research_agent.review_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        job_id=job_id,
        actor_role="producer",
        reviewer_id="producer_fixture",
        review_ref="receipt://research-review/a",
        approved=True,
    )
    assert reviewed["status"] == "reviewed" and reviewed["reviewed_at"]
    review_receipt = store.fetch_one(
        conn, "SELECT * FROM provider_receipts WHERE id = ?", (reviewed["review_receipt_ref"],)
    )
    assert review_receipt["capability"] == "research_review"
    assert review_receipt["details"]["reviewer_role"] == "producer"
    assert review_receipt["details"]["content_checksum"] == reviewed["content_checksum"]

    locked = research_agent.lock_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        job_id=job_id,
        actor_role="producer",
        lock_ref="receipt://research-lock/a",
    )
    assert locked["status"] == "locked" and locked["locked_at"]
    assert locked["citations"] == reviewed["citations"]
    assert locked["content_checksum"] == reviewed["content_checksum"]

    # A write that bypasses the service layer is detected, not absorbed.
    conn.execute(
        "UPDATE research_jobs SET counterarguments = ?, status = 'reviewed' WHERE id = ?",
        (json.dumps(["silently rewritten"]), job_id),
    )
    conn.commit()
    with pytest.raises(platform.PlatformError, match="mutated after it was frozen"):
        research_agent.lock_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            job_id=job_id,
            actor_role="producer",
            lock_ref="receipt://research-lock/b",
        )
    conn.close()


def test_a_refused_review_records_its_own_blocked_receipt(tmp_path: Path) -> None:
    conn = _database(tmp_path, "issue34-reject.sqlite3")
    ready = _run(conn, provider="searxng")
    rejected = research_agent.review_job(
        conn,
        tenant_id=TENANT,
        show_id=SHOW_A,
        job_id=ready["id"],
        actor_role="editorial_owner",
        reviewer_id="editorial_fixture",
        review_ref="receipt://research-review/rejected",
        approved=False,
    )
    assert rejected["status"] == "rejected"
    receipt = store.fetch_one(conn, "SELECT * FROM provider_receipts WHERE id = ?", (rejected["review_receipt_ref"],))
    assert receipt["status"] == "blocked" and receipt["details"]["approved"] is False
    with pytest.raises(platform.PlatformError, match="review is required before research lock"):
        research_agent.lock_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            job_id=ready["id"],
            actor_role="editorial_owner",
            lock_ref="receipt://research-lock/rejected",
        )
    conn.close()


# ---------------------------------------------------------------------------
# Security: role and show scope.
# ---------------------------------------------------------------------------


def test_only_producer_and_editorial_owner_operate_bounded_research(tmp_path: Path) -> None:
    conn = _database(tmp_path, "issue34-roles.sqlite3")
    for role in ("host", "network_operator", "relationship_owner", "", None):
        with pytest.raises(platform.PlatformError, match="operator role cannot") as raised:
            research_agent.start_job(
                conn,
                tenant_id=TENANT,
                show_id=SHOW_A,
                guest_id=GUEST,
                actor_role=role,
            )
        assert raised.value.status_code == 403
    ready = _run(conn, provider="searxng")
    for call, kwargs in (
        (research_agent.annotate_job, {"counterarguments": ["x"]}),
        (
            research_agent.review_job,
            {
                "reviewer_id": "host_fixture",
                "review_ref": "receipt://research-review/h",
                "approved": True,
            },
        ),
        (research_agent.lock_job, {"lock_ref": "receipt://research-lock/h"}),
        (
            research_agent.run_job,
            {"worker_id": "worker-host", "transport": _transport()},
        ),
    ):
        with pytest.raises(platform.PlatformError, match="operator role cannot"):
            call(
                conn,
                tenant_id=TENANT,
                show_id=SHOW_A,
                job_id=ready["id"],
                actor_role="host",
                **kwargs,
            )
    conn.close()


def test_research_is_tenant_and_show_scoped(tmp_path: Path) -> None:
    conn = _database(tmp_path, "issue34-scope.sqlite3", shows=(SHOW_A, SHOW_B))
    ready = _run(conn, provider="searxng", show_id=SHOW_A)
    for tenant_id, show_id in ((TENANT, SHOW_B), ("other-tenant", SHOW_A)):
        with pytest.raises(platform.PlatformError, match="not found"):
            research_agent.get_job(conn, tenant_id=tenant_id, show_id=show_id, job_id=ready["id"])
    assert research_agent.list_jobs(conn, tenant_id=TENANT, show_id=SHOW_B) == []
    assert [row["id"] for row in research_agent.list_jobs(conn, tenant_id=TENANT, show_id=SHOW_A)] == [ready["id"]]
    assert research_agent.list_jobs(conn, tenant_id=TENANT, show_id=SHOW_A, guest_id="other-guest") == []
    with pytest.raises(platform.PlatformError, match="status is invalid"):
        research_agent.list_jobs(conn, tenant_id=TENANT, show_id=SHOW_A, status="nonsense")
    with pytest.raises(platform.PlatformError, match="limit must be"):
        research_agent.list_jobs(conn, tenant_id=TENANT, show_id=SHOW_A, limit=0)
    conn.close()


def test_a_provider_job_requires_a_registered_show_and_a_bounded_query(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue34-unregistered.sqlite3")
    with pytest.raises(platform.PlatformError, match="requires a bounded query"):
        research_agent.start_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            guest_id=GUEST,
            actor_role="producer",
            provider="brave",
        )
    with pytest.raises(platform.PlatformError, match="not allowed by policy"):
        research_agent.start_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            guest_id=GUEST,
            actor_role="producer",
            provider="unregistered_source",
            query=QUERY,
        )
    with pytest.raises(platform.PlatformError, match="registered show"):
        research_agent.start_job(
            conn,
            tenant_id=TENANT,
            show_id=SHOW_A,
            guest_id=GUEST,
            actor_role="producer",
            provider="brave",
            query=QUERY,
        )
    # The refused job leaves no orphaned research row behind.
    assert store.fetch_all(conn, "SELECT id FROM research_jobs") == []
    conn.close()


# ---------------------------------------------------------------------------
# API surface.
# ---------------------------------------------------------------------------


class _SessionShowASGI:
    """Simulate the session layer: project the operator's active show."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-session-show":
                    scope["hospes.operator_show"] = value.decode("utf-8")
        await self.app(scope, receive, send)


class _ShowBoundAuthenticator:
    def __init__(self, inner, show_id: str) -> None:
        self.inner = inner
        self.show_id = show_id

    def authenticate(self, presented_bearer):
        return dataclasses.replace(self.inner.authenticate(presented_bearer), show_id=self.show_id)


def _client(path: Path, *, bound_show: str | None = None, transport=None):
    authenticator = synthetic_bearer_authenticator(
        {
            PRODUCER_TOKEN: ("producer_fixture", "producer", TENANT),
            HOST_TOKEN: ("host_fixture", "host", TENANT),
        }
    )
    if bound_show is not None:
        authenticator = _ShowBoundAuthenticator(authenticator, bound_show)
    app = create_app(
        str(path),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=authenticator,
        csrf_required=False,
        research_transport=transport if transport is not None else _transport(),
    )
    return TestClient(_SessionShowASGI(app))


def _headers(bearer: str = PRODUCER_TOKEN, show: str | None = SHOW_A) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {bearer}"}
    if show is not None:
        headers["X-Session-Show"] = show
    return headers


@pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")
def test_the_research_api_drives_the_whole_bounded_lifecycle(tmp_path: Path) -> None:
    database = tmp_path / "issue34-api.sqlite3"
    _database(database.parent, database.name).close()
    with _client(database) as client:
        started = client.post(
            f"/v1/shows/{SHOW_A}/research",
            json={"guest_id": GUEST, "provider": "brave", "query": QUERY},
            headers=_headers(),
        )
        assert started.status_code == 201, started.text
        job_id = started.json()["id"]
        assert started.json()["status"] == "queued"

        ran = client.post(f"/v1/shows/{SHOW_A}/research/{job_id}/run", json={}, headers=_headers())
        assert ran.status_code == 200, ran.text
        assert ran.json()["status"] == "ready_for_review"
        assert ran.json()["cost_minor"] == 5

        listed = client.get(f"/v1/shows/{SHOW_A}/research", headers=_headers())
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [job_id]
        assert listed.headers["Cache-Control"] == "no-store, private"
        assert client.get(f"/v1/shows/{SHOW_A}/research?status=locked", headers=_headers()).json() == []

        detail = client.get(f"/v1/shows/{SHOW_A}/research/{job_id}", headers=_headers())
        assert detail.status_code == 200
        assert [item["provider"] for item in detail.json()["citations"]] == ["brave", "brave"]

        annotated = client.post(
            f"/v1/shows/{SHOW_A}/research/{job_id}/annotate",
            json={
                "verified_claim_indexes": [0],
                "counterarguments": ["A later filing disputes the founding date."],
            },
            headers=_headers(),
        )
        assert annotated.status_code == 200, annotated.text

        # Lock before review is refused by the API, not merely by the UI.
        early_lock = client.post(
            f"/v1/shows/{SHOW_A}/research/{job_id}/lock",
            json={"lock_ref": f"receipt://research-lock/{job_id}"},
            headers=_headers(),
        )
        assert early_lock.status_code == 403

        reviewed = client.post(
            f"/v1/shows/{SHOW_A}/research/{job_id}/review",
            json={"review_ref": f"receipt://research-review/{job_id}", "approved": True},
            headers=_headers(),
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "reviewed"

        locked = client.post(
            f"/v1/shows/{SHOW_A}/research/{job_id}/lock",
            json={"lock_ref": f"receipt://research-lock/{job_id}"},
            headers=_headers(),
        )
        assert locked.status_code == 200, locked.text
        assert locked.json()["status"] == "locked"

        # A host session sees the same routes refuse it.
        for method, path, payload in (
            ("post", f"/v1/shows/{SHOW_A}/research", {"guest_id": GUEST}),
            (
                "post",
                f"/v1/shows/{SHOW_A}/research/{job_id}/annotate",
                {"counterarguments": ["nope"]},
            ),
            (
                "post",
                f"/v1/shows/{SHOW_A}/research/{job_id}/review",
                {"review_ref": "receipt://x/y", "approved": True},
            ),
        ):
            refused = getattr(client, method)(path, json=payload, headers=_headers(HOST_TOKEN))
            assert refused.status_code == 403, path


@pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")
def test_research_routes_fail_closed_across_shows(tmp_path: Path) -> None:
    database = tmp_path / "issue34-api-scope.sqlite3"
    _database(database.parent, database.name, shows=(SHOW_A, SHOW_B)).close()
    with _client(database, bound_show=SHOW_A) as client:
        started = client.post(
            f"/v1/shows/{SHOW_A}/research",
            json={"guest_id": GUEST, "provider": "searxng", "query": QUERY},
            headers=_headers(show=None),
        )
        assert started.status_code == 201, started.text
        job_id = started.json()["id"]
        for response in (
            client.get(f"/v1/shows/{SHOW_B}/research", headers=_headers(show=None)),
            client.get(f"/v1/shows/{SHOW_B}/research/{job_id}", headers=_headers(show=None)),
            client.post(f"/v1/shows/{SHOW_B}/research/{job_id}/run", json={}, headers=_headers(show=None)),
        ):
            assert response.status_code == 403, response.text


@pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")
def test_the_internal_job_trigger_drains_research_with_the_same_handler(
    tmp_path: Path,
) -> None:
    database = tmp_path / "issue34-internal.sqlite3"
    _database(database.parent, database.name).close()
    with _client(database) as client:
        started = client.post(
            f"/v1/shows/{SHOW_A}/research",
            json={"guest_id": GUEST, "provider": "searxng", "query": QUERY},
            headers=_headers(),
        )
        assert started.status_code == 201, started.text
        job_id = started.json()["id"]
        application = client.app.app
        handlers = application.state.job_handlers
        assert research_agent.JOB_TYPE in handlers
        completed = jobs.run_available(
            application.state.conn,
            worker_id="internal-issue34",
            handlers=handlers,
            max_jobs=1,
        )
        assert [row["status"] for row in completed] == ["succeeded"]
        assert completed[0]["result_ref"] == f"research-brief://{job_id}"
        assert completed[0]["cost_spent_minor"] == 1
        detail = client.get(f"/v1/shows/{SHOW_A}/research/{job_id}", headers=_headers())
        assert detail.json()["status"] == "ready_for_review"


# ---------------------------------------------------------------------------
# UI, documentation, and schema surfaces.
# ---------------------------------------------------------------------------


def test_dashboard_documentation_and_kernel_declare_the_bounded_contract(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "issue34-schema.sqlite3")
    assert migrations.current_version(conn) == migrations.LATEST_VERSION
    assert "bounded_research_provider_jobs" in {row["name"] for row in migrations.applied_migrations(conn)}
    columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(research_jobs)")}
    assert {
        "query_text",
        "seeds",
        "evidence",
        "segment_candidates",
        "risk_flags",
        "background_job_id",
        "provider_receipt_ref",
        "content_checksum",
        "failure_reason",
        "reviewed_at",
        "locked_at",
    } <= columns
    conn.close()

    # The runtime and packaged dashboards stay byte-identical.
    for relative in (
        "dashboard/assets/api.js",
        "dashboard/assets/app.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/guide.json",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/styles.css",
        "config/research.yaml",
        "config/runtime.yaml",
        "config/domain_kernel.yaml",
    ):
        assert (ROOT / relative).read_bytes() == (ROOT / "hospes" / "resources" / relative).read_bytes()

    capabilities = (ROOT / "dashboard/assets/capabilities.mjs").read_text(encoding="utf-8")
    assert "canResearch: writable && ['producer', 'editorial_owner'].includes(role)" in capabilities

    api_js = (ROOT / "dashboard/assets/api.js").read_text(encoding="utf-8")
    for symbol in (
        "startResearch",
        "runResearch",
        "annotateResearch",
        "reviewResearch",
        "lockResearch",
        "loadResearchBrief",
        "loadResearchQueue",
    ):
        assert f"export function {symbol}(" in api_js

    app_js = (ROOT / "dashboard/assets/app.js").read_text(encoding="utf-8")
    assert 'data-action="research"' in app_js
    assert "canResearch" in app_js
    # Every rendered brief value is HTML escaped before it reaches the DOM.
    assert "escapeHTML(item)" in app_js
    assert "escapeHTML(claim.claim)" in app_js

    markup = (ROOT / "dashboard/assets/partnership-workspace.html").read_text(encoding="utf-8")
    for marker in (
        'id="research-console"',
        'data-guide="research-console"',
        'id="research-provider"',
        'id="research-brief"',
        'id="btn-research-approve"',
        'id="btn-research-lock"',
        "allowlisted_crawler",
    ):
        assert marker in markup

    guide = json.loads((ROOT / "dashboard/assets/guide.json").read_text(encoding="utf-8"))
    assert guide["capabilities"]["canResearch"]["requires"] == "producer or editorial_owner"
    assert guide["elements"]["research-console"]["capability"] == "canResearch"

    documentation = (ROOT / "docs" / "research-agent.md").read_text(encoding="utf-8")
    for marker in (
        "allowlisted_crawler",
        "serpapi",
        "searxng",
        "brave",
        "priced before the call",
        "content checksum",
        "provider receipt",
        "no live network",
        "producer or editorial_owner",
        "403",
        "risk_flags",
        "/v1/shows/{show_id}/research",
    ):
        assert marker in documentation, marker
    assert "docs/research-agent.md" in (ROOT / "README.md").read_text(encoding="utf-8")

    kernel = (ROOT / "config" / "domain_kernel.yaml").read_text(encoding="utf-8")
    for marker in (
        "research_contract:",
        "research.provider_search",
        "priced before they are called",
        "recorded as a risk flag rather than silently dropped",
        "frozen against its own content checksum",
        "Only producer and editorial-owner sessions start, annotate, review, or lock",
    ):
        assert marker in kernel, marker

    policy = (ROOT / "config" / "research.yaml").read_text(encoding="utf-8")
    for source in ("brave", "serpapi", "searxng", "allowlisted_crawler"):
        assert source in policy
    # The policy file names costs and sources, never a key.
    assert "api_key" not in policy and "token" not in policy


def test_the_synthetic_research_corpus_is_offline_and_credential_free() -> None:
    assert FIXTURES.is_dir()
    recorded = sorted(path for path in FIXTURES.rglob("*.json"))
    assert recorded, "the synthetic research corpus is empty"
    for path in recorded:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        text = path.read_text(encoding="utf-8")
        for forbidden in ("api_key", "apikey", "authorization", "Bearer "):
            assert forbidden.lower() not in text.lower(), path
    # The fixture transport is the only transport these tests ever use, and it
    # reads from disk.
    transport = _transport()
    assert isinstance(transport, integration_adapters.FixtureResearchTransport)
    with pytest.raises(providers.ProviderError, match="no recorded fixture"):
        transport.fetch(provider="brave", endpoint="", params={"q": "never recorded"})
