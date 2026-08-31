"""Executable completion predicate for HOSPES issue #25.

Close condition: sponsor inventory and slot CRUD, revenue totals, accounting
CSV, claims governance, Revenue UI, and the configurable unfilled-slot gate pass.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import synthetic_bearer_authenticator
from hospes import configuration, distribution, migrations, platform, sponsors, store
from hospes.api import create_app

try:  # optional extra
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - exercised only without the optional extra
    TestClient = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
NOW = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
TENANT = "hospes"
SHOW = "flagship"
OTHER_SHOW = "field"
OPEN_SHOW = "client-x"
EPISODE = "episode-12"
PRODUCER_TOKEN = "issue25-producer-token-0123456789abcdef"  # allow-secret: fixture
OWNER_TOKEN = "issue25-owner-token-0123456789abcdefgh"  # allow-secret: fixture
HOST_TOKEN = "issue25-host-token-0123456789abcdefghij"  # allow-secret: fixture

pytestmark = pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


class _SessionShowASGI:
    """Project the operator's active show into request scope, as the shell does."""

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
    return synthetic_bearer_authenticator(
        {
            PRODUCER_TOKEN: ("producer_fixture", "producer", TENANT),
            OWNER_TOKEN: ("owner_fixture", "editorial_owner", TENANT),
            HOST_TOKEN: ("host_fixture", "host", TENANT),
        }
    )


def _headers(
    token: str = PRODUCER_TOKEN,  # allow-secret: fixture
    session_show: str | None = SHOW,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}  # allow-secret: fixture
    if session_show is not None:
        headers["X-Session-Show"] = session_show
    return headers


def _seed(path: Path) -> None:
    conn = store.connect(path)
    for show_id, label in (
        (SHOW, "Flagship Show"),
        (OTHER_SHOW, "Field Show"),
        (OPEN_SHOW, "Client X"),
    ):
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
        )
    conn.commit()
    conn.close()


def _client(path: Path, *, bound_show: str | None = None):
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


def _sponsor(conn, name: str, *, show_id: str = SHOW, slug: str = "ag") -> dict:
    return sponsors.create_sponsor(
        conn,
        tenant_id=TENANT,
        show_id=show_id,
        name=name,
        contact_ref=f"vault://sponsor/{slug}/contact",
        terms_ref=f"vault://sponsor/{slug}/terms",
        category="nutrition",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )


def _declare(conn, *, show_id: str = SHOW, episode_id: str = EPISODE, committed=("pre", "mid")):
    return sponsors.declare_slots(
        conn,
        tenant_id=TENANT,
        show_id=show_id,
        episode_id=episode_id,
        slots=[
            {"slot_type": slot_type, "rate_minor": 150_000, "committed": slot_type in committed}
            for slot_type in ("pre", "mid", "post")
        ],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )


