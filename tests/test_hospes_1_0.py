from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hospes import analytics, configuration, distribution, guest_portal, network_dashboard, network_graph, onboarding, platform, providers, research_agent, store
from hospes import integration_adapters, nurture
from hospes.__main__ import main
from hospes.api import create_app
from conftest import synthetic_bearer_authenticator

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]


def _conn(tmp_path: Path):
    return store.connect(tmp_path / "platform.sqlite3")


def test_configuration_and_provider_registry_expose_additive_modes():
    runtime = configuration.load_runtime()
    assert set(runtime.profiles) == {"local", "tunnel", "hosted", "hybrid"}
    statuses = configuration.capability_report(runtime)
    assert any(item.status == "unconfigured" for item in statuses)
    assert any(item.provider == "manual" and item.status == "ready" for item in statuses)
    registry = providers.ProviderRegistry(runtime)
    assert registry.choose("calendar").name == "ics"
    assert registry.verify("calendar")[0].status == "ready"


def test_platform_records_are_scoped_and_outbound_is_authorized(tmp_path: Path):
    conn = _conn(tmp_path)
    platform.register_show(conn, tenant_id="tenant-a", show_id="flagship", label="Flagship", config_ref="config://flagship")
    platform.register_show(conn, tenant_id="tenant-a", show_id="field", label="Field", config_ref="config://field")
    platform.record_contact_route(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", route_type="publicist", route_ref="owner://route/theo", provenance_ref="source://prior-archive", verified_at="2026-08-10T00:00:00+00:00")
    platform.record_touchpoint(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", channel="text", notes_ref="vault://touchpoint/1", occurred_at="2026-08-09T00:00:00+00:00", initiator="ari")
    platform.add_relationship_edge(conn, tenant_id="tenant-a", show_id="flagship", source_guest_id="ari", target_guest_id="theo-von", edge_type="knows", relationship_class="C1", provenance_ref="source://edge/1")
    platform.add_relationship_edge(conn, tenant_id="tenant-a", show_id="flagship", source_guest_id="theo-von", target_guest_id="mark-norman", edge_type="knows", relationship_class="C2", provenance_ref="source://edge/2")
    graph = network_graph.graph(conn, tenant_id="tenant-a", show_id="flagship", root_guest_id="ari", depth=2, config_path=tmp_path / "missing.yaml")
    assert [node["id"] for node in graph["nodes"]] == ["ari", "theo-von", "mark-norman"]
    assert "graph LR" in network_graph.render_graph(graph, "mermaid")
    assert "digraph" in network_graph.render_graph(graph, "graphviz")
    assert network_graph.render_graph(graph, "json").startswith("{")
    with pytest.raises(platform.PlatformError):
        platform.record_contact_route(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", route_type="email", route_ref="person@example.test", provenance_ref="source://bad", verified_at="now")
    platform.record_guest_history(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", season="1", episode="5", disposition="SOFT_DECLINE", notes_ref="vault://history/1")
    platform.set_do_not_contact(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", notes_ref="vault://dnc/1")
    assert platform.guest_history(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von")[-1]["do_not_contact"]
    sponsor = platform.add_sponsor(conn, tenant_id="tenant-a", show_id="flagship", name="Fixture Sponsor", contact_ref="vault://sponsor/contact", terms_ref="vault://sponsor/terms")
    platform.add_sponsorship(conn, tenant_id="tenant-a", show_id="flagship", sponsor_id=sponsor["id"], episode_id="episode-1", slot_type="mid", rate_minor=1000)
    clearance = platform.add_clearance(conn, tenant_id="tenant-a", show_id="flagship", episode_id="episode-1", clearance_type="music", rights_holder_ref="vault://rights/1", license_terms_ref="vault://license/1")
    draft = distribution.create_draft(conn, tenant_id="tenant-a", show_id="flagship", episode_id="episode-1", platform_name="rss", metadata={"title": "Fixture"}, idempotency_key="episode-1:rss")
    assert draft["status"] == "blocked"
    platform.update_clearance(conn, tenant_id="tenant-a", show_id="flagship", clearance_id=clearance["id"], status="cleared")
    draft = distribution.create_draft(conn, tenant_id="tenant-a", show_id="flagship", episode_id="episode-2", platform_name="rss", metadata={"title": "Fixture"}, idempotency_key="episode-2:rss")
    with pytest.raises(platform.PlatformError):
        distribution.mark_published(conn, tenant_id="tenant-a", show_id="flagship", distribution_id=draft["id"], external_id_ref="external://episode-2")
    distribution.authorize_publish(conn, tenant_id="tenant-a", show_id="flagship", distribution_id=draft["id"], outbound_mode="manual_receipt", authorized_by="owner", authorization_ref="receipt://human/publish-2", idempotency_key="episode-2:rss:human")
    published = distribution.mark_published(conn, tenant_id="tenant-a", show_id="flagship", distribution_id=draft["id"], external_id_ref="external://episode-2")
    assert published["status"] == "published"
    assert "<title>Fixture</title>" in distribution.rss_preview({"title": "Fixture"})
    platform.notify(conn, tenant_id="tenant-a", show_id="flagship", recipient_role="producer", notification_type="draft_ready", title="Review", body_ref="vault://notification/1", entity_ref="episode-2")
    assert len(platform.list_notifications(conn, tenant_id="tenant-a", show_id="flagship", recipient_role="producer", unread_only=True)) == 1
    assert platform.tenant_summary(conn, tenant_id="tenant-a", show_id="field")["candidates"] == 0


def test_analytics_research_portal_and_network_report(tmp_path: Path):
    conn = _conn(tmp_path)
    platform.register_show(conn, tenant_id="tenant-a", show_id="flagship", label="Flagship", config_ref="config://flagship")
    metrics = analytics.import_rows(conn, tenant_id="tenant-a", show_id="flagship", provider="spotify_creator_csv", rows=[{"episode_id": "episode-1", "period_start": "2026-08-01", "period_end": "2026-08-07", "downloads": "12"}], source_receipt_ref="receipt://analytics/1")
    assert metrics[0]["metrics"]["downloads"] == "12"
    assert "episode-1" in analytics.export_csv(conn, tenant_id="tenant-a", show_id="flagship")
    job = research_agent.start_job(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", actor_role="producer")
    ready = research_agent.complete_job(conn, tenant_id="tenant-a", show_id="flagship", job_id=job["id"], actor_role="producer", brief={"segment_candidates": ["claim"]}, citations=[{"title": "Public source", "url": "https://en.wikipedia.org/wiki/Podcast"}], counterarguments=["Needs review"], cost_minor=10)
    assert ready["status"] == "ready_for_review"
    with pytest.raises(platform.PlatformError):
        research_agent.lock_job(conn, tenant_id="tenant-a", show_id="flagship", job_id=job["id"], actor_role="producer", lock_ref="receipt://lock/before-review")
    research_agent.review_job(conn, tenant_id="tenant-a", show_id="flagship", job_id=job["id"], actor_role="producer", reviewer_id="producer", review_ref="receipt://review/1", approved=True)
    assert research_agent.lock_job(conn, tenant_id="tenant-a", show_id="flagship", job_id=job["id"], actor_role="producer", lock_ref="receipt://lock/1")["status"] == "locked"
    link = guest_portal.create_portal_token(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", secret="portal-secret-for-tests", base_url="https://guest.example", enabled=True)  # allow-secret: synthetic test fixture
    token = link["url"].split("#token=", 1)[1]  # allow-secret: synthetic one-time token
    intake = guest_portal.complete_intake(conn, token=token, secret="portal-secret-for-tests", consent_ref="vault://consent/1", intake_ref="vault://intake/1", availability_ref="vault://availability/1")  # allow-secret: synthetic test fixture
    assert intake["guest_id"] == "theo-von"
    with pytest.raises(guest_portal.PortalError):
        guest_portal.complete_intake(conn, token=token, secret="portal-secret-for-tests", consent_ref="vault://consent/1", intake_ref="vault://intake/1", availability_ref="vault://availability/1")  # allow-secret: synthetic test fixture
    if TestClient is not None:
        portal_app = guest_portal.create_app(conn=conn, secret="portal-secret-for-tests", enabled=True)  # allow-secret: synthetic test fixture
        with TestClient(portal_app) as client:
            assert client.get("/guest/").status_code == 200
            assert "noindex" in client.headers.get("x-robots-tag", "noindex") or client.get("/guest/").headers["x-robots-tag"] == "noindex, nofollow"
        disabled = guest_portal.create_app(conn=conn, secret="portal-secret-for-tests", enabled=False)  # allow-secret: synthetic test fixture
        with TestClient(disabled) as client:
            assert client.get("/guest/").status_code == 404
    report = network_dashboard.export_health_report(
        conn, tenant_id="tenant-a", actor_role="network_operator", actor_id="network-operator-1", format="html"
    )
    assert "Flagship" in report
    assert "tenant-a" in network_dashboard.export_health_report(
        conn, tenant_id="tenant-a", actor_role="network_operator", actor_id="network-operator-1", format="json"
    )
    registry = providers.ProviderRegistry()
    assert registry.record_verification(conn, tenant_id="tenant-a", show_id="flagship", capability="calendar")


def test_onboarding_generates_editable_workspace(tmp_path: Path):
    result = onboarding.init_workspace(tmp_path / "new-show", {"show_name": "Test Show", "host_names": "Ari, Anthony", "recording_cities": "LA, NYC", "show_format": "conversation", "primary_format": "both", "partnership_type": "co-host", "notification_email_ref": "credential://mail/operator", "github_repo_name": "test-show"})
    assert result["show_id"] == "test-show"
    assert (tmp_path / "new-show" / "data" / "pipeline.csv").is_file()


def test_named_adapters_nurture_and_cli_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert integration_adapters.ManualCalendarAdapter().free_busy([{"start": "a"}])[0]["start"] == "a"
    assert integration_adapters.ICSCalendarAdapter().name == "ics"
    assert integration_adapters.ManualTourAdapter().events([{"city": "LA"}])[0]["city"] == "LA"
    assert integration_adapters.ApprovedRssTourAdapter().name == "approved_rss"
    assert integration_adapters.RSSDistributionAdapter().prepare({"title": "x"})["mode"] == "draft"
    assert integration_adapters.ManualExternalReceiptAdapter().name == "manual_external_receipt"
    assert integration_adapters.YouTubeMetadataAdapter().name == "youtube_metadata"
    with pytest.raises(providers.ProviderError):
        integration_adapters.GoogleCalendarAdapter().free_busy([])
    with pytest.raises(providers.ProviderError):
        integration_adapters.SongkickTourAdapter().events([])
    assert nurture.next_due(relationship_class="C4", last_contact=datetime.now(timezone.utc)) is None
    conn = _conn(tmp_path)
    platform.register_show(conn, tenant_id="tenant-a", show_id="flagship", label="Flagship", config_ref="config://flagship")
    due = nurture.due_for_guest(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", relationship_class="C2", last_contact=datetime(2020, 1, 1, tzinfo=timezone.utc), now=datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert due["due"] is True
    platform.set_do_not_contact(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", notes_ref="vault://dnc/2")
    assert nurture.due_for_guest(conn, tenant_id="tenant-a", show_id="flagship", guest_id="theo-von", relationship_class="C2", last_contact=datetime(2020, 1, 1, tzinfo=timezone.utc), now=datetime(2026, 8, 10, tzinfo=timezone.utc))["due"] is False
    assert main(["capabilities"]) == 0
    assert main(["config", "validate"]) == 0
    assert main(["provider", "verify", "calendar"]) == 0
    captured = capsys.readouterr().out
    assert "calendar" in captured


@pytest.mark.skipif(TestClient is None, reason="api extra is not installed")
def test_1_0_api_routes_use_the_same_auth_and_scope_boundary(tmp_path: Path):
    database = tmp_path / "api.sqlite3"
    conn = store.connect(database)
    platform.register_show(conn, tenant_id="tenant-a", show_id="flagship", label="Flagship", config_ref="config://flagship")
    platform.register_show(conn, tenant_id="tenant-a", show_id="field", label="Field", config_ref="config://field")
    conn.close()
    app = create_app(  # allow-secret: synthetic test fixture
        db_path=str(database),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {
                "api-token-that-is-long-enough": (
                    "operator",
                    "producer",
                    "tenant-a",
                )
            }
        ),
        csrf_required=False,
    )
    headers = {"Authorization": "Bearer api-token-that-is-long-enough", "X-Hospes-Actor": "operator", "X-Hospes-Role": "producer", "X-Hospes-Tenant": "tenant-a"}  # allow-secret: synthetic test fixture
    with TestClient(app) as client:
        assert client.get("/v1/capabilities", headers=headers).status_code == 200
        assert {show["show_id"] for show in client.get("/v1/shows", headers=headers).json()} == {"field", "flagship"}
        touchpoint = client.post("/v1/shows/flagship/touchpoints", headers=headers, json={"guest_id": "theo-von", "channel": "text", "notes": "Synthetic note", "occurred_at": "2026-08-10T00:00:00+00:00"})
        assert touchpoint.status_code == 503
        assert touchpoint.json()["detail"] == "touchpoint note custody is not configured"
        assert client.get("/v1/shows/flagship/network-map?guest=ari", headers=headers).status_code == 200
        assert client.get("/v1/shows/flagship/notifications", headers=headers).status_code == 200
        assert client.post("/v1/shows/flagship/clearances", headers=headers, json={"episode_id": "ep-1", "type": "music", "rights_holder": "vault://rights/1", "license_terms": "vault://terms/1"}).status_code == 201
        draft = client.post("/v1/shows/flagship/distributions", headers=headers, json={"episode_id": "ep-1", "platform": "rss", "metadata": {"title": "Fixture"}, "idempotency_key": "ep-1:rss"})
        assert draft.status_code == 201
        assert client.post(f"/v1/shows/flagship/distributions/{draft.json()['id']}/authorize", headers=headers, json={"authorization_ref": "receipt://auth/flagship", "idempotency_key": "auth-flagship"}).status_code == 403
        field_draft = client.post("/v1/shows/field/distributions", headers=headers, json={"episode_id": "ep-2", "platform": "rss", "metadata": {"title": "Fixture"}, "idempotency_key": "ep-2:rss"})
        assert field_draft.status_code == 201
        # A producer may draft a package but not authorize its publication: #28
        # restricts that gate to an editorial or relationship owner, so the role is
        # refused before the payload is read. The malformed-payload 422 for an
        # authorized role is covered in the #28 predicate.
        assert client.post(f"/v1/shows/field/distributions/{field_draft.json()['id']}/authorize", headers=headers, json={}).status_code == 403
        research = client.post("/v1/shows/flagship/research", headers=headers, json={"guest_id": "theo-von"})
        assert research.status_code == 201
        job_id = research.json()["id"]
        assert client.post(f"/v1/shows/flagship/research/{job_id}/complete", headers=headers, json={"brief": {}, "citations": [{"title": "Public", "url": "https://en.wikipedia.org/wiki/Podcast"}], "counterarguments": ["The public source is a single secondary reference."], "cost_minor": 1}).status_code == 200
        assert client.post(f"/v1/shows/flagship/research/{job_id}/review", headers=headers, json={"review_ref": "receipt://review/api", "approved": "false"}).status_code == 422
        assert client.post(f"/v1/shows/flagship/research/{job_id}/review", headers=headers, json={"review_ref": "receipt://review/api", "approved": True}).status_code == 200
        assert client.post(f"/v1/shows/flagship/research/{job_id}/lock", headers=headers, json={"lock_ref": "receipt://lock/api"}).status_code == 200
