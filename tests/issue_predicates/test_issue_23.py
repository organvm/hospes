"""Issue #23 predicate: multi-show / multi-tenant dashboard.

Close condition: URL and session show state drive isolated reloads and
per-show DNA, voice, templates, and branding with negative cross-show
authorization proof.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conftest import synthetic_bearer_authenticator
from hospes import configuration, drafts, migrations, partnerships, platform, store
from hospes.api import create_app


try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]


TENANT = "hospes"
SHOW_A = "flagship"
SHOW_B = "field"
PRODUCER_TOKEN = "issue23-producer-token-0123456789abcdef"  # allow-secret: fixture
TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "partnerships" / "example-partnership-private-pilot.yaml"

pytestmark = pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")


class _SessionShowASGI:
    """Simulate the session layer: project the operator's active show into scope."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-session-show":
                    scope["hospes.operator_show"] = value.decode("utf-8")
        await self.app(scope, receive, send)


class _ShowBoundAuthenticator:
    """Bind a synthetic bearer identity to one show, mirroring identity_mappings."""

    def __init__(self, inner, show_id: str) -> None:
        self.inner = inner
        self.show_id = show_id

    def authenticate(self, presented_bearer):
        identity = self.inner.authenticate(presented_bearer)
        return dataclasses.replace(identity, show_id=self.show_id)


def _authenticator():
    return synthetic_bearer_authenticator({PRODUCER_TOKEN: ("producer_fixture", "producer", TENANT)})


HEADERS = {"Authorization": f"Bearer {PRODUCER_TOKEN}"}


def _headers(session_show: str | None = None) -> dict[str, str]:
    headers = dict(HEADERS)
    if session_show is not None:
        headers["X-Session-Show"] = session_show
    return headers


def _seed(path: Path) -> None:
    conn = store.connect(path)
    for show_id, label in ((SHOW_A, "Flagship Show"), (SHOW_B, "Field Show")):
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
        )
    conn.commit()
    conn.close()


def _client(path: Path, *, bound_show: str | None = None) -> TestClient:
    authenticator = _authenticator()
    if bound_show is not None:
        authenticator = _ShowBoundAuthenticator(authenticator, bound_show)
    app = create_app(
        str(path),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=authenticator,
        csrf_required=False,
    )
    return TestClient(_SessionShowASGI(app))


def _opportunity_payload(show_id: str, guest: str) -> dict:
    return {
        "tenant_id": TENANT,
        "network_id": "issue23_network",
        "show_id": show_id,
        "guest_name": guest,
        "why_guest": "The synthetic guest can stress-test a concrete operating claim.",
        "why_now": "The synthetic project has reached a useful public decision point.",
        "proposed_artifact": "A one-page decision framework",
        "relationship_class": "C2",
        "relationship_owner": "ari_owner",
        "social_cost_1_5": 2,
        "ari_effort": "review_only",
        "preferred_city": "Los Angeles",
        "next_action": "Review the synthetic candidate.",
        "source_provenance": "Issue 23 predicate fixture provenance.",
    }


def test_show_switcher_and_session_show_state_drive_isolated_reloads(tmp_path: Path) -> None:
    database = tmp_path / "issue23.sqlite3"
    _seed(database)
    with _client(database) as client:
        for show_id, guest in ((SHOW_A, "Guest Alpha"), (SHOW_B, "Guest Beta")):
            created = client.post(
                "/v1/opportunities",
                json=_opportunity_payload(show_id, guest),
                headers=_headers(show_id),
            )
            assert created.status_code == 201, created.text

        # The switcher lists registered shows with per-show configured profiles.
        switcher = client.get("/v1/shows", headers=_headers()).json()
        by_show = {row["show_id"]: row for row in switcher}
        assert set(by_show) == {SHOW_A, SHOW_B}
        assert not any(row["active"] for row in switcher)
        for show_id in (SHOW_A, SHOW_B):
            profile = by_show[show_id]["profile"]
            assert profile["show_id"] == show_id
            assert profile["dna_ref"] == f"dna/{show_id}.show.yaml"
            assert profile["voice_ref"] == f"config/voices/{show_id}.yaml"
            assert profile["template_ref"] == f"config/templates/{show_id}.yaml"
            assert profile["brand_ref"] == f"config/brands/{show_id}.yaml"
            assert profile["pilot_policy_ref"] == f"config/pilot_policies/{show_id}.yaml"

        # Session show state marks the active show and isolates every reload.
        session_a = client.get("/v1/shows", headers=_headers(SHOW_A)).json()
        assert {row["show_id"]: row["active"] for row in session_a} == {
            SHOW_A: True,
            SHOW_B: False,
        }
        rows_a = client.get("/v1/opportunities", headers=_headers(SHOW_A)).json()
        rows_b = client.get("/v1/opportunities", headers=_headers(SHOW_B)).json()
        assert {row["show_id"] for row in rows_a} == {SHOW_A}
        assert {row["show_id"] for row in rows_b} == {SHOW_B}
        assert {row["guest_name"] for row in rows_a} == {"Guest Alpha"}
        assert {row["guest_name"] for row in rows_b} == {"Guest Beta"}

        # Mutations aimed at another show than the session's are rejected.
        cross_create = client.post(
            "/v1/opportunities",
            json=_opportunity_payload(SHOW_B, "Guest Smuggled"),
            headers=_headers(SHOW_A),
        )
        assert cross_create.status_code == 403


