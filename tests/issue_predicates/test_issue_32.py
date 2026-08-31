"""Executable completion predicate for HOSPES issue #32.

Close condition: network-operator-only portfolio metrics, drilldown, escaped
HTML, PDF export, and cross-tenant leakage tests pass.

Required surfaces: api, service, ui, security, pdf, documentation, receipts.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import synthetic_bearer_authenticator
from hospes import generation, migrations, network_dashboard, platform, sponsors, store
from hospes.api import create_app


try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - exercised only without the api extra
    TestClient = None  # type: ignore[assignment]


UTC = timezone.utc
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]

TENANT = "hospes"
RIVAL_TENANT = "rival-network"
FLAGSHIP = "flagship"
FIELD = "field"
RIVAL_SHOW = "rival-show"

NETWORK_ACTOR = "network-operator-32"
NETWORK_TOKEN = "issue32-network-token-0123456789abcdef"  # allow-secret: fixture
PRODUCER_TOKEN = "issue32-producer-token-0123456789abcdef"  # allow-secret: fixture
NETWORK_HEADERS = {"Authorization": f"Bearer {NETWORK_TOKEN}"}
PRODUCER_HEADERS = {"Authorization": f"Bearer {PRODUCER_TOKEN}"}

#: Every role the product declares except the network operator. The portfolio
#: must refuse all of them, including roles that operate a show in the tenant.
NON_NETWORK_ROLES = ("host", "producer", "editorial_owner", "relationship_owner", "editor")

#: A show label carrying markup, so the HTML report's escaping is proven on a
#: value an operator actually controls rather than on a synthetic string.
HOSTILE_LABEL = '<script>alert("network")</script>'

NETWORK_ROUTES = (
    "/v1/network/portfolio",
    f"/v1/network/shows/{FLAGSHIP}",
    "/v1/network/receipts",
    "/v1/network/report.html",
)


# --- fixtures --------------------------------------------------------------


def _candidate(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    guest_id: str,
    guest_name: str,
    status: str,
    days_ago: int,
) -> None:
    """Insert one appearance opportunity at an exact milestone and age.

    The predicate needs candidates parked on specific lifecycle states with
    specific ``updated_at`` ages, which is what makes the milestone counts and
    the trailing booking-velocity window assertable at all.
    """
    moved_at = (NOW - timedelta(days=days_ago)).isoformat()
    store.insert(
        conn,
        "appearance_opportunities",
        {
            "id": generation.new_id("opportunity"),
            "tenant_id": tenant_id,
            "network_id": "fixture_network",
            "show_id": show_id,
            "guest_id": guest_id,
            "source_key": f"issue32:{guest_id}",
            "guest_name": guest_name,
            "why_guest": "A synthetic candidate that gives the portfolio a milestone to count.",
            "why_now": "The issue predicate needs a bounded fixture at this exact state now.",
            "proposed_artifact": "A synthetic portfolio row",
            "relationship_class": "C2",
            "status": status,
            "created_at": (NOW - timedelta(days=days_ago + 30)).isoformat(),
            "updated_at": moved_at,
        },
    )


def _sell(
    conn: store.DatabaseConnection,
    *,
    tenant_id: str,
    show_id: str,
    episode_id: str,
    sponsor_name: str,
    rate_minor: int,
) -> None:
    """Declare one episode's inventory and sell its committed pre-roll."""
    sponsor = sponsors.create_sponsor(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        name=sponsor_name,
        contact_ref=f"vault://sponsor/{show_id}/contact",
        terms_ref=f"vault://sponsor/{show_id}/terms",
        actor_id="producer-32",
        actor_role="producer",
        now=NOW,
    )
    sponsors.declare_slots(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        episode_id=episode_id,
        slots=[
            {"slot_type": "pre", "rate_minor": rate_minor, "committed": True},
            {"slot_type": "mid", "rate_minor": rate_minor // 2},
        ],
        actor_id="producer-32",
        actor_role="producer",
        now=NOW,
    )
    sponsors.assign_slot(
        conn,
        tenant_id=tenant_id,
        show_id=show_id,
        episode_id=episode_id,
        slot_type="pre",
        sponsor_id=sponsor["id"],
        actor_id="producer-32",
        actor_role="producer",
        status="sold",
        now=NOW,
    )


def _seed(path: Path, *, flagship_label: str = "Flagship Show") -> store.DatabaseConnection:
    """Build a two-show network plus a second tenant that must stay invisible."""
    conn = store.connect(path)
    for tenant_id, show_id, label in (
        (TENANT, FLAGSHIP, flagship_label),
        (TENANT, FIELD, "Field Show"),
        (RIVAL_TENANT, RIVAL_SHOW, "Rival Show"),
    ):
        platform.register_show(
            conn,
            tenant_id=tenant_id,
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
            now=NOW,
        )

    # Flagship: one published, one recently booked, one approved, one declined.
    _candidate(
        conn,
        tenant_id=TENANT,
        show_id=FLAGSHIP,
        guest_id="theo-von",
        guest_name="Theo Von",
        status="PUBLISHED",
        days_ago=40,
    )
    _candidate(
        conn,
        tenant_id=TENANT,
        show_id=FLAGSHIP,
        guest_id="chad-kroeger",
        guest_name="Chad Kroeger",
        status="BOOKED",
        days_ago=5,
    )
    _candidate(
        conn,
        tenant_id=TENANT,
        show_id=FLAGSHIP,
        guest_id="solo-flagship",
        guest_name="Solo Flagship Guest",
        status="APPROVED",
        days_ago=3,
    )
    _candidate(
        conn,
        tenant_id=TENANT,
        show_id=FLAGSHIP,
        guest_id="declined-guest",
        guest_name="Declined Guest",
        status="DECLINED",
        days_ago=2,
    )
    # Field: the same Chad Kroeger identity, plus one candidate of its own.
    _candidate(
        conn,
        tenant_id=TENANT,
        show_id=FIELD,
        guest_id="chad-kroeger",
        guest_name="Chad Kroeger",
        status="BOOKED",
        days_ago=10,
    )
    _candidate(
        conn,
        tenant_id=TENANT,
        show_id=FIELD,
        guest_id="field-only",
        guest_name="Field Only Guest",
        status="DISCOVERED",
        days_ago=1,
    )
    # A second tenant with its own show, guest, and money. Nothing below may see it.
    _candidate(
        conn,
        tenant_id=RIVAL_TENANT,
        show_id=RIVAL_SHOW,
        guest_id="rival-guest",
        guest_name="Rival Network Guest",
        status="BOOKED",
        days_ago=4,
    )

    platform.add_clearance(
        conn,
        tenant_id=TENANT,
        show_id=FLAGSHIP,
        episode_id="episode-1",
        clearance_type="music",
        rights_holder_ref="vault://rights/holder",
        license_terms_ref="vault://rights/terms",
        now=NOW,
    )
    _sell(
        conn,
        tenant_id=TENANT,
        show_id=FLAGSHIP,
        episode_id="episode-1",
        sponsor_name="Athletic Greens",
        rate_minor=250_000,
    )
    _sell(conn, tenant_id=TENANT, show_id=FIELD, episode_id="episode-2", sponsor_name="Field Fuel", rate_minor=100_000)
    _sell(
        conn,
        tenant_id=RIVAL_TENANT,
        show_id=RIVAL_SHOW,
        episode_id="episode-9",
        sponsor_name="Rival Money",
        rate_minor=999_000,
    )
    conn.commit()
    return conn


def _client(path: Path) -> TestClient:
    authenticator = synthetic_bearer_authenticator(
        {
            NETWORK_TOKEN: (NETWORK_ACTOR, "network_operator", TENANT),
            PRODUCER_TOKEN: ("producer-32", "producer", TENANT),
        }
    )
    app = create_app(
        str(path),
        runtime_kind="synthetic_test",
        _test_bearer_authenticator=authenticator,
        csrf_required=False,
    )
    return TestClient(app)


def _portfolio(conn: store.DatabaseConnection, tenant_id: str = TENANT) -> dict:
    return network_dashboard.portfolio(conn, tenant_id=tenant_id, actor_role="network_operator", now=NOW)


def _show(portfolio: dict, show_id: str) -> dict:
    return next(row for row in portfolio["shows"] if row["show_id"] == show_id)


# --- service: portfolio aggregation ----------------------------------------


def test_portfolio_aggregates_pipeline_velocity_revenue_and_overlap(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue32-portfolio.sqlite3")
    portfolio = _portfolio(conn)

    assert portfolio["tenant_id"] == TENANT
    assert portfolio["window_days"] == network_dashboard.VELOCITY_WINDOW_DAYS == 28
    assert [row["show_id"] for row in portfolio["shows"]] == [FIELD, FLAGSHIP]

    # Milestones are cumulative: a PUBLISHED candidate was also booked and
    # approved, and a branch state is reported off-ladder rather than folded in.
    flagship = _show(portfolio, FLAGSHIP)
    assert flagship["pipeline"] == {
        "candidates": 4,
        "off_ladder": 1,
        "approved": 3,
        "booked": 2,
        "published": 1,
    }
    field = _show(portfolio, FIELD)
    assert field["pipeline"] == {
        "candidates": 2,
        "off_ladder": 0,
        "approved": 1,
        "booked": 1,
        "published": 0,
    }

    # Velocity counts only bookings inside the trailing window: the flagship's
    # 40-day-old published episode is outside it, its 5-day-old booking is in.
    assert flagship["booking_velocity"] == {
        "window_days": 28,
        "booked_in_window": 1,
        "bookings_per_week": 0.25,
    }
    assert field["booking_velocity"]["booked_in_window"] == 1

    # Revenue comes from the sponsor organ, so the portfolio and the Revenue
    # view answer the same number for the same show.
    assert flagship["revenue"]["revenue_minor"] == 250_000
    assert flagship["revenue"]["slots_sold"] == 1
    assert flagship["revenue"]["slots_total"] == 2
    assert flagship["revenue"]["currency"] == "USD"
    assert (
        flagship["revenue"]["revenue_minor"]
        == sponsors.revenue_report(conn, tenant_id=TENANT, show_id=FLAGSHIP, actor_role="network_operator")["totals"][
            "revenue_minor"
        ]
    )
    assert flagship["summary"]["pending_clearances"] == 1

    assert portfolio["totals"] == {
        "shows": 2,
        "candidates": 6,
        "off_ladder": 1,
        "pending_clearances": 1,
        "currency": "USD",
        "revenue_minor": 350_000,
        "slots_sold": 2,
        "committed_unfilled": 0,
        "blocked_episodes": 0,
        "overlapping_guests": 1,
        "booked_in_window": 2,
        "bookings_per_week": 0.5,
        "approved": 4,
        "booked": 3,
        "published": 1,
    }

    # The overlap matrix names the guest and both shows it appears on.
    assert portfolio["overlap"] == [
        {
            "guest_id": "chad-kroeger",
            "guest_name": "Chad Kroeger",
            "show_ids": [FIELD, FLAGSHIP],
            "shows": [
                {"show_id": FIELD, "label": "Field Show"},
                {"show_id": FLAGSHIP, "label": "Flagship Show"},
            ],
            "show_count": 2,
        }
    ]


def test_an_unreadable_timestamp_is_dropped_from_velocity_rather_than_crashing(tmp_path: Path) -> None:
    """A legacy or corrupted ``updated_at`` must not take the portfolio down.

    Velocity is the one projection that reads a stored timestamp back. A row it
    cannot parse is excluded from the rate — never counted, never fatal — so a
    single bad row cannot cost the operator the whole portfolio.
    """
    conn = _seed(tmp_path / "issue32-timestamps.sqlite3")
    store.update(
        conn,
        "appearance_opportunities",
        store.fetch_one(
            conn,
            "SELECT id FROM appearance_opportunities WHERE tenant_id = ? AND show_id = ? AND guest_id = ?",
            (TENANT, FLAGSHIP, "chad-kroeger"),
        )["id"],
        {"updated_at": "not-a-timestamp"},
    )
    conn.commit()

    flagship = _show(_portfolio(conn), FLAGSHIP)
    assert flagship["pipeline"]["booked"] == 2  # the milestone still counts it
    assert flagship["booking_velocity"] == {
        "window_days": 28,
        "booked_in_window": 0,
        "bookings_per_week": 0.0,
    }


def test_drilldown_opens_one_show_and_names_the_guests_it_shares(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue32-drilldown.sqlite3")
    detail = network_dashboard.show_detail(
        conn, tenant_id=TENANT, show_id=FLAGSHIP, actor_role="network_operator", now=NOW
    )

    assert detail["tenant_id"] == TENANT
    assert detail["show"]["show_id"] == FLAGSHIP
    assert detail["show"]["label"] == "Flagship Show"
    assert detail["show"]["pipeline"] == _show(_portfolio(conn), FLAGSHIP)["pipeline"]
    assert detail["overlap"] == [
        {
            "guest_id": "chad-kroeger",
            "guest_name": "Chad Kroeger",
            "show_id": FIELD,
            "label": "Field Show",
        }
    ]

    # A show that is not registered active in this tenant is a 404, whether it
    # belongs to nobody or to somebody else.
    for show_id in ("no-such-show", RIVAL_SHOW):
        with pytest.raises(network_dashboard.NetworkDashboardError) as unknown:
            network_dashboard.show_detail(conn, tenant_id=TENANT, show_id=show_id, actor_role="network_operator")
        assert unknown.value.status_code == 404


# --- security: one role, one tenant ----------------------------------------


def test_only_the_network_operator_reads_any_network_surface(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue32-roles.sqlite3")
    assert network_dashboard.READ_ROLES == frozenset({"network_operator"})

    for role in (*NON_NETWORK_ROLES, "", None, "network_operator "):
        with pytest.raises(network_dashboard.NetworkDashboardError) as portfolio_denied:
            network_dashboard.portfolio(conn, tenant_id=TENANT, actor_role=role)
        assert portfolio_denied.value.status_code == 403

        with pytest.raises(network_dashboard.NetworkDashboardError) as drilldown_denied:
            network_dashboard.show_detail(conn, tenant_id=TENANT, show_id=FLAGSHIP, actor_role=role)
        assert drilldown_denied.value.status_code == 403

        with pytest.raises(network_dashboard.NetworkDashboardError) as export_denied:
            network_dashboard.export_health_report(conn, tenant_id=TENANT, actor_role=role, actor_id=NETWORK_ACTOR)
        assert export_denied.value.status_code == 403

        with pytest.raises(network_dashboard.NetworkDashboardError) as receipts_denied:
            network_dashboard.list_report_receipts(conn, tenant_id=TENANT, actor_role=role)
        assert receipts_denied.value.status_code == 403

    # A refused read writes nothing, so no receipt was minted along the way.
    assert store.fetch_one(conn, "SELECT COUNT(*) AS count FROM network_report_receipts")["count"] == 0


def test_the_portfolio_never_reaches_a_second_tenant(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue32-tenants.sqlite3")
    portfolio = _portfolio(conn)
    rendered = json.dumps(portfolio)

    assert RIVAL_SHOW not in rendered
    assert "Rival" not in rendered
    assert "rival-guest" not in rendered
    assert portfolio["totals"]["revenue_minor"] == 350_000  # the rival's 999,000 is absent

    # The other tenant's own operator sees their network and only theirs.
    rival = _portfolio(conn, tenant_id=RIVAL_TENANT)
    assert [row["show_id"] for row in rival["shows"]] == [RIVAL_SHOW]
    assert rival["totals"]["revenue_minor"] == 999_000
    assert FLAGSHIP not in json.dumps(rival)

    # Scope is a custody reference, not free text: a wildcard cannot widen it.
    # Scope is a custody reference, not free text: no wildcard or injected
    # predicate widens it, and an unrecognized-but-well-formed tenant simply
    # has no shows rather than falling back to somebody else's.
    for hostile in ("hospes' OR '1'='1", "*", "%", ""):
        with pytest.raises(platform.PlatformError):
            network_dashboard.portfolio(conn, tenant_id=hostile, actor_role="network_operator")
    empty = network_dashboard.portfolio(conn, tenant_id="unknown-tenant", actor_role="network_operator")
    assert empty["shows"] == []
    assert empty["totals"]["shows"] == 0
    assert empty["totals"]["currency"] is None


# --- receipts + pdf: the health report -------------------------------------


def test_health_report_escapes_operator_text_and_carries_no_external_asset(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue32-escaping.sqlite3", flagship_label=HOSTILE_LABEL)
    report = network_dashboard.export_health_report(
        conn, tenant_id=TENANT, actor_role="network_operator", actor_id=NETWORK_ACTOR, now=NOW
    )

    assert isinstance(report, str)
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "Chad Kroeger" in report  # the overlap matrix renders in the report
    assert "Field Show" in report
    # A report that is mailed around must not fetch anything when it is opened.
    for external in ("http://", "https://", "src=", "<link", "<iframe"):
        assert external not in report


def test_every_export_appends_an_attributable_receipt(tmp_path: Path) -> None:
    conn = _seed(tmp_path / "issue32-receipts.sqlite3")

    html_report = network_dashboard.export_health_report(
        conn, tenant_id=TENANT, actor_role="network_operator", actor_id=NETWORK_ACTOR, now=NOW
    )
    json_report = network_dashboard.export_health_report(
        conn,
        tenant_id=TENANT,
        actor_role="network_operator",
        actor_id=NETWORK_ACTOR,
        format="json",
        now=NOW,
    )
    assert json.loads(json_report)["totals"]["shows"] == 2

    receipts = network_dashboard.list_report_receipts(conn, tenant_id=TENANT, actor_role="network_operator")
    assert len(receipts) == 2
    assert {receipt["report_format"] for receipt in receipts} == {"html", "json"}
    by_format = {receipt["report_format"]: receipt for receipt in receipts}
    assert by_format["html"]["document_checksum"] == hashlib.sha256(html_report.encode("utf-8")).hexdigest()
    assert by_format["json"]["document_checksum"] == hashlib.sha256(json_report.encode("utf-8")).hexdigest()
    assert by_format["html"]["document_checksum"] != by_format["json"]["document_checksum"]

    for receipt in receipts:
        assert receipt["event_type"] == network_dashboard.EXPORT_EVENT == "network.health_report_exported"
        assert receipt["actor_id"] == NETWORK_ACTOR
        assert receipt["actor_role"] == "network_operator"
        assert receipt["tenant_id"] == TENANT
        assert receipt["show_count"] == 2
        assert receipt["overlap_count"] == 1
        assert receipt["revenue_minor"] == 350_000
        assert receipt["details"] == {
            "booked": 3,
            "candidates": 6,
            "currency": "USD",
            "window_days": 28,
        }

    # The trail is tenant-scoped and bounded like every other receipt reader.
    assert network_dashboard.list_report_receipts(conn, tenant_id=RIVAL_TENANT, actor_role="network_operator") == []
    assert (
        len(network_dashboard.list_report_receipts(conn, tenant_id=TENANT, actor_role="network_operator", limit=1)) == 1
    )
    for bad_limit in (0, 501, True, "10"):
        with pytest.raises(network_dashboard.NetworkDashboardError):
            network_dashboard.list_report_receipts(
                conn, tenant_id=TENANT, actor_role="network_operator", limit=bad_limit
            )


def test_pdf_export_renders_through_the_extra_and_fails_visibly_without_it(tmp_path: Path, monkeypatch) -> None:
    conn = _seed(tmp_path / "issue32-pdf.sqlite3")

    class _StubHTML:
        """The narrowest stand-in for the optional WeasyPrint dependency."""

        def __init__(self, *, string: str) -> None:
            self.string = string

        def write_pdf(self) -> bytes:
            assert "HOSPES network health" in self.string
            return b"%PDF-1.7 synthetic network health report"

    class _StubModule:
        HTML = _StubHTML

    monkeypatch.setitem(sys.modules, "weasyprint", _StubModule)
    rendered = network_dashboard.export_health_report(
        conn,
        tenant_id=TENANT,
        actor_role="network_operator",
        actor_id=NETWORK_ACTOR,
        format="pdf",
        now=NOW,
    )
    assert isinstance(rendered, bytes)
    assert rendered.startswith(b"%PDF")
    receipt = network_dashboard.list_report_receipts(conn, tenant_id=TENANT, actor_role="network_operator")[0]
    assert receipt["report_format"] == "pdf"
    assert receipt["document_checksum"] == hashlib.sha256(rendered).hexdigest()

    # Without the declared extra the export names the missing extra rather than
    # returning a partial document — and it receipts nothing it never produced.
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    with pytest.raises(network_dashboard.NetworkDashboardError) as missing:
        network_dashboard.export_health_report(
            conn,
            tenant_id=TENANT,
            actor_role="network_operator",
            actor_id=NETWORK_ACTOR,
            format="pdf",
            now=NOW,
        )
    assert missing.value.status_code == 503
    assert "pdf" in missing.value.detail
    assert len(network_dashboard.list_report_receipts(conn, tenant_id=TENANT, actor_role="network_operator")) == 1

    # A renderer that returns nothing is a failure, not an empty attachment.
    class _EmptyHTML(_StubHTML):
        def write_pdf(self) -> bytes:
            return b""

    class _EmptyModule:
        HTML = _EmptyHTML

    monkeypatch.setitem(sys.modules, "weasyprint", _EmptyModule)
    with pytest.raises(network_dashboard.NetworkDashboardError) as empty:
        network_dashboard.export_health_report(
            conn,
            tenant_id=TENANT,
            actor_role="network_operator",
            actor_id=NETWORK_ACTOR,
            format="pdf",
            now=NOW,
        )
    assert empty.value.status_code == 503
    assert len(network_dashboard.list_report_receipts(conn, tenant_id=TENANT, actor_role="network_operator")) == 1

    assert network_dashboard.REPORT_FORMATS == ("html", "json", "pdf")
    with pytest.raises(network_dashboard.NetworkDashboardError):
        network_dashboard.export_health_report(
            conn,
            tenant_id=TENANT,
            actor_role="network_operator",
            actor_id=NETWORK_ACTOR,
            format="docx",
        )


# --- api -------------------------------------------------------------------


@pytest.mark.skipif(TestClient is None, reason="the api optional extra is not installed")
def test_api_network_routes_are_role_gated_tenant_scoped_and_private(tmp_path: Path) -> None:
    database = tmp_path / "issue32-api.sqlite3"
    _seed(database).close()

    with _client(database) as client:
        portfolio = client.get("/v1/network/portfolio", headers=NETWORK_HEADERS)
        assert portfolio.status_code == 200, portfolio.text
        assert portfolio.headers["Cache-Control"] == "no-store, private"
        assert portfolio.json()["totals"]["shows"] == 2
        assert portfolio.json()["totals"]["overlapping_guests"] == 1
        assert "Rival" not in portfolio.text

        drilldown = client.get(f"/v1/network/shows/{FLAGSHIP}", headers=NETWORK_HEADERS)
        assert drilldown.status_code == 200
        assert drilldown.json()["show"]["label"] == "Flagship Show"
        assert drilldown.headers["Cache-Control"] == "no-store, private"

        # No path segment carries a tenant, so another tenant's show is a 404.
        assert client.get(f"/v1/network/shows/{RIVAL_SHOW}", headers=NETWORK_HEADERS).status_code == 404

        report = client.get("/v1/network/report.html", headers=NETWORK_HEADERS)
        assert report.status_code == 200
        assert report.headers["content-type"].startswith("text/html")
        assert report.headers["Cache-Control"] == "no-store, private"
        assert f'filename="network-health-{TENANT}.html"' in report.headers["Content-Disposition"]
        assert "Chad Kroeger" in report.text
        assert client.get("/v1/network/report.json", headers=NETWORK_HEADERS).status_code == 200
        assert client.get("/v1/network/report.docx", headers=NETWORK_HEADERS).status_code == 404

        receipts = client.get("/v1/network/receipts", headers=NETWORK_HEADERS)
        assert receipts.status_code == 200
        assert [row["report_format"] for row in receipts.json()] == ["json", "html"]
        assert all(row["actor_id"] == NETWORK_ACTOR for row in receipts.json())

        # Every network route refuses a role that is not the network operator,
        # including a producer who legitimately operates a show in this tenant.
        for route in NETWORK_ROUTES:
            denied = client.get(route, headers=PRODUCER_HEADERS)
            assert denied.status_code == 403, route
            assert "network operator" in denied.json()["detail"]

        # There is no network route that writes, books, publishes, or sends.
        assert client.post("/v1/network/portfolio", json={}, headers=NETWORK_HEADERS).status_code in {404, 405}
        assert client.post("/v1/network/report.pdf", json={}, headers=NETWORK_HEADERS).status_code in {404, 405}
        assert client.get("/v1/network/publish", headers=NETWORK_HEADERS).status_code == 404


# --- storage ---------------------------------------------------------------


def test_migration_adds_the_network_report_receipt_ledger(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue32-migrations.sqlite3")
    assert migrations.LATEST_VERSION >= 22
    assert migrations.current_version(conn) == migrations.LATEST_VERSION
    assert "network_report_receipts" in store.table_names(conn)
    applied = {int(row["version"]): str(row["name"]) for row in migrations.applied_migrations(conn)}
    assert applied[22] == "network_report_receipts"

    columns = {row[1] for row in conn.execute("PRAGMA table_info(network_report_receipts)")}
    assert {
        "tenant_id",
        "event_type",
        "report_format",
        "document_checksum",
        "show_count",
        "overlap_count",
        "revenue_minor",
        "actor_id",
        "actor_role",
        "created_at",
    } <= columns


# --- ui + documentation ----------------------------------------------------


def test_dashboard_domain_kernel_docs_and_packaged_resources_are_complete() -> None:
    for relative in (
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/views.mjs",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/styles.css",
        "dashboard/assets/guide.json",
        "config/domain_kernel.yaml",
        "spec/permission-matrix.yaml",
    ):
        assert (ROOT / relative).read_bytes() == (ROOT / "hospes" / "resources" / relative).read_bytes(), relative

    index = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    workspace = (ROOT / "dashboard" / "assets" / "partnership-workspace.html").read_text(encoding="utf-8")
    javascript = (ROOT / "dashboard" / "assets" / "partnership-workspace.js").read_text(encoding="utf-8")
    api_javascript = (ROOT / "dashboard" / "assets" / "api.js").read_text(encoding="utf-8")
    capabilities = (ROOT / "dashboard" / "assets" / "capabilities.mjs").read_text(encoding="utf-8")
    views = (ROOT / "dashboard" / "assets" / "views.mjs").read_text(encoding="utf-8")
    styles = (ROOT / "dashboard" / "assets" / "styles.css").read_text(encoding="utf-8")

    # The Network view is reachable, and it renders the portfolio, the overlap
    # matrix, the export control, and the receipt trail.
    assert 'data-view="network"' in index
    assert 'data-guide="network-tab"' in index
    assert 'id="network-view"' in workspace
    assert 'id="network-table"' in workspace
    assert 'id="network-overlap-table"' in workspace
    assert 'id="network-receipt-list"' in workspace
    assert 'id="btn-network-export"' in workspace
    assert 'id="network-report-format"' in workspace
    for column in ("Show", "Candidates", "Approved", "Booked", "Bookings/week", "Revenue", "Drilldown"):
        assert f">{column}</th>" in workspace
    assert "'network'" in views
    assert "loadNetworkPortfolio" in api_javascript
    assert "loadNetworkReceipts" in api_javascript
    assert "/network/report." in api_javascript
    assert "renderNetworkPortfolio" in javascript
    assert "escapeHTML(entry.guest_name)" in javascript
    assert "openShowDashboard" in javascript
    assert "canViewNetwork" in capabilities
    assert "canExportNetworkReport" in capabilities
    assert "role === 'network_operator'" in capabilities
    # Every control the view adds carries a full touch target at every width.
    for rule in (".network-toolbar select", ".network-toolbar button", ".network-table .network-drilldown"):
        assert rule in styles
    assert styles.count("min-height: 44px") >= 4

    guide = json.loads((ROOT / "dashboard" / "assets" / "guide.json").read_text(encoding="utf-8"))
    assert {"canViewNetwork", "canExportNetworkReport"} <= set(guide["capabilities"])
    assert {
        "network-tab",
        "network-view",
        "network-portfolio",
        "network-overlap",
        "network-export",
        "network-receipts",
    } <= set(guide["elements"])
    assert any(beat["view"] == "network" for beat in guide["beats"])

    domain = (ROOT / "config" / "domain_kernel.yaml").read_text(encoding="utf-8")
    assert "network_dashboard_contract:" in domain
    assert "role: network_operator" in domain
    assert "receipt_event: network.health_report_exported" in domain
    assert "velocity_window_days: 28" in domain

    matrix = (ROOT / "spec" / "permission-matrix.yaml").read_text(encoding="utf-8")
    assert "team_roles:" in matrix
    assert "network_operator:" in matrix
    assert "scope: network" in matrix

    documentation = (ROOT / "docs" / "network-dashboard.md").read_text(encoding="utf-8")
    for phrase in (
        "network_operator",
        "spec/states.json",
        "booking velocity",
        "guest overlap",
        "network_report_receipts",
        "document_checksum",
        "/v1/network/portfolio",
        "pip install -e '.[pdf]'",
        "404",
    ):
        assert phrase in documentation, phrase
    assert "docs/network-dashboard.md" in (ROOT / "README.md").read_text(encoding="utf-8")
