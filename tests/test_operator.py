"""Focused tests for the localhost-only operator/dashboard boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="operator requires the optional 'api' extra")
pytest.importorskip("uvicorn", reason="operator requires the optional 'api' extra")
from fastapi.testclient import TestClient  # noqa: E402

from hospes import operator, partnerships, platform, service, store  # noqa: E402
from hospes.__main__ import build_parser, main  # noqa: E402

TOKEN = "synthetic-operator-token-12345"
SESSION_SECRET = "synthetic-session-secret-12345"


def operator_app(database: Path) -> Any:
    return operator.create_operator_app(
        db_path=str(database),
        auth_token=TOKEN,  # allow-secret: synthetic test fixture
        actor_id="ari_fixture",
        role="relationship_owner",
        tenant_id="hospes",
        session_secret=SESSION_SECRET,
    )


def opportunity_payload() -> dict[str, str]:
    return {
        "tenant_id": "hospes",
        "network_id": "example_network",
        "show_id": "flagship",
        "guest_name": "Synthetic Operator Guest",
        "why_guest": "The synthetic guest demonstrates the live operator boundary.",
        "why_now": "The private pilot needs a reload-safe approval queue now.",
        "proposed_artifact": "A compact operator decision card",
        "relationship_class": "C2",
    }


def test_dashboard_login_uses_httponly_session_and_api_proxy(tmp_path: Path) -> None:
    database = tmp_path / "operator.sqlite3"
    template = Path(__file__).resolve().parents[1] / "config" / "partnerships" / "example-partnership-private-pilot.yaml"
    connection = store.connect(database)
    platform.register_show(
        connection,
        tenant_id="hospes",
        show_id="flagship",
        label="Flagship",
        config_ref="config/shows/flagship.yaml",
    )
    imported = partnerships.import_template(
        connection,
        template,
        tenant_id="hospes",
        actor_id="producer_fixture",
        actor_role="producer",
        show_id="flagship",
    )
    connection.commit()
    connection.close()
    with TestClient(operator_app(database)) as client:
        locked = client.get("/operator/", follow_redirects=False)
        assert locked.status_code == 303
        assert locked.headers["location"] == "/operator/login"

        unauthenticated_api = client.get("/operator/api/opportunities")
        assert unauthenticated_api.status_code == 401

        rejected = client.post(
            "/operator/session",
            data={"token": "incorrect-token-value"},
            follow_redirects=False,
        )
        assert rejected.status_code == 401

        unlocked = client.post("/operator/session", data={"token": TOKEN}, follow_redirects=False)
        assert unlocked.status_code == 303
        cookie = unlocked.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert TOKEN not in cookie
        csrf_headers = {
            "X-Hospes-CSRF": client.cookies.get("hospes_csrf"),
            "X-Session-Show": "flagship",
        }

        dashboard = client.get("/operator/")
        assert dashboard.status_code == 200
        assert "Live SQLite mode" in dashboard.text
        assert "never sends or delivers" in dashboard.text
        assert "Partnership Cockpit" in dashboard.text
        assert "Executive Overview" in dashboard.text
        assert "Pilot Workbench" in dashboard.text
        assert "Complete Register" in dashboard.text
        assert "deferred-workspace" in dashboard.text
        assert "workbench-opportunity" not in dashboard.text
        assert 'src="./assets/partnership-shell.js"' in dashboard.text
        assert 'src="./assets/partnership.js"' not in dashboard.text
        assert "__HOSPES_" not in dashboard.text
        assert 'value="' + client.cookies.get("hospes_csrf") + '"' in dashboard.text
        workspace_fragment = client.get("/operator/assets/partnership-workspace.html")
        assert workspace_fragment.status_code == 200
        assert "Complete Partnership Register" in workspace_fragment.text
        assert "workbench-opportunity" in workspace_fragment.text

        forged = {
            "Authorization": f"Bearer {TOKEN}",
            "X-Hospes-Actor": "forged_actor",
            "X-Hospes-Role": "producer",
            "X-Hospes-Tenant": "forged_tenant",
            "X-Session-Show": "flagship",
        }
        assert client.get("/v1/operator-context", headers=forged).status_code == 404
        bound = client.get("/operator/api/operator-context", headers=forged)
        assert bound.status_code == 200
        assert bound.json()["actor_id"] == "ari_fixture"
        assert bound.json()["role"] == "relationship_owner"
        assert bound.json()["tenant_id"] == "hospes"
        assert bound.json()["raw_v1_enabled"] is False
        assert bound.json()["reply_classifications"] == sorted(service.REPLY_CLASSIFICATIONS)

        partnership_list = client.get("/operator/api/partnerships", headers=csrf_headers)
        assert partnership_list.status_code == 200
        assert partnership_list.json()[0]["label"] == "Host + Producer"
        center = client.get(
            f"/operator/api/partnerships/{imported.partnership_id}/command-center",
            headers=csrf_headers,
        )
        assert center.status_code == 200
        assert center.json()["summary"]["total"] == 22
        overview = client.get(
            f"/operator/api/partnerships/{imported.partnership_id}/command-center?surface=overview",
            headers=csrf_headers,
        )
        assert overview.status_code == 200
        assert set(overview.json()) == {
            "partnership",
            "summary",
            "coverage",
            "engine_capabilities",
            "agenda",
            "recent_events",
        }
        assert len(overview.json()["recent_events"]) <= 12
        unsupported_surface = client.get(
            f"/operator/api/partnerships/{imported.partnership_id}/command-center?surface=private",
            headers=csrf_headers,
        )
        assert unsupported_surface.status_code == 422
        captured = client.post(
            f"/operator/api/partnerships/{imported.partnership_id}/items",
            headers=csrf_headers,
            json={
                "item_key": "unknown.operator_fixture",
                "category": "unknown",
                "title": "<Operator fixture unknown>",
                "summary": "A forgotten item captured through the live operator boundary.",
                "owner": "Partners",
                "state": "unknown",
            },
        )
        assert captured.status_code == 201, captured.text
        refreshed_dashboard = client.get("/operator/")
        assert "&lt;Operator fixture unknown&gt;" in refreshed_dashboard.text
        assert "<Operator fixture unknown>" not in refreshed_dashboard.text

        created = client.post(
            "/operator/api/opportunities",
            headers=csrf_headers,
            json=opportunity_payload(),
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == "hospes"
        opportunity_id = created.json()["id"]

        noted = client.post(
            f"/operator/api/opportunities/{opportunity_id}/decisions",
            headers=csrf_headers,
            json={
                "action": "note",
                "note": "Recorded through the live dashboard proxy.",
            },
        )
        assert noted.status_code == 200, noted.text
        assert noted.json()["decisions"][-1]["note"] == ("Recorded through the live dashboard proxy.")

        listed = client.get("/operator/api/opportunities", headers=csrf_headers)
        assert listed.status_code == 200
        assert [item["guest_name"] for item in listed.json()] == ["Synthetic Operator Guest"]

    # A new app over the same database observes the decision; browser storage
    # was never the authority.
    with TestClient(operator_app(database)) as restarted:
        unlocked = restarted.post("/operator/session", data={"token": TOKEN}, follow_redirects=False)
        assert unlocked.status_code == 303
        assert restarted.get("/operator/").status_code == 200
        persisted = restarted.get(
            f"/operator/api/opportunities/{opportunity_id}",
            headers={"X-Session-Show": "flagship"},
        )
        assert persisted.status_code == 200
        assert persisted.json()["decisions"][-1]["note"] == ("Recorded through the live dashboard proxy.")
        center = restarted.get(
            f"/operator/api/partnerships/{imported.partnership_id}/command-center",
            headers={"X-Session-Show": "flagship"},
        )
        assert center.status_code == 200
        assert center.json()["summary"]["total"] == 23


def test_dashboard_rejects_an_empty_active_show_registry(tmp_path: Path) -> None:
    database = tmp_path / "empty-operator.sqlite3"
    store.connect(database).close()
    with TestClient(operator_app(database)) as client:
        unlocked = client.post("/operator/session", data={"token": TOKEN}, follow_redirects=False)
        assert unlocked.status_code == 303
        dashboard = client.get("/operator/")
        assert dashboard.status_code == 403
        assert client.get("/operator/api/partnerships").status_code == 403


def test_dashboard_show_switcher_scopes_url_render_and_proxy_requests(tmp_path: Path) -> None:
    database = tmp_path / "multi-show-operator.sqlite3"
    template = Path(__file__).resolve().parents[1] / "config" / "partnerships" / "example-partnership-private-pilot.yaml"
    connection = store.connect(database)
    for show_id, label in (("flagship", "Flagship"), ("field", "Field")):
        platform.register_show(
            connection,
            tenant_id="hospes",
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
        )
    flagship = partnerships.import_template(
        connection,
        template,
        tenant_id="hospes",
        actor_id="producer_fixture",
        actor_role="producer",
        show_id="flagship",
    )
    field = partnerships.import_template(
        connection,
        template,
        tenant_id="hospes",
        actor_id="producer_fixture",
        actor_role="producer",
        show_id="field",
    )
    connection.commit()
    connection.close()
    app = operator.create_operator_app(
        db_path=str(database),
        auth_token=TOKEN,  # allow-secret: synthetic test fixture
        actor_id="ari_fixture",
        role="relationship_owner",
        tenant_id="hospes",
        session_secret=SESSION_SECRET,
    )
    with TestClient(app) as client:
        client.post("/operator/session", data={"token": TOKEN}, follow_redirects=False)
        canonical = client.get("/operator/", follow_redirects=False)
        assert canonical.status_code == 303
        assert canonical.headers["location"] == "/operator/?show=flagship"
        field_dashboard = client.get("/operator/?show=field")
        assert field_dashboard.status_code == 200
        assert "hospes_operator_show=" in field_dashboard.headers["set-cookie"]
        assert "HttpOnly" in field_dashboard.headers["set-cookie"]
        assert '<meta name="hospes-active-show" content="field">' in field_dashboard.text
        assert "HOSPES Field" in field_dashboard.text
        assert "--brand-primary:#6f7f5b" in field_dashboard.text
        assert 'id="show-select"' in field_dashboard.text
        assert 'value="field" selected' in field_dashboard.text
        assert 'value="flagship"' in field_dashboard.text
        assert field.partnership_id in field_dashboard.text
        assert flagship.partnership_id not in field_dashboard.text

        scoped = client.get(
            "/operator/api/partnerships",
            headers={"X-Session-Show": "field"},
        )
        assert scoped.status_code == 200
        assert [row["id"] for row in scoped.json()] == [field.partnership_id]
        context = client.get(
            "/operator/api/operator-context",
            headers={"X-Session-Show": "field"},
        )
        assert context.json()["show_id"] == "field"

        missing_scope = client.get("/operator/api/partnerships")
        assert missing_scope.status_code == 403

        client.cookies.set("hospes_operator_show", "field.invalid")
        tampered_scope = client.get(
            "/operator/api/partnerships",
            headers={"X-Session-Show": "field"},
        )
        assert tampered_scope.status_code == 403
        client.get("/operator/?show=field")

        mismatch = client.get(
            "/operator/api/partnerships?show=field",
            headers={"X-Session-Show": "flagship"},
        )
        assert mismatch.status_code == 403
        assert client.get("/operator/?show=not-a-show").status_code == 403
        app.state.conn.execute(
            "UPDATE show_registry SET status = 'retired' "
            "WHERE tenant_id = 'hospes' AND show_id = 'field'"
        )
        app.state.conn.commit()
        assert client.get("/operator/?show=field").status_code == 403


def test_dashboard_modules_have_live_api_and_no_browser_authority() -> None:
    dashboard = Path(__file__).resolve().parents[1] / "dashboard"
    index_shell = (dashboard / "index.html").read_text(encoding="utf-8")
    workspace_html = (dashboard / "assets" / "partnership-workspace.html").read_text(encoding="utf-8")
    index = "\n".join((index_shell, workspace_html))
    app_js = (dashboard / "assets" / "app.js").read_text(encoding="utf-8")
    api_js = (dashboard / "assets" / "api.js").read_text(encoding="utf-8")
    shell_js = (dashboard / "assets" / "partnership-shell.js").read_text(encoding="utf-8")
    demo_js = (dashboard / "assets" / "demo.js").read_text(encoding="utf-8")
    partnership_js = (dashboard / "assets" / "partnership.js").read_text(encoding="utf-8")
    partnership_workspace_js = (dashboard / "assets" / "partnership-workspace.js").read_text(encoding="utf-8")
    decisions_mjs = (dashboard / "assets" / "decisions.mjs").read_text(encoding="utf-8")
    combined = "\n".join(
        (
            index,
            app_js,
            api_js,
            demo_js,
            shell_js,
            partnership_js,
            partnership_workspace_js,
            decisions_mjs,
        )
    )

    assert "./assets/styles.css" in index
    assert "./assets/partnership-shell.js" in index_shell
    assert "deferred-workspace" in index_shell
    assert "workbench-opportunity" not in index_shell
    assert "workbench-opportunity" in workspace_html
    assert "partnership-workspace.html" in partnership_js
    assert "import('./partnership.js')" in shell_js
    assert "import('./partnership-workspace.js')" in partnership_js
    assert "loadOpportunities" not in partnership_js
    assert "import('./app.js')" in partnership_workspace_js
    assert "hydrateApprovalQueue" in app_js
    # organvm/hospes#59: the approval queue and the workbench project one
    # candidate lifecycle through two modules. The queue announces a committed
    # decision on the shared channel and the workbench re-reads live truth, so
    # an approved candidate stops reading EDITORIAL_REVIEW until a human
    # presses "Refresh live truth".
    assert "hospes:decision-committed" in decisions_mjs
    assert "publishDecisionCommitted" in app_js
    assert "subscribeDecisionCommitted" in partnership_workspace_js
    assert "resolveSelectedOpportunity" in partnership_workspace_js
    assert "/operator/api" in api_js
    assert "/approval-queue" in api_js
    assert "/decisions" in api_js
    assert "/partnerships" in api_js
    assert "loadOpportunityDetail" in api_js
    assert "reply_classifications" in partnership_workspace_js
    assert "newestCompletePrimaryPackage" in partnership_workspace_js
    assert "previousPartnershipId" in partnership_workspace_js
    assert "btn-review-draft" in index
    assert "receipt-submit" in index
    assert "0/14" in index
    assert "What might we be forgetting?" in partnership_workspace_js
    assert "Presentation mode" in index
    assert "technical rehearsal" in index
    assert "no booking, signing, or auto-publishing routes" in index
    assert "a publication is a recorded human receipt" in index
    assert "contracts, private" in index
    assert "Demo CSV fallback — synthetic mode" in workspace_html
    assert "localStorage" not in combined
    assert "sessionStorage" not in combined
    assert "Authorization" not in combined


def test_demo_recorder_does_not_press_the_stale_selector_workaround() -> None:
    # organvm/hospes#59: the roster beat pressed "Refresh live truth" after
    # approving a candidate because the workbench selector did not repopulate
    # itself. The selector now reconciles on the committed decision, so the
    # recorder films the product instead of a workaround.
    recorder = (Path(__file__).resolve().parents[1] / "scripts" / "record-demo.mjs").read_text(encoding="utf-8")
    assert "btn-partnership-refresh" not in recorder


def test_synthetic_mode_uses_one_persistent_chip() -> None:
    root = Path(__file__).resolve().parents[1]
    index = (root / "dashboard" / "index.html").read_text(encoding="utf-8")
    workspace = (root / "dashboard" / "assets" / "partnership-workspace.html").read_text(encoding="utf-8")
    styles = (root / "dashboard" / "assets" / "styles.css").read_text(encoding="utf-8")
    assert 'id="mode-badge"' in index
    assert 'data-guide="synthetic-marker"' in index
    assert "data-runtime-warning" not in index
    assert "data-runtime-warning" not in workspace
    assert "--synthetic-warning-height" not in styles


def test_operator_cli_has_no_public_bind_or_token_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = build_parser().parse_args(
        [
            "operator",
            "--actor",
            "ari_fixture",
            "--role",
            "relationship_owner",
            "--tenant",
            "pilot_fixture",
        ]
    )
    assert parsed.port == operator.DEFAULT_PORT
    assert parsed.enable_raw_v1 is False
    assert not hasattr(parsed, "host")
    assert not hasattr(parsed, "token")

    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        captured["app"] = app

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setenv(operator.DEFAULT_TOKEN_ENV, TOKEN)
    result = main(
        [
            "operator",
            "--db",
            ":memory:",
            "--actor",
            "ari_fixture",
            "--role",
            "relationship_owner",
            "--tenant",
            "pilot_fixture",
            "--port",
            "8877",
        ]
    )
    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8877


def test_documented_operator_prompt_is_bash_compatible() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative_path in ("README.md", "docs/example-pilot-runbook.md"):
        content = (root / relative_path).read_text()
        assert 'read -s "HOSPES_OPERATOR_TOKEN?' not in content
        assert 'read -r -s -p "Operator token: " HOSPES_OPERATOR_TOKEN' in content


def test_operator_rejects_missing_or_unsafe_auth_configuration() -> None:
    with pytest.raises(operator.OperatorConfigError, match="at least 16"):
        operator.load_auth_token(env={operator.DEFAULT_TOKEN_ENV: "short"})
    with pytest.raises(operator.OperatorConfigError, match="actor is required"):
        operator.resolve_identity(actor=None, role="host", tenant="pilot_fixture", env={})
    with pytest.raises(operator.OperatorConfigError, match="port"):
        operator.validate_port(0)
    with pytest.raises(operator.OperatorConfigError, match="database is required"):
        operator.serve_operator(
            db_path=None,
            port=8765,
            actor="ari_fixture",
            role="relationship_owner",
            tenant="pilot_fixture",
            env={operator.DEFAULT_TOKEN_ENV: TOKEN},
        )


def test_operator_accepts_hosted_database_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_app(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(operator, "create_operator_app", fake_app)
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)
    hosted = "postgresql://example.test/hospes?sslmode=require"
    operator.serve_operator(
        db_path=None,
        port=8765,
        actor="ari_fixture",
        role="relationship_owner",
        tenant="pilot_fixture",
        env={
            operator.DEFAULT_TOKEN_ENV: TOKEN,
            "HOSPES_DATABASE_URL": hosted,
        },
    )
    assert captured["db_path"] == hosted