def test_url_show_state_and_show_bound_identity_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "issue23-url.sqlite3"
    _seed(database)
    with _client(database) as client:
        # URL show state: a session scoped to show A reads A but never B.
        own = client.get(f"/v1/shows/{SHOW_A}/suggestions", headers=_headers(SHOW_A))
        assert own.status_code == 200
        cross = client.get(f"/v1/shows/{SHOW_B}/suggestions", headers=_headers(SHOW_A))
        assert cross.status_code == 403
        assert "show scope" in cross.json()["detail"]

    with _client(database, bound_show=SHOW_A) as bound:
        # A show-bound identity only sees its own show in the switcher.
        switcher = bound.get("/v1/shows", headers=_headers()).json()
        assert [row["show_id"] for row in switcher] == [SHOW_A]

        # It cannot reach another show's URL scope...
        cross = bound.get(f"/v1/shows/{SHOW_B}/suggestions", headers=_headers())
        assert cross.status_code == 403

        # ...nor claim another show as its session state.
        hijack = bound.get("/v1/opportunities", headers=_headers(SHOW_B))
        assert hijack.status_code == 403


def test_partnerships_and_pilot_scope_are_show_isolated(tmp_path: Path) -> None:
    database = tmp_path / "issue23-partnership.sqlite3"
    _seed(database)
    conn = store.connect(database)
    imported_a = partnerships.import_template(
        conn,
        TEMPLATE,
        tenant_id=TENANT,
        actor_id="producer_fixture",
        actor_role="producer",
        show_id=SHOW_A,
    )
    imported_b = partnerships.import_template(
        conn,
        TEMPLATE,
        tenant_id=TENANT,
        actor_id="producer_fixture",
        actor_role="producer",
        show_id=SHOW_B,
    )
    # One template imported under two shows yields two independent partnerships.
    assert imported_a.partnership_id != imported_b.partnership_id
    only_a = partnerships.list_partnerships(conn, TENANT, SHOW_A)
    only_b = partnerships.list_partnerships(conn, TENANT, SHOW_B)
    assert [row["show_id"] for row in only_a] == [SHOW_A]
    assert [row["show_id"] for row in only_b] == [SHOW_B]
    conn.commit()
    conn.close()

    with _client(database) as client:
        # Show B's partnership resolves inside its own session scope...
        inside = client.get(
            f"/v1/partnerships/{imported_b.partnership_id}/command-center",
            headers=_headers(SHOW_B),
        )
        assert inside.status_code == 200

        # ...and is unreachable from a session scoped to show A.
        outside = client.get(
            f"/v1/partnerships/{imported_b.partnership_id}/command-center",
            headers=_headers(SHOW_A),
        )
        assert outside.status_code == 403

        # Invalid payloads are rejected before any cross-show lookup can leak state.
        invalid = client.post(
            f"/v1/partnerships/{imported_b.partnership_id}/items",
            json={},
            headers=_headers(SHOW_A),
        )
        assert invalid.status_code == 422

        # A valid mutation aimed across shows fails closed on the lookup.
        valid_item = {
            "item_key": "issue23.cross",
            "category": "plan",
            "title": "Cross-show mutation",
            "summary": "Cross-show mutation must fail closed.",
            "owner": "producer_fixture",
            "state": "planned",
        }
        cross_mutation = client.post(
            f"/v1/partnerships/{imported_b.partnership_id}/items",
            json=valid_item,
            headers=_headers(SHOW_A),
        )
        assert cross_mutation.status_code == 403


