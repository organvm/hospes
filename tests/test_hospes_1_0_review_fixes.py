from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from hospes import (
    analytics,
    configuration,
    distribution,
    guest_portal,
    network_dashboard,
    network_graph,
    onboarding,
    platform,
    privacy,
    providers,
    research_agent,
    store,
)


NOW = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)


def _conn(tmp_path: Path):
    return store.connect(tmp_path / "review.sqlite3")


def _opportunity(conn, *, tenant: str, show: str, guest: str) -> None:
    store.insert(
        conn,
        "appearance_opportunities",
        {
            "id": f"opp-{tenant}-{show}-{guest}",
            "tenant_id": tenant,
            "network_id": "network-a",
            "show_id": show,
            "guest_name": guest,
            "why_guest": "fixture",
            "why_now": "fixture",
            "proposed_artifact": "fixture",
            "relationship_class": "C2",
            "status": "DISCOVERED",
            "source_key": guest,
            "social_cost_1_5": 2,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        },
    )
    conn.commit()


def test_scope_dnc_clearance_and_opaque_reference_guards(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _opportunity(conn, tenant="tenant-a", show="show-a", guest="guest-a")
    platform.record_guest_history(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        guest_id="guest-a",
        season="1",
        episode="1",
        disposition="ASKED",
        notes_ref="vault://history/ordinary",
        now=NOW,
    )
    platform.set_do_not_contact(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        guest_id="guest-a",
        notes_ref="vault://history/dnc",
        now=NOW,
    )
    assert platform.suggest_guests(
        conn, tenant_id="tenant-a", show_id="show-a"
    ) == []

    sponsor = platform.add_sponsor(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        name="Sponsor",
        contact_ref="vault://sponsor/contact",
        terms_ref="vault://sponsor/terms",
        now=NOW,
    )
    with pytest.raises(platform.PlatformError, match="sponsor not found"):
        platform.add_sponsorship(
            conn,
            tenant_id="tenant-b",
            show_id="show-b",
            sponsor_id=sponsor["id"],
            episode_id="episode-b",
            slot_type="mid",
            rate_minor=100,
            now=NOW,
        )

    clearance = platform.add_clearance(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        episode_id="episode-a",
        clearance_type="music",
        rights_holder_ref="vault://rights/one",
        license_terms_ref="vault://terms/one",
        now=NOW,
    )
    platform.update_clearance(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        clearance_id=clearance["id"],
        status="denied",
        now=NOW,
    )
    assert platform.publish_blockers(
        conn, tenant_id="tenant-a", show_id="show-a", episode_id="episode-a"
    )[0]["status"] == "denied"

    with pytest.raises(platform.PlatformError, match="opaque custody"):
        platform.record_contact_route(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            guest_id="guest-a",
            route_type="phone",
            route_ref="555-123-4567",
            provenance_ref="vault://source/one",
            verified_at=NOW.isoformat(),
            now=NOW,
        )
    with pytest.raises(platform.PlatformError, match="future"):
        platform.record_contact_route(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            guest_id="guest-a",
            route_type="publicist",
            route_ref="vault://route/one",
            provenance_ref="vault://source/one",
            verified_at="2026-08-11T00:00:00+00:00",
            now=NOW,
        )


def test_network_config_is_scoped_validated_and_render_safe(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    path = tmp_path / "edges.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "edges": [
                    {
                        "tenant_id": "tenant-a",
                        "show_id": "show-a",
                        "source_guest_id": "guest-a",
                        "target_guest_id": "guest-b",
                        "edge_type": "knows",
                        "relationship_class": "C2",
                        "provenance_ref": "vault://edge/a",
                    },
                    {
                        "tenant_id": "tenant-b",
                        "show_id": "show-b",
                        "source_guest_id": "guest-a",
                        "target_guest_id": "private-b",
                        "edge_type": "knows",
                        "relationship_class": "C1",
                        "provenance_ref": "vault://edge/b",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    value = network_graph.graph(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        root_guest_id="guest-a",
        config_path=path,
    )
    assert {node["id"] for node in value["nodes"]} == {"guest-a", "guest-b"}
    assert "private-b" not in network_graph.render_graph(value, "mermaid")
    with pytest.raises(platform.PlatformError):
        network_graph.render_graph(
            {
                "edges": [
                    {
                        "source_guest_id": "guest-a]-->evil",
                        "target_guest_id": "guest-b",
                        "relationship_class": "C2",
                    }
                ]
            },
            "mermaid",
        )


def test_research_scope_policy_review_identity_and_lock_immutability(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    with pytest.raises(platform.PlatformError, match="policy ceiling"):
        research_agent.start_job(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            actor_role="producer",
            guest_id="guest-a",
            max_cost_minor=501,
            now=NOW,
        )
    job = research_agent.start_job(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        actor_role="producer",
        guest_id="guest-a",
        now=NOW,
    )
    with pytest.raises(platform.PlatformError, match="not found"):
        research_agent.complete_job(
            conn,
            tenant_id="tenant-b",
            show_id="show-a",
            actor_role="producer",
            job_id=job["id"],
            brief={},
            citations=[{"title": "Allowed", "url": "https://en.wikipedia.org/wiki/Test"}],
            counterarguments=[],
            cost_minor=1,
            now=NOW,
        )
    with pytest.raises(platform.PlatformError, match="not allowed"):
        research_agent.complete_job(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            actor_role="producer",
            job_id=job["id"],
            brief={},
            citations=[{"title": "Private", "url": "https://private.example.test/source"}],
            counterarguments=[],
            cost_minor=1,
            now=NOW,
        )
    ready = research_agent.complete_job(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        actor_role="producer",
        job_id=job["id"],
        brief={"claim": "bounded"},
        citations=[{"title": "Allowed", "url": "https://en.wikipedia.org/wiki/Test"}],
        counterarguments=["counter"],
        cost_minor=1,
        now=NOW,
    )
    with pytest.raises(platform.PlatformError, match="boolean"):
        research_agent.review_job(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            actor_role="producer",
            job_id=job["id"],
            reviewer_id="reviewer-a",
            review_ref="receipt://review/a",
            approved="false",  # type: ignore[arg-type]
            now=NOW,
        )
    reviewed = research_agent.review_job(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        actor_role="producer",
        job_id=job["id"],
        reviewer_id="reviewer-a",
        review_ref="receipt://review/a",
        approved=True,
        now=NOW,
    )
    receipt = store.fetch_one(
        conn, "SELECT * FROM provider_receipts WHERE id = ?", (reviewed["review_receipt_ref"],)
    )
    assert receipt and receipt["details"]["reviewer_id"] == "reviewer-a"
    research_agent.lock_job(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        actor_role="producer",
        job_id=job["id"],
        lock_ref="receipt://lock/a",
        now=NOW,
    )
    with pytest.raises(platform.PlatformError, match="researching"):
        research_agent.complete_job(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            actor_role="producer",
            job_id=job["id"],
            brief={"claim": "mutated"},
            citations=[{"title": "Allowed", "url": "https://en.wikipedia.org/wiki/Test"}],
            counterarguments=[],
            cost_minor=1,
            now=NOW,
        )
    locked = store.fetch_one(conn, "SELECT * FROM research_jobs WHERE id = ?", (job["id"],))
    assert locked and locked["brief"] == ready["brief"]


def test_distribution_subject_clearance_retry_audit_and_rss(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    first = distribution.create_draft(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        episode_id="episode-a",
        platform_name="rss",
        metadata={"title": "A"},
        idempotency_key="draft-a",
        now=NOW,
    )
    with pytest.raises(platform.PlatformError, match="outbound mode"):
        distribution.authorize_publish(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            distribution_id=first["id"],
            outbound_mode="draft_only",
            authorized_by="owner-a",
            authorization_ref="receipt://auth/a",
            idempotency_key="auth-shared",
            now=NOW,
        )
    authorized = distribution.authorize_publish(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        distribution_id=first["id"],
        outbound_mode="manual_receipt",
        authorized_by="owner-a",
        authorization_ref="receipt://auth/a",
        idempotency_key="auth-shared",
        now=NOW,
    )
    second = distribution.create_draft(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        episode_id="episode-b",
        platform_name="rss",
        metadata={"title": "B"},
        idempotency_key="draft-b",
        now=NOW,
    )
    with pytest.raises(providers.ProviderError, match="another subject"):
        distribution.authorize_publish(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            distribution_id=second["id"],
            outbound_mode="manual_receipt",
            authorized_by="owner-a",
            authorization_ref="receipt://auth/b",
            idempotency_key="auth-shared",
            now=NOW,
        )
    published = distribution.mark_published(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        distribution_id=first["id"],
        external_id_ref="external://publish/a",
        now=NOW,
    )
    assert published["status"] == "published"
    assert distribution.authorize_publish(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        distribution_id=first["id"],
        outbound_mode="manual_receipt",
        authorized_by="owner-a",
        authorization_ref="receipt://auth/a",
        idempotency_key="auth-shared",
        now=NOW,
    )["status"] == "published"
    delivery = store.fetch_one(
        conn,
        "SELECT * FROM provider_receipts WHERE capability = 'distribution' AND receipt_ref = ?",
        ("external://publish/a",),
    )
    assert delivery and delivery["details"]["authorized_by"] == "owner-a"

    third = distribution.create_draft(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        episode_id="episode-c",
        platform_name="rss",
        metadata={"title": "C"},
        idempotency_key="draft-c",
        now=NOW,
    )
    distribution.authorize_publish(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        distribution_id=third["id"],
        outbound_mode="manual_receipt",
        authorized_by="owner-a",
        authorization_ref="receipt://auth/c",
        idempotency_key="auth-c",
        now=NOW,
    )
    platform.add_clearance(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        episode_id="episode-c",
        clearance_type="clip",
        rights_holder_ref="vault://rights/c",
        license_terms_ref="vault://terms/c",
        now=NOW,
    )
    with pytest.raises(platform.PlatformError, match="rights"):
        distribution.mark_published(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            distribution_id=third["id"],
            external_id_ref="external://publish/c",
            now=NOW,
        )
    ET.fromstring(distribution.rss_preview({"title": "Fixture", "duration": "01:00"}))
    assert authorized["authorization_receipt_ref"]


def test_configuration_statuses_disabled_show_and_credential_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = configuration.load_runtime()
    report = configuration.capability_report(runtime)
    assert {item.status for item in report} <= {
        "disabled",
        "unconfigured",
        "blocked",
        "ready",
        "unavailable",
    }
    assert providers.ProviderRegistry(runtime).choose("calendar").name == "ics"

    runtime_raw = yaml.safe_load((configuration.CONFIG_DIR / "runtime.yaml").read_text())
    runtime_raw["runtime"]["providers"]["calendar"][2]["credential_ref"] = "raw-secret-value"
    bad_runtime = tmp_path / "runtime.yaml"
    bad_runtime.write_text(yaml.safe_dump(runtime_raw), encoding="utf-8")
    with pytest.raises(configuration.ConfigurationError, match="credential-wall"):
        configuration.load_runtime(bad_runtime)

    config_root = tmp_path / "config"
    (config_root / "shows").mkdir(parents=True)
    (config_root / "runtime.yaml").write_bytes(
        (configuration.CONFIG_DIR / "runtime.yaml").read_bytes()
    )
    (config_root / "shows" / "disabled.yaml").write_text(
        yaml.safe_dump(
            {
                "tenant_id": "tenant-a",
                "show_id": "disabled",
                "enabled": False,
                "modes": {"guest_interaction": "operator_packet", "outbound": "draft_only"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(configuration, "CONFIG_DIR", config_root)
    assert configuration.validate_configuration() == []


def test_show_configuration_path_is_confined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config"
    shows_root = config_root / "shows"
    shows_root.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        yaml.safe_dump({"tenant_id": "tenant-a", "show_id": "escaped"}),
        encoding="utf-8",
    )
    (shows_root / "escaped.yaml").symlink_to(outside)
    monkeypatch.setattr(configuration, "CONFIG_DIR", config_root)

    with pytest.raises(configuration.ConfigurationError, match="safe configuration identifier"):
        configuration.load_show("../outside")
    with pytest.raises(configuration.ConfigurationError, match="escapes"):
        configuration.load_show("escaped")


def test_privacy_scan_bounds_untrusted_text() -> None:
    assert privacy.contact_kind("%" * 999) is None
    assert privacy.contact_kind("%" * 1001) == "oversized content"
    assert privacy.contact_kind("owner@example.com") == "email-like content"


def test_onboarding_is_complete_and_non_destructive(tmp_path: Path) -> None:
    answers = {
        "show_name": "Review Show",
        "host_names": "Host A, Host B",
        "recording_cities": "LA, NYC",
        "show_format": "conversation",
        "primary_format": "both",
        "partnership_type": "co-host",
        "notification_email_ref": "credential://hospes/operator-mail",
        "github_repo_name": "review-show",
    }
    root = tmp_path / "workspace"
    onboarding.init_workspace(root, answers)
    for relative in (
        "config/runtime.yaml",
        "config/outreach_templates.yaml",
        "config/voice.yaml",
        "config/research.yaml",
        "data/example-pipeline.csv",
    ):
        assert (root / relative).is_file()
    original = (root / "data" / "pipeline.csv").read_bytes()
    with pytest.raises(ValueError, match="already exist"):
        onboarding.init_workspace(root, answers)
    merged = onboarding.init_workspace(root, answers, merge=True)
    assert merged["preserved"]
    assert (root / "data" / "pipeline.csv").read_bytes() == original


def test_analytics_portal_and_html_output_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ISO 8601"):
        analytics.normalize_row(
            {"episode_id": "one", "period_start": "not-a-date", "period_end": "2026-08-10"},
            provider="spotify_creator_csv",
            source_receipt_ref="receipt://analytics/a",
        )
    with pytest.raises(ValueError, match="cannot be after"):
        analytics.normalize_row(
            {"episode_id": "one", "period_start": "2026-08-11", "period_end": "2026-08-10"},
            provider="spotify_creator_csv",
            source_receipt_ref="receipt://analytics/a",
        )

    conn = _conn(tmp_path)
    platform.register_show(
        conn,
        tenant_id="tenant-a",
        show_id="show-a",
        label="<script>alert(1)</script>",
        config_ref="config://show/a",
        now=NOW,
    )
    report = network_dashboard.export_health_report(
        conn,
        tenant_id="tenant-a",
        actor_role="network_operator",
        actor_id="network-operator-1",
        format="html",
    )
    assert "<script>" not in report
    assert "&lt;script&gt;" in report

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    portal = guest_portal.create_app(
        conn=conn,
        secret="synthetic-portal-secret",  # allow-secret: synthetic test fixture
        enabled=True,
    )  # allow-secret: synthetic test fixture
    with TestClient(portal) as client:
        landing = client.get("/guest/")
        assert landing.status_code == 200
        assert "/guest/app.js" in landing.text
        assert "location.hash" in client.get("/guest/app.js").text
        link = guest_portal.create_portal_token(
            conn,
            tenant_id="tenant-a",
            show_id="show-a",
            guest_id="guest-a",
            secret="synthetic-portal-secret",  # allow-secret: synthetic test fixture
            base_url="https://guest.example",
            enabled=True,
        )  # allow-secret: synthetic test fixture
        token = link["url"].split("#token=", 1)[1]  # allow-secret: synthetic one-time token
        invalid = client.post(
            "/guest/intake",
            json={
                "token": token,
                "consent_ref": "",
                "intake_ref": "vault://intake/a",
                "availability_ref": "vault://availability/a",
            },
        )
        assert invalid.status_code == 422


def test_packaged_resources_match_canonical_assets() -> None:
    root = Path(__file__).resolve().parents[1]
    for source, packaged in (
        ("dashboard/index.html", "hospes/resources/dashboard/index.html"),
        ("dashboard/assets/api.js", "hospes/resources/dashboard/assets/api.js"),
        ("dashboard/assets/app.js", "hospes/resources/dashboard/assets/app.js"),
        (
            "dashboard/assets/capabilities.mjs",
            "hospes/resources/dashboard/assets/capabilities.mjs",
        ),
        (
            "dashboard/assets/partnership.js",
            "hospes/resources/dashboard/assets/partnership.js",
        ),
        (
            "dashboard/assets/partnership-workspace.js",
            "hospes/resources/dashboard/assets/partnership-workspace.js",
        ),
        (
            "dashboard/assets/partnership-workspace.html",
            "hospes/resources/dashboard/assets/partnership-workspace.html",
        ),
        (
            "dashboard/assets/partnership-shell.js",
            "hospes/resources/dashboard/assets/partnership-shell.js",
        ),
        (
            "dashboard/assets/styles.css",
            "hospes/resources/dashboard/assets/styles.css",
        ),
        ("config/domain_kernel.yaml", "hospes/resources/config/domain_kernel.yaml"),
        ("config/runtime.yaml", "hospes/resources/config/runtime.yaml"),
        ("spec/candidate.schema.json", "hospes/resources/spec/candidate.schema.json"),
        (
            "spec/appearance_opportunity.schema.json",
            "hospes/resources/spec/appearance_opportunity.schema.json",
        ),
        (
            "spec/contact_route.schema.json",
            "hospes/resources/spec/contact_route.schema.json",
        ),
        ("spec/states.json", "hospes/resources/spec/states.json"),
        ("briefs/guest-packet.html", "hospes/resources/briefs/guest-packet.html"),
    ):
        assert (root / source).read_bytes() == (root / packaged).read_bytes()
    schema = json.loads((root / "spec/receipt.schema.json").read_text())
    assert {"ig", "hallway"} <= set(schema["properties"]["channel"]["enum"])