def _assign(conn, sponsor_id: str, slot_type: str, status: str = "sold", *, show_id: str = SHOW):
    return sponsors.assign_slot(
        conn,
        tenant_id=TENANT,
        show_id=show_id,
        episode_id=EPISODE,
        slot_type=slot_type,
        sponsor_id=sponsor_id,
        status=status,
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Inventory and slot CRUD.
# ---------------------------------------------------------------------------


def test_inventory_declaration_is_an_idempotent_put_with_a_sale_guard(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "inventory.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")
    declared = _declare(conn)
    assert [row["slot_type"] for row in declared] == ["mid", "post", "pre"]
    assert {row["status"] for row in declared} == {"available"}
    assert {row["slot_type"] for row in declared if row["committed"]} == {"pre", "mid"}

    # Re-declaring the same inventory changes the rate in place, not the row count.
    again = sponsors.declare_slots(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        slots=[
            {"slot_type": "pre", "rate_minor": 200_000, "committed": True},
            {"slot_type": "mid", "rate_minor": 150_000, "committed": True},
            {"slot_type": "post", "rate_minor": 150_000, "committed": False},
        ],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert len(again) == 3
    assert {row["slot_type"]: row["rate_minor"] for row in again}["pre"] == 200_000

    # An undeclared slot is withdrawn while it is unsold...
    trimmed = sponsors.declare_slots(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        slots=[
            {"slot_type": "pre", "rate_minor": 200_000, "committed": True},
            {"slot_type": "mid", "rate_minor": 150_000, "committed": True},
        ],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert [row["slot_type"] for row in trimmed] == ["mid", "pre"]

    # ...and refused once a sponsor holds it, so a sale is never silently erased.
    _declare(conn)
    _assign(conn, sponsor["id"], "post")
    with pytest.raises(sponsors.SponsorError, match="release the sponsorship") as excinfo:
        sponsors.declare_slots(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            episode_id=EPISODE,
            slots=[{"slot_type": "pre", "rate_minor": 150_000, "committed": True}],
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    assert excinfo.value.status_code == 409
    conn.close()


def test_slot_assignment_release_and_double_sale_guard(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "assignment.sqlite3")
    first = _sponsor(conn, "Athletic Greens")
    second = _sponsor(conn, "AG1", slug="ag1")
    _declare(conn)

    sold = _assign(conn, first["id"], "pre")
    assert sold["status"] == "sold" and sold["sold_at"] is not None
    assert sold["recorded_by"] == "producer_fixture"
    reserved = _assign(conn, first["id"], "post", status="reserved")
    assert reserved["status"] == "reserved" and reserved["sold_at"] is None

    # One episode slot belongs to exactly one sponsor.
    with pytest.raises(sponsors.SponsorError, match="already held by another sponsor"):
        _assign(conn, second["id"], "pre")

    # Re-assigning the same sponsor is an idempotent state change, not a duplicate.
    promoted = _assign(conn, first["id"], "post")
    assert promoted["id"] == reserved["id"] and promoted["status"] == "sold"

    released = sponsors.release_slot(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        slot_type="post",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert released["status"] == "available"
    states = {
        row["slot_type"]: row["status"]
        for row in sponsors.list_slots(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", episode_id=EPISODE)
    }
    assert states == {"pre": "sold", "mid": "available", "post": "available"}

    with pytest.raises(sponsors.SponsorError, match="no sponsorship holds this slot"):
        sponsors.release_slot(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            episode_id=EPISODE,
            slot_type="post",
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )

    # Selling into inventory that was never declared fails closed.
    with pytest.raises(sponsors.SponsorError, match="declare the episode ad inventory"):
        sponsors.assign_slot(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            episode_id="episode-99",
            slot_type="pre",
            sponsor_id=first["id"],
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    conn.close()


def test_sponsor_registration_custody_and_status_boundaries(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "sponsor.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")
    assert sponsor["contact_ref"] == "vault://sponsor/ag/contact"

    # Registering the same brand twice is idempotent...
    assert _sponsor(conn, "Athletic Greens")["id"] == sponsor["id"]

    # ...unless the custody references disagree.
    with pytest.raises(sponsors.SponsorError, match="different custody references"):
        sponsors.create_sponsor(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            name="Athletic Greens",
            contact_ref="vault://sponsor/other/contact",
            terms_ref="vault://sponsor/ag/terms",
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )

    # Raw contact data never enters the record, by name or by reference.
    for field, values in (
        ("name", ("sponsor@example.test", "a")),
        ("contact_ref", ("sponsor@example.test", "555-123-4567")),
    ):
        for value in values:
            payload = {
                "name": "Fresh Brand",
                "contact_ref": "vault://sponsor/fresh/contact",
                "terms_ref": "vault://sponsor/fresh/terms",
                field: value,
            }
            with pytest.raises(platform.PlatformError):
                sponsors.create_sponsor(
                    conn,
                    tenant_id=TENANT,
                    show_id=SHOW,
                    actor_id="producer_fixture",
                    actor_role="producer",
                    now=NOW,
                    **payload,
                )

    with pytest.raises(sponsors.SponsorError, match="category"):
        sponsors.create_sponsor(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            name="Categorized Brand",
            contact_ref="vault://sponsor/cat/contact",
            terms_ref="vault://sponsor/cat/terms",
            category="!!bad category!!",
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )

    _declare(conn)
    paused = sponsors.set_sponsor_status(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=sponsor["id"],
        status="paused",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert paused["status"] == "paused"
    with pytest.raises(sponsors.SponsorError, match="not active"):
        _assign(conn, sponsor["id"], "pre")
    with pytest.raises(sponsors.SponsorError, match="sponsor status must be"):
        sponsors.set_sponsor_status(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            sponsor_id=sponsor["id"],
            status="deleted",
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    with pytest.raises(sponsors.SponsorError, match="sponsor not found") as missing:
        sponsors.set_sponsor_status(
            conn,
            tenant_id=TENANT,
            show_id=OTHER_SHOW,
            sponsor_id=sponsor["id"],
            status="ended",
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    assert missing.value.status_code == 404
    conn.close()


def test_slot_and_rate_validation_rejects_unusable_values(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "validation.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")

    def declare(slots):
        return sponsors.declare_slots(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            episode_id=EPISODE,
            slots=slots,
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )

    for slots, message in (
        ("pre", "slots must be a list"),
        ([["pre"]], "each slot declaration must be an object"),
        ([{"slot_type": "pre", "rate_minor": 1, "sold": True}], "unsupported fields"),
        ([{"slot_type": "middle", "rate_minor": 1}], "slot_type must be one of"),
        ([{"slot_type": "pre", "rate_minor": "1500"}], "minor currency units"),
        ([{"slot_type": "pre", "rate_minor": True}], "minor currency units"),
        ([{"slot_type": "pre", "rate_minor": -1}], "must be between 0"),
        ([{"slot_type": "pre", "rate_minor": 10**12}], "must be between 0"),
        ([{"slot_type": "pre", "rate_minor": 1, "committed": "yes"}], "committed must be a boolean"),
        (
            [{"slot_type": "pre", "rate_minor": 1}, {"slot_type": "pre", "rate_minor": 2}],
            "declared twice",
        ),
    ):
        with pytest.raises(sponsors.SponsorError, match=message):
            declare(slots)

    declare([{"slot_type": "pre", "rate_minor": 150_000}])
    with pytest.raises(sponsors.SponsorError, match="status must be one of"):
        _assign(conn, sponsor["id"], "pre", status="invoiced")
    override = sponsors.assign_slot(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        slot_type="pre",
        sponsor_id=sponsor["id"],
        rate_minor=99_000,
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    assert override["rate_minor"] == 99_000
    with pytest.raises(sponsors.SponsorError, match="limit must be an integer"):
        sponsors.list_receipts(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", limit=0)
    conn.close()


# ---------------------------------------------------------------------------
# Revenue totals and the accounting export.
# ---------------------------------------------------------------------------


def test_revenue_totals_reproduce_the_issue_demo_and_recognize_sold_only(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "revenue.sqlite3")
    athletic = _sponsor(conn, "Athletic Greens")
    ag1 = _sponsor(conn, "AG1", slug="ag1")
    _declare(conn, committed=())
    _assign(conn, athletic["id"], "pre")
    _assign(conn, athletic["id"], "mid")
    _assign(conn, athletic["id"], "post", status="reserved")

    sponsors.declare_slots(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id="episode-13",
        slots=[{"slot_type": "mid", "rate_minor": 150_000, "committed": False}],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    sponsors.assign_slot(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id="episode-13",
        slot_type="mid",
        sponsor_id=ag1["id"],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )

    report = sponsors.revenue_report(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer")
    assert report["currency"] == "USD"
    episodes = {row["episode_id"]: row for row in report["episodes"]}
    twelve = episodes[EPISODE]
    assert (twelve["slots_sold"], twelve["slots_total"]) == (2, 3)
    assert twelve["slots_reserved"] == 1 and twelve["slots_available"] == 0
    assert twelve["revenue_minor"] == 300_000
    assert [
        (row["sponsor_name"], row["slots_sold"], row["slots_held"], row["revenue_minor"]) for row in twelve["sponsors"]
    ] == [("Athletic Greens", 2, 3, 300_000)]
    assert episodes["episode-13"]["revenue_minor"] == 150_000
    assert report["totals"]["revenue_minor"] == 450_000
    assert report["totals"]["episodes"] == 2

    # A single-episode read is the same shape, scoped.
    scoped = sponsors.revenue_report(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer", episode_id=EPISODE)
    assert [row["episode_id"] for row in scoped["episodes"]] == [EPISODE]

    # Legacy rows written before the inventory table still surface honestly.
    platform.add_sponsorship(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=ag1["id"],
        episode_id="episode-14",
        slot_type="pre",
        rate_minor=50_000,
        now=NOW,
    )
    undeclared = {
        row["episode_id"]: row
        for row in sponsors.list_slots(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer")
    }["episode-14"]
    assert undeclared["declared"] is False and undeclared["status"] == "sold"
    conn.close()


def test_accounting_csv_has_the_five_declared_columns_and_is_formula_safe(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "csv.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")
    hostile = sponsors.create_sponsor(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        name="=cmd|' /c calc'!A1",
        contact_ref="vault://sponsor/hostile/contact",
        terms_ref="vault://sponsor/hostile/terms",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    _declare(conn, committed=())
    _assign(conn, sponsor["id"], "pre")
    _assign(conn, sponsor["id"], "mid", status="reserved")
    sponsors.declare_slots(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id="episode-15",
        slots=[{"slot_type": "pre", "rate_minor": 12_345}],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    sponsors.assign_slot(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id="episode-15",
        slot_type="pre",
        sponsor_id=hostile["id"],
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    # An episode id can never start with a formula prefix (it is an opaque
    # custody reference), so the neutralizer is proven on the cell that can.
    with pytest.raises(platform.PlatformError, match="opaque custody reference"):
        sponsors.declare_slots(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            episode_id="-episode-15",
            slots=[{"slot_type": "pre", "rate_minor": 1}],
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    assert sponsors._csv_cell("=SUM(A1)") == "'=SUM(A1)"
    assert sponsors._csv_cell(None) == ""

    rendered = sponsors.accounting_csv(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer")
    lines = rendered.strip().splitlines()
    assert lines[0] == "Episode,Sponsor,Slot,Rate,Date"
    assert sponsors.CSV_COLUMNS == ("Episode", "Sponsor", "Slot", "Rate", "Date")
    # Reserved slots are not revenue, so they are not in the accounting export.
    assert len(lines) == 3
    assert f"{EPISODE},Athletic Greens,pre,1500.00,2026-08-14" in rendered
    # Spreadsheet-formula prefixes are neutralized in every cell that can carry them.
    assert "episode-15,'=cmd|' /c calc'!A1,pre,123.45," in rendered
    conn.close()


# ---------------------------------------------------------------------------
# Claims governance.
# ---------------------------------------------------------------------------


def test_claims_are_cited_dated_and_approved_only_by_an_editorial_owner(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "claims.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")

    def record(**overrides):
        payload = {
            "claim": "Athletic Greens ships to 40 countries.",
            "source_url": "https://example.test/press/ag",
            "verified_date": "2026-08-01",
        }
        payload.update(overrides)
        return sponsors.record_claim(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            sponsor_id=sponsor["id"],
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
            **payload,
        )

    claim = record()
    assert claim["approved_for_external_use"] is False
    assert claim["approved_by"] is None and claim["approved_at"] is None

    # Recording the same claim text with the same evidence is idempotent.
    assert record()["claim_id"] == claim["claim_id"]
    with pytest.raises(sponsors.SponsorError, match="different evidence"):
        record(source_url="https://example.test/press/other")

    for overrides, message in (
        ({"claim": "   "}, "claim text is required"),
        ({"claim": "x" * 501}, "exceeds 500"),
        ({"claim": "bad\x07claim"}, "control characters"),
        ({"source_url": "javascript:alert(1)"}, "public http"),
        ({"source_url": "file:///etc/passwd"}, "public http"),
        ({"verified_date": "not-a-date"}, "ISO 8601 date"),
        ({"verified_date": "2099-01-01"}, "cannot be in the future"),
    ):
        with pytest.raises(sponsors.SponsorError, match=message):
            record(**overrides)

    # A producer may sell the slot but never approve the claim about it.
    with pytest.raises(sponsors.SponsorError, match="cannot approve sponsor claims") as denied:
        sponsors.approve_claim(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            sponsor_id=sponsor["id"],
            claim_id=claim["claim_id"],
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    assert denied.value.status_code == 403

    approved = sponsors.approve_claim(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=sponsor["id"],
        claim_id=claim["claim_id"],
        actor_id="owner_fixture",
        actor_role="editorial_owner",
        now=NOW,
    )
    assert approved["approved_for_external_use"] is True
    assert approved["approved_by"] == "owner_fixture"
    assert approved["approved_by_role"] == "editorial_owner"

    withdrawn = sponsors.approve_claim(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=sponsor["id"],
        claim_id=claim["claim_id"],
        approved=False,
        actor_id="owner_fixture",
        actor_role="editorial_owner",
        now=NOW,
    )
    assert withdrawn["approved_for_external_use"] is False
    assert withdrawn["approved_by"] is None and withdrawn["approved_at"] is None

    with pytest.raises(sponsors.SponsorError, match="approved must be a boolean"):
        sponsors.approve_claim(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            sponsor_id=sponsor["id"],
            claim_id=claim["claim_id"],
            approved="yes",
            actor_id="owner_fixture",
            actor_role="editorial_owner",
            now=NOW,
        )
    with pytest.raises(sponsors.SponsorError, match="sponsor claim not found") as missing:
        sponsors.approve_claim(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            sponsor_id=sponsor["id"],
            claim_id="claim-that-does-not-exist",
            actor_id="owner_fixture",
            actor_role="editorial_owner",
            now=NOW,
        )
    assert missing.value.status_code == 404

    listed = sponsors.list_claims(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="host",
        sponsor_id=sponsor["id"],
    )
    assert [row["claim_id"] for row in listed] == [claim["claim_id"]]
    roster = sponsors.list_sponsors(conn, tenant_id=TENANT, show_id=SHOW, actor_role="host")
    assert roster[0]["unapproved_claim_count"] == 1
    assert (
        sponsors.list_sponsors(conn, tenant_id=TENANT, show_id=SHOW, actor_role="host", include_claims=False)[0].get(
            "claims"
        )
        is None
    )
    conn.close()


# ---------------------------------------------------------------------------
# The configurable publication gate.
# ---------------------------------------------------------------------------


def test_unfilled_committed_slots_and_unapproved_claims_block_publication(
    tmp_path: Path,
) -> None:
    conn = store.connect(tmp_path / "gate.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")
    _declare(conn)

    blockers = platform.publish_blockers(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE)
    assert {row["slot_type"] for row in blockers} == {"pre", "mid"}
    assert {row["kind"] for row in blockers} == {"sponsor_slot_unfilled"}

    # A distribution draft created against a blocked episode is blocked.
    draft = distribution.create_draft(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        platform_name="rss",
        metadata={"title": "Episode 12"},
        idempotency_key=f"{EPISODE}:rss",
        now=NOW,
    )
    assert draft["status"] == "blocked"
    with pytest.raises(platform.PlatformError):
        distribution.authorize_publish(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            distribution_id=draft["id"],
            outbound_mode="manual_receipt",
            authorized_by="owner_fixture",
            authorization_ref="receipt://human/publish-12",
            idempotency_key=f"{EPISODE}:rss:human",
            now=NOW,
        )

    _assign(conn, sponsor["id"], "pre")
    _assign(conn, sponsor["id"], "mid")
    assert not platform.publish_blockers(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE)

    # An unapproved claim on a sold sponsor is the second blocker.
    claim = sponsors.record_claim(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=sponsor["id"],
        claim="Athletic Greens ships to 40 countries.",
        source_url="https://example.test/press/ag",
        verified_date="2026-08-01",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    claim_blockers = platform.publish_blockers(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE)
    assert [row["kind"] for row in claim_blockers] == ["sponsor_claim_unapproved"]
    assert claim_blockers[0]["unapproved_claim_count"] == 1

    sponsors.approve_claim(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=sponsor["id"],
        claim_id=claim["claim_id"],
        actor_id="owner_fixture",
        actor_role="editorial_owner",
        now=NOW,
    )
    assert not platform.publish_blockers(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE)

    # Rights clearance still leads the blocker list; the two gates compose.
    clearance = platform.add_clearance(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        clearance_type="music",
        rights_holder_ref="vault://rights/one",
        license_terms_ref="vault://terms/one",
        now=NOW,
    )
    composed = platform.publish_blockers(conn, tenant_id=TENANT, show_id=SHOW, episode_id=EPISODE)
    assert composed[0]["id"] == clearance["id"] and composed[0]["status"] == "pending"
    conn.close()


def test_the_gate_is_per_show_configuration_not_code(tmp_path: Path) -> None:
    default_policy = configuration.sponsor_inventory_policy(SHOW)
    assert default_policy.block_publication_on_unfilled_committed_slots is True
    assert default_policy.source == "runtime"

    open_policy = configuration.sponsor_inventory_policy(OPEN_SHOW)
    assert open_policy.block_publication_on_unfilled_committed_slots is False
    assert open_policy.require_approved_claims_before_publication is True
    assert open_policy.source == f"show:{OPEN_SHOW}"

    # An unregistered show falls back to the safe runtime default, never an error.
    assert configuration.sponsor_inventory_policy("not-a-tracked-show").source == "runtime"

    conn = store.connect(tmp_path / "policy.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens", show_id=OPEN_SHOW)
    _declare(conn, show_id=OPEN_SHOW)
    assert not platform.publish_blockers(conn, tenant_id=TENANT, show_id=OPEN_SHOW, episode_id=EPISODE)

    # The claims half of the gate remains armed for that same show.
    _assign(conn, sponsor["id"], "pre", show_id=OPEN_SHOW)
    sponsors.record_claim(
        conn,
        tenant_id=TENANT,
        show_id=OPEN_SHOW,
        sponsor_id=sponsor["id"],
        claim="Client X sponsor claim.",
        source_url="http://example.test/press",
        verified_date="2026-08-01",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    remaining = platform.publish_blockers(conn, tenant_id=TENANT, show_id=OPEN_SHOW, episode_id=EPISODE)
    assert [row["kind"] for row in remaining] == ["sponsor_claim_unapproved"]
    conn.close()

    for block, origin in (
        ({"currency": "dollars"}, "runtime.yaml"),
        ({"slot_types": []}, "runtime.yaml"),
        ({"slot_types": ["pre", "pre"]}, "runtime.yaml"),
        ({"slot_types": ["preroll"]}, "runtime.yaml"),
        ({"block_publication_on_unfilled_committed_slots": "yes"}, "runtime.yaml"),
        ({"unknown_rule": True}, "runtime.yaml"),
        ("not-an-object", "runtime.yaml"),
    ):
        with pytest.raises(configuration.ConfigurationError):
            configuration._sponsor_policy_block(block, origin)
    assert configuration._sponsor_policy_block(None, "runtime.yaml") == {}
    assert configuration.validate_configuration() == []


# ---------------------------------------------------------------------------
# Authorization, scope, and receipts.
# ---------------------------------------------------------------------------


def test_roles_and_scope_fail_closed(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "authorization.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")
    _declare(conn)

    for call, message in (
        (
            lambda: sponsors.create_sponsor(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                name="Host Brand",
                contact_ref="vault://sponsor/host/contact",
                terms_ref="vault://sponsor/host/terms",
                actor_id="host_fixture",
                actor_role="host",
                now=NOW,
            ),
            "cannot manage sponsors",
        ),
        (
            lambda: sponsors.declare_slots(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                episode_id=EPISODE,
                slots=[],
                actor_id="host_fixture",
                actor_role="host",
                now=NOW,
            ),
            "cannot manage sponsor inventory",
        ),
        (
            lambda: sponsors.assign_slot(
                conn,
                tenant_id=TENANT,
                show_id=SHOW,
                episode_id=EPISODE,
                slot_type="pre",
                sponsor_id=sponsor["id"],
                actor_id="host_fixture",
                actor_role="host",
                now=NOW,
            ),
            "cannot sell sponsor inventory",
        ),
        (
            lambda: sponsors.revenue_report(conn, tenant_id=TENANT, show_id=SHOW, actor_role="stranger"),
            "cannot read sponsor inventory",
        ),
        (
            lambda: sponsors.accounting_csv(conn, tenant_id=TENANT, show_id=SHOW, actor_role=None),
            "cannot read sponsor inventory",
        ),
    ):
        with pytest.raises(sponsors.SponsorError, match=message) as excinfo:
            call()
        assert excinfo.value.status_code == 403

    # A host may read the money without being able to move it.
    assert sponsors.revenue_report(conn, tenant_id=TENANT, show_id=SHOW, actor_role="host")["episodes"]

    # Another show in the same tenant sees none of it.
    assert sponsors.revenue_report(conn, tenant_id=TENANT, show_id=OTHER_SHOW, actor_role="producer")["episodes"] == []
    assert sponsors.list_sponsors(conn, tenant_id=TENANT, show_id=OTHER_SHOW, actor_role="producer") == []
    conn.close()


def test_every_decision_leaves_an_attributable_receipt(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "receipts.sqlite3")
    sponsor = _sponsor(conn, "Athletic Greens")
    _declare(conn)
    _assign(conn, sponsor["id"], "pre")
    sponsors.release_slot(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        episode_id=EPISODE,
        slot_type="pre",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    claim = sponsors.record_claim(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=sponsor["id"],
        claim="Athletic Greens ships to 40 countries.",
        source_url="https://example.test/press/ag",
        verified_date="2026-08-01",
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    sponsors.approve_claim(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        sponsor_id=sponsor["id"],
        claim_id=claim["claim_id"],
        actor_id="owner_fixture",
        actor_role="editorial_owner",
        now=NOW,
    )
    receipts = sponsors.list_receipts(conn, tenant_id=TENANT, show_id=SHOW, actor_role="producer")
    assert {row["event_type"] for row in receipts} == {
        "sponsor.registered",
        "sponsorship.inventory_declared",
        "sponsorship.slot_assigned",
        "sponsorship.slot_released",
        "sponsor.claim_recorded",
        "sponsor.claim_approved",
    }
    assert {row["actor_role"] for row in receipts} == {"producer", "editorial_owner"}
    assert all(row["subject_ref"] for row in receipts)
    scoped = sponsors.list_receipts(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        actor_role="producer",
        episode_id=EPISODE,
    )
    assert {row["event_type"] for row in scoped} == {
        "sponsorship.inventory_declared",
        "sponsorship.slot_assigned",
        "sponsorship.slot_released",
    }
    conn.close()


# ---------------------------------------------------------------------------
# HTTP surface.
# ---------------------------------------------------------------------------


def test_http_surface_is_show_scoped_private_and_complete(tmp_path: Path) -> None:
    database = tmp_path / "http.sqlite3"
    _seed(database)
    with _client(database) as client:
        created = client.post(
            f"/v1/shows/{SHOW}/sponsors",
            headers=_headers(),
            json={
                "name": "Athletic Greens",
                "contact_ref": "vault://sponsor/ag/contact",
                "terms_ref": "vault://sponsor/ag/terms",
                "category": "nutrition",
            },
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store, private"
        sponsor_id = created.json()["id"]

        declared = client.put(
            f"/v1/shows/{SHOW}/ad-slots",
            headers=_headers(),
            json={
                "episode_id": EPISODE,
                "slots": [
                    {"slot_type": "pre", "rate_minor": 150_000, "committed": True},
                    {"slot_type": "mid", "rate_minor": 150_000, "committed": True},
                    {"slot_type": "post", "rate_minor": 150_000, "committed": False},
                ],
            },
        )
        assert declared.status_code == 200, declared.text
        assert [row["slot_type"] for row in declared.json()] == ["mid", "post", "pre"]

        gate = client.get(f"/v1/shows/{SHOW}/episodes/{EPISODE}/publication-gate", headers=_headers())
        assert gate.status_code == 200
        assert gate.json()["publishable"] is False
        assert len(gate.json()["blockers"]) == 2

        for slot_type in ("pre", "mid"):
            assigned = client.post(
                f"/v1/shows/{SHOW}/ad-slots/allocations",
                headers=_headers(),
                json={
                    "episode_id": EPISODE,
                    "slot_type": slot_type,
                    "sponsor_id": sponsor_id,
                },
            )
            assert assigned.status_code == 201, assigned.text
        assert (
            client.get(f"/v1/shows/{SHOW}/episodes/{EPISODE}/publication-gate", headers=_headers()).json()[
                "publishable"
            ]
            is True
        )

        revenue = client.get(f"/v1/shows/{SHOW}/revenue", headers=_headers())
        assert revenue.status_code == 200
        assert revenue.headers["cache-control"] == "no-store, private"
        assert revenue.json()["totals"]["revenue_minor"] == 300_000
        assert revenue.json()["policy"]["source"] == "runtime"

        export = client.get(f"/v1/shows/{SHOW}/revenue", headers=_headers(), params={"format": "csv"})
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert "attachment" in export.headers["content-disposition"]
        assert export.text.splitlines()[0] == "Episode,Sponsor,Slot,Rate,Date"
        assert client.get(f"/v1/shows/{SHOW}/revenue", headers=_headers(), params={"format": "pdf"}).status_code == 422

        claim = client.post(
            f"/v1/shows/{SHOW}/sponsors/{sponsor_id}/claims",
            headers=_headers(),
            json={
                "claim": "Athletic Greens ships to 40 countries.",
                "source_url": "https://example.test/press/ag",
                "verified_date": "2026-08-01",
            },
        )
        assert claim.status_code == 201, claim.text
        claim_id = claim.json()["claim_id"]
        assert (
            client.post(
                f"/v1/shows/{SHOW}/sponsors/{sponsor_id}/claims/{claim_id}/approval",
                headers=_headers(),
                json={"approved": True},
            ).status_code
            == 403
        )
        approved = client.post(
            f"/v1/shows/{SHOW}/sponsors/{sponsor_id}/claims/{claim_id}/approval",
            headers=_headers(OWNER_TOKEN),
            json={"approved": True},
        )
        assert approved.status_code == 200
        assert approved.json()["approved_for_external_use"] is True
        assert client.get(f"/v1/shows/{SHOW}/sponsors", headers=_headers()).json()[0]["unapproved_claim_count"] == 0

        released = client.post(
            f"/v1/shows/{SHOW}/ad-slots/releases",
            headers=_headers(),
            json={"episode_id": EPISODE, "slot_type": "mid"},
        )
        assert released.status_code == 200 and released.json()["status"] == "available"

        receipts = client.get(f"/v1/shows/{SHOW}/sponsorship-receipts", headers=_headers())
        assert receipts.status_code == 200
        assert "sponsor.claim_approved" in {row["event_type"] for row in receipts.json()}

        # A malformed payload is rejected before any lookup.
        assert client.post(f"/v1/shows/{SHOW}/sponsors", headers=_headers(), json={"name": "x"}).status_code == 422
        assert (
            client.post(
                f"/v1/shows/{SHOW}/ad-slots/allocations",
                headers=_headers(),
                json={"episode_id": EPISODE, "slot_type": "pre", "sponsor_id": "missing"},
            ).status_code
            == 404
        )
        # A host reads the revenue but cannot register a sponsor.
        assert client.get(f"/v1/shows/{SHOW}/revenue", headers=_headers(HOST_TOKEN)).status_code == 200
        assert (
            client.post(
                f"/v1/shows/{SHOW}/sponsors",
                headers=_headers(HOST_TOKEN),
                json={
                    "name": "Host Brand",
                    "contact_ref": "vault://sponsor/host/contact",
                    "terms_ref": "vault://sponsor/host/terms",
                },
            ).status_code
            == 403
        )

    with _client(database, bound_show=SHOW) as bound:
        for path, method, payload in (
            (f"/v1/shows/{OTHER_SHOW}/revenue", "get", None),
            (f"/v1/shows/{OTHER_SHOW}/ad-slots", "get", None),
            (f"/v1/shows/{OTHER_SHOW}/sponsors", "get", None),
            (f"/v1/shows/{OTHER_SHOW}/sponsorship-receipts", "get", None),
            (f"/v1/shows/{OTHER_SHOW}/episodes/{EPISODE}/publication-gate", "get", None),
            (
                f"/v1/shows/{OTHER_SHOW}/ad-slots",
                "put",
                {"episode_id": EPISODE, "slots": []},
            ),
            (
                f"/v1/shows/{OTHER_SHOW}/ad-slots/allocations",
                "post",
                {"episode_id": EPISODE, "slot_type": "pre", "sponsor_id": "x"},
            ),
            (
                f"/v1/shows/{OTHER_SHOW}/ad-slots/releases",
                "post",
                {"episode_id": EPISODE, "slot_type": "pre"},
            ),
        ):
            call = getattr(bound, method)
            response = (
                call(path, headers=_headers(session_show=None), json=payload)
                if payload is not None
                else call(path, headers=_headers(session_show=None))
            )
            assert response.status_code == 403, (path, response.status_code)


# ---------------------------------------------------------------------------
# Schema, contract, UI, and documentation surfaces.
# ---------------------------------------------------------------------------


def test_schema_contract_dashboard_and_documentation_are_complete(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "schema.sqlite3")
    ledger = {row["version"]: row["name"] for row in migrations.applied_migrations(conn)}
    assert ledger[16] == "sponsor_inventory_and_claims"
    assert migrations.LATEST_VERSION >= 16
    tables = store.table_names(conn)
    assert {"sponsor_slots", "sponsor_claims", "sponsorship_receipts"} <= tables
    slot_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(sponsor_slots)")}
    assert {"episode_id", "slot_type", "rate_minor", "rate_currency", "committed"} <= slot_columns
    sponsorship_columns = {row["name"] for row in store.fetch_all(conn, "PRAGMA table_info(sponsorships)")}
    assert {"slot_id", "sold_at", "recorded_by", "recorded_by_role"} <= sponsorship_columns
    indexes = {row["name"] for row in store.fetch_all(conn, "PRAGMA index_list(sponsorships)")}
    assert {"ux_sponsorship_slot", "ix_sponsorship_revenue"} <= indexes
    conn.close()

    schema = json.loads((ROOT / "spec/sponsor.schema.json").read_text(encoding="utf-8"))
    slots = schema["properties"]["slots"]["items"]
    assert set(slots["required"]) == {"type", "rate", "sold"}
    assert slots["properties"]["type"]["enum"] == ["pre", "mid", "post"]
    assert slots["properties"]["status"]["enum"] == ["available", "reserved", "sold"]
    assert "committed" in slots["properties"] and "episode_range" in slots["properties"]
    assert schema["properties"]["contact"]["pattern"].startswith("^[A-Za-z0-9]")
    assert schema["properties"]["terms"]["pattern"].startswith("^[A-Za-z0-9]")
    claims = schema["properties"]["claims"]["items"]
    assert set(claims["required"]) == {
        "claim",
        "source_url",
        "verified_date",
        "approved_for_external_use",
    }
    assert schema["properties"]["status"]["enum"] == ["active", "paused", "ended"]

    for relative in (
        "spec/sponsor.schema.json",
        "config/domain_kernel.yaml",
        "config/runtime.yaml",
        "config/shows/client-x.yaml",
        "dashboard/index.html",
        "dashboard/assets/api.js",
        "dashboard/assets/capabilities.mjs",
        "dashboard/assets/guide.json",
        "dashboard/assets/partnership.js",
        "dashboard/assets/partnership-workspace.html",
        "dashboard/assets/partnership-workspace.js",
        "dashboard/assets/styles.css",
    ):
        assert (ROOT / relative).read_bytes() == (ROOT / "hospes/resources" / relative).read_bytes(), relative

    domain = (ROOT / "config/domain_kernel.yaml").read_text(encoding="utf-8")
    assert "sponsor_inventory_contract:" in domain
    for marker in (
        "block_publication_on_unfilled_committed_slots",
        "require_approved_claims_before_publication",
        "export_columns: [Episode, Sponsor, Slot, Rate, Date]",
        "One episode ad slot is sellable to exactly one sponsor",
        "opaque custody references to their external owner",
    ):
        assert marker in domain, marker
    for entity in ("SponsorSlot", "SponsorClaim", "SponsorshipReceipt"):
        assert f"- {entity}\n" in domain

    html = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "dashboard/index.html",
            "dashboard/assets/partnership-workspace.html",
        )
    )
    javascript = (ROOT / "dashboard/assets/partnership-workspace.js").read_text(encoding="utf-8")
    api_javascript = (ROOT / "dashboard/assets/api.js").read_text(encoding="utf-8")
    capabilities = (ROOT / "dashboard/assets/capabilities.mjs").read_text(encoding="utf-8")
    assert 'data-view="revenue"' in html
    assert "import { VIEWS, viewTitle } from './views.mjs';" in javascript
    assert "for (const name of VIEWS)" in javascript
    views_registry = (ROOT / "dashboard/assets/views.mjs").read_text(encoding="utf-8")
    assert "'revenue'" in views_registry
    assert 'id="revenue-view"' in html
    assert 'id="revenue-table"' in html
    assert 'id="sponsor-dialog"' in html
    assert 'id="btn-add-sponsor"' in html
    assert 'id="btn-export-revenue"' in html
    assert 'id="slot-inventory-rows"' in html
    for column in ("Episode", "Sponsor", "Slots sold/total", "Revenue"):
        assert f">{column}</th>" in html, column
    # Every element the Revenue code addresses must exist in the shipped markup:
    # a missing id is a silent runtime failure no Python test would otherwise see.
    for element_id in (
        "revenue-view",
        "revenue-table",
        "revenue-rows",
        "revenue-gate",
        "revenue-episode",
        "revenue-policy-note",
        "rev-slots-sold",
        "rev-slots-available",
        "rev-committed-unfilled",
        "rev-total",
        "slot-inventory-rows",
        "sponsor-list",
        "sponsor-dialog",
        "sponsor-form",
        "sponsorship-form",
        "sponsorship-sponsor",
        "sponsor-claim-form",
        "claim-sponsor",
        "ad-slot-form",
        "btn-revenue-refresh",
        "btn-add-sponsor",
        "btn-cancel-sponsor",
        "btn-export-revenue",
    ):
        assert f'id="{element_id}"' in html, element_id
        # revenue-view is reached through the generic view loop and revenue-table
        # is a guide anchor; every other id is addressed by an explicit selector.
        if element_id not in {"revenue-view", "revenue-table"}:
            assert f"'#{element_id}'" in javascript, element_id
    assert "escapeHTML(sponsor.sponsor_name" in javascript
    assert "escapeHTML(claim.claim)" in javascript
    assert "hospes-revenue.csv" in javascript
    assert "exportRevenueCsv" in api_javascript
    assert "saveSlotAssignment" in api_javascript
    assert "releaseSlotAssignment" in api_javascript
    assert "canViewRevenue" in capabilities
    assert "canManageSponsors" in capabilities
    assert "canApproveSponsorClaims" in capabilities

    guide = json.loads((ROOT / "dashboard/assets/guide.json").read_text(encoding="utf-8"))
    assert {"canViewRevenue", "canManageSponsors", "canApproveSponsorClaims"} <= set(guide["capabilities"])
    assert "revenue" in {beat["view"] for beat in guide["beats"]}

    documentation = (ROOT / "docs/sponsor-inventory.md").read_text(encoding="utf-8")
    for phrase in (
        "Sold versus available",
        "minor currency units",
        "Episode, Sponsor, Slot, Rate, Date",
        "block_publication_on_unfilled_committed_slots",
        "opaque custody references",
        "Cache-Control: no-store, private",
        "sponsor_claim_unapproved",
    ):
        assert phrase in documentation, phrase
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/sponsor-inventory.md" in readme