def test_per_show_resources_resolve_with_traversal_negatives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shows = {show.show_id: show for show in configuration.list_show_configs(TENANT)}
    assert {SHOW_A, SHOW_B, "client-x"} <= set(shows)

    # Every enabled show declares its own DNA, voice, template, brand, and policy.
    for field_name in ("dna_ref", "voice_ref", "template_ref", "brand_ref", "pilot_policy_ref"):
        values = [getattr(show, field_name) for show in shows.values()]
        assert all(values), f"{field_name} must be declared for every enabled show"
        assert len(values) == len(set(values)), f"{field_name} must not be shared across shows"

    # Profiles resolve each show's own DNA identity.
    for show_id in (SHOW_A, SHOW_B, "client-x"):
        profile = configuration.show_profile(shows[show_id])
        assert profile["show_id"] == show_id
        assert profile["dna_title"]
        assert profile["primary_format"] != "unconfigured"

    foreign_voice = dataclasses.replace(shows[SHOW_B], voice_ref="config/voices/flagship.yaml")
    with pytest.raises(configuration.ConfigurationError, match="owning show_id"):
        configuration.show_profile(foreign_voice)

    # The resolver refuses traversal, absolute paths, foreign suffixes, and unknown kinds.
    for reference, kind in (
        ("dna/../secrets.yaml", "dna"),
        ("/etc/passwd.yaml", "dna"),
        ("dna/flagship.show.txt", "dna"),
        ("config/voices/flagship.yaml", "unknown"),
        ("config/templates/../voices/flagship.yaml", "template"),
    ):
        with pytest.raises(configuration.ConfigurationError):
            configuration.show_resource_path(reference, kind)

    # validate_configuration accepts the shipped multi-show estate.
    assert configuration.validate_configuration() == []

    resource_root = tmp_path / "voices"
    resource_root.mkdir()
    target = tmp_path / "outside.yaml"
    target.write_text("version: 1\nshow_id: field\n", encoding="utf-8")
    (resource_root / "field.yaml").symlink_to(target)
    monkeypatch.setitem(configuration._SHOW_RESOURCE_ROOTS, "voice", resource_root)
    with pytest.raises(configuration.ConfigurationError, match="symbolic link"):
        configuration.show_resource_path("config/voices/field.yaml", "voice")


def test_drafts_render_from_the_opportunity_show_resources() -> None:
    candidate = {
        "guest_name": "Synthetic Guest",
        "episode_thesis": "A testable editorial question",
        "why_guest": "a verified project",
        "why_now": "a verified reason",
        "relationship_class": "C2",
    }
    flagship_key, flagship = drafts.render_outreach(candidate, show_id=SHOW_A)
    field_key, field = drafts.render_outreach(candidate, show_id=SHOW_B)
    client_key, client = drafts.render_outreach(candidate, show_id="client-x")
    assert (flagship_key, field_key, client_key) == (
        "prior_collaborator",
        "owner_field",
        "client_review",
    )
    assert len({flagship, field, client}) == 3
    assert "prior conversation" in flagship
    assert "field format" in field
    assert "client language" in client


def test_migration_receipt_and_documentation_surface(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue23-schema.sqlite3")
    ledger = {row["version"]: row["name"] for row in migrations.applied_migrations(conn)}
    assert ledger[14] == "multi_show_partnership_and_pilot_scope"
    assert migrations.current_version(conn) == migrations.LATEST_VERSION
    for table in ("partnerships", "partnership_items", "pilot_runs"):
        columns = {row["name"] for row in store.fetch_all(conn, f"PRAGMA table_info({table})")}
        assert "show_id" in columns, f"{table} must carry show scope"
    conn.close()

    root = Path(__file__).resolve().parents[2]
    documentation = (root / "docs" / "multi-show.md").read_text(encoding="utf-8")
    for marker in (
        "hospes.operator_show",
        "/v1/shows",
        "403",
        "dna_ref",
        "show_resource_path",
        "isolated reload",
    ):
        assert marker in documentation
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "docs/multi-show.md" in readme
