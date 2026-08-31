"""Issue #33 predicate: the self-serve guest portal.

Close condition: the isolated portal boundary, one-time fragment exchange,
secure short session, noindex intake/consent/three-date UI, operator status,
and expiry tests pass; links remain human-transmitted.

The load-bearing half is what a *browser* and a *replay* actually get.  A link
whose token sits in the query string is logged by every proxy it crosses; a
link that can be opened twice is not one-time however the copy describes it;
and a session that outlives the tab is a durable credential wearing a
short-lived name.  These tests therefore assert the fragment, the spent row,
the revocation, and the expiry — not merely that the module exposes functions
with those words in their names.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from hospes import branding, completion_registry, configuration, encryption, guest_portal, migrations, platform, store

pytest.importorskip("fastapi", reason="the operator surface requires the optional 'api' extra")
from fastapi.testclient import TestClient  # noqa: E402

from hospes import api  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TENANT = "hospes"
SHOW = "flagship"
GUEST = "synthetic-guest"
PORTAL_SECRET = "issue33-portal-secret-value"  # allow-secret: synthetic test fixture
OPERATOR_TOKEN = "issue33-operator-token-0123456789"  # allow-secret: synthetic test fixture
MASTER_REF = "credential://hospes/master-key"
MASTER_KEY = bytes(range(32))
BASE_URL = "https://guests.example.test"
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
OFFERED = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
CHOSEN = ["2026-09-01", "2026-09-03", "2026-09-04"]

CARE_PROFILE = {
    "name_pronunciation": "ah-REE",
    "pronouns": "they/them",
    "dietary_restrictions": ["vegan"],
    "allergies": ["walnuts"],
    "accessibility_needs": ["step-free access"],
    "topics_off_limits": ["the 2019 lawsuit"],
    "preferred_beverage": "black coffee",
    "notes": "Prefers a short warm-up before recording.",
}


class _SessionShowASGI:
    """Project the operator's active show into scope, as the operator shell does."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-session-show":
                    scope["hospes.operator_show"] = value.decode("utf-8")
        await self.app(scope, receive, send)


def _vault() -> encryption.FieldVault:
    provider = encryption.StaticMasterKeyProvider({MASTER_REF: MASTER_KEY})
    return encryption.FieldVault(encryption.TenantKeyManager(provider, master_key_ref=MASTER_REF))


def _database(tmp_path: Path, name: str = "issue33.sqlite3") -> Any:
    conn = store.connect(tmp_path / name)
    platform.register_show(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        label="Flagship Show",
        config_ref=f"config/shows/{SHOW}.yaml",
    )
    return conn


def _link(conn: Any, *, offered: list[str] | None = None, ttl_days: int = 7, now: datetime = NOW) -> dict[str, Any]:
    return guest_portal.create_portal_token(
        conn,
        tenant_id=TENANT,
        show_id=SHOW,
        guest_id=GUEST,
        secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
        base_url=BASE_URL,
        ttl_days=ttl_days,
        enabled=True,
        offered_dates=OFFERED if offered is None else offered,
        created_by="ari_fixture",
        created_by_role="producer",
        now=now,
    )


def _token(link: dict[str, Any]) -> str:
    return link["url"].split("#token=", 1)[1]  # allow-secret: synthetic one-time token


def _complete(
    conn: Any, link: dict[str, Any], *, now: datetime = NOW, dates: list[str] | None = None
) -> dict[str, Any]:
    opened = guest_portal.open_session(
        conn, token=_token(link), secret=PORTAL_SECRET, now=now  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    return guest_portal.submit_intake(
        conn,
        _vault(),
        session=opened["session"],
        care_profile=CARE_PROFILE,
        consent={"accepted": True, "version": guest_portal.CONSENT_VERSION},
        preferred_dates=CHOSEN if dates is None else dates,
        now=now,
    )


def test_the_portal_is_a_separate_application_that_is_disabled_by_default(tmp_path: Path) -> None:
    conn = _database(tmp_path)

    # The doctrine this module implements is declared, not merely coded.
    kernel = yaml.safe_load((ROOT / "config" / "domain_kernel.yaml").read_text(encoding="utf-8"))
    assert "Guest portal access is disabled by default" in json.dumps(kernel)

    # Minting is refused before a show opts in, and every route 404s.
    with pytest.raises(guest_portal.PortalError, match="disabled by show configuration") as refused:
        guest_portal.create_portal_token(
            conn,
            tenant_id=TENANT,
            show_id=SHOW,
            guest_id=GUEST,
            secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
            base_url=BASE_URL,
        )
    assert refused.value.status_code == 403

    disabled = guest_portal.create_app(
        conn=conn, secret=PORTAL_SECRET, enabled=False  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    with TestClient(disabled) as client:
        for route in ("/guest/", "/guest/app.js", "/guest/styles.css", branding.PORTAL_STYLESHEET_PATH):
            assert client.get(route).status_code == 404, route
        assert client.post("/guest/session", json={"token": "anything"}).status_code == 404
        assert client.post("/guest/intake", json={"session": "anything"}).status_code == 404

    # The portal is its own app: the operator surface never carries a guest route.
    operator_app = api.create_app(
        db_path=str(tmp_path / "issue33.sqlite3"), auth_token=OPERATOR_TOKEN  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    assert not [route for route in operator_app.routes if str(getattr(route, "path", "")).startswith("/guest")]
    conn.close()


def test_the_one_time_fragment_exchange_spends_the_link(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    link = _link(conn)

    # The token rides in the fragment: no proxy, access log, or Referer sees it.
    base, _, fragment = link["url"].partition("#")
    assert base == f"{BASE_URL}/guest/"
    assert "?" not in link["url"] and fragment.startswith("token=")

    # Only the digest is durable, and the token never appears in the row.
    row = store.fetch_one(conn, "SELECT * FROM portal_tokens WHERE id = ?", (link["id"],))
    assert len(str(row["token_digest"])) == 64
    assert _token(link) not in json.dumps(row) and row["used_at"] is None
    assert row["created_by"] == "ari_fixture" and row["created_by_role"] == "producer"

    opened = guest_portal.open_session(
        conn, token=_token(link), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    assert opened["guest_id"] == GUEST
    assert opened["offered_dates"] == OFFERED
    assert opened["preferred_date_count"] == guest_portal.PREFERRED_DATE_COUNT
    assert [clause["consent_type"] for clause in opened["consent_clauses"]] == [
        key for key, _ in guest_portal.CONSENT_CLAUSES
    ]

    # A replayed fragment finds the row already taken — before any data is typed.
    with pytest.raises(guest_portal.PortalError, match="already been opened") as replayed:
        guest_portal.open_session(
            conn, token=_token(link), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
        )  # allow-secret: synthetic test fixture
    assert replayed.value.status_code == 401

    # A forged or re-signed token is refused outright.
    for bad in (_token(link)[:-1] + ("0" if _token(link)[-1] != "0" else "1"), "not-a-token", ""):
        with pytest.raises(guest_portal.PortalError):
            guest_portal.open_session(
                conn, token=bad, secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
            )  # allow-secret: synthetic test fixture
    conn.close()


def test_the_session_is_short_lived_and_dies_on_submission_and_on_expiry(tmp_path: Path) -> None:
    conn = _database(tmp_path)

    # A session may never be asked to outlive its ceiling.
    with pytest.raises(guest_portal.PortalError, match="session ttl"):
        guest_portal.open_session(
            conn,
            token=_token(_link(conn)),  # allow-secret: runtime signing material, not a value
            secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
            ttl_minutes=guest_portal.SESSION_TTL_MINUTES + 1,
            now=NOW,
        )

    # It is stored as a digest only, and it is revoked the moment intake lands.
    link = _link(conn)
    opened = guest_portal.open_session(
        conn, token=_token(link), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    session_row = store.fetch_one(conn, "SELECT * FROM portal_sessions WHERE portal_token_id = ?", (link["id"],))
    assert opened["session"] not in json.dumps(session_row)
    assert len(str(session_row["session_digest"])) == 64
    assert session_row["revoked_at"] is None
    expires = datetime.fromisoformat(str(opened["expires_at"]))
    assert expires - NOW <= timedelta(minutes=guest_portal.SESSION_TTL_MINUTES)

    guest_portal.submit_intake(
        conn,
        _vault(),
        session=opened["session"],
        care_profile=CARE_PROFILE,
        consent={"accepted": True, "version": guest_portal.CONSENT_VERSION},
        preferred_dates=CHOSEN,
        now=NOW,
    )
    assert store.fetch_one(conn, "SELECT revoked_at FROM portal_sessions WHERE id = ?", (session_row["id"],))[
        "revoked_at"
    ]
    with pytest.raises(guest_portal.PortalError, match="already closed"):
        guest_portal.submit_intake(
            conn,
            _vault(),
            session=opened["session"],
            care_profile=CARE_PROFILE,
            consent={"accepted": True, "version": guest_portal.CONSENT_VERSION},
            preferred_dates=CHOSEN,
            now=NOW,
        )

    # An untouched session simply expires.
    stale = guest_portal.open_session(
        conn, token=_token(_link(conn)), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    with pytest.raises(guest_portal.PortalError, match="expired") as timed_out:
        guest_portal.submit_intake(
            conn,
            _vault(),
            session=stale["session"],
            care_profile=CARE_PROFILE,
            consent={"accepted": True, "version": guest_portal.CONSENT_VERSION},
            preferred_dates=CHOSEN,
            now=NOW + timedelta(minutes=guest_portal.SESSION_TTL_MINUTES + 1),
        )
    assert timed_out.value.status_code == 401
    conn.close()


def test_an_expired_link_cannot_be_opened_at_all(tmp_path: Path) -> None:
    conn = _database(tmp_path)

    # A link lives at most a week, and the ceiling is enforced at mint time.
    for ttl in (0, guest_portal.MAX_LINK_TTL_DAYS + 1):
        with pytest.raises(guest_portal.PortalError, match="ttl must be between"):
            _link(conn, ttl_days=ttl)
    assert guest_portal.DEFAULT_LINK_TTL_DAYS == 7

    link = _link(conn, ttl_days=7)
    assert datetime.fromisoformat(str(link["expires_at"])) - NOW == timedelta(days=7)

    # The signed expiry is checked before the database is ever consulted, so an
    # expired link is refused even though its row is still present and unused.
    expired = _link(conn, ttl_days=1, now=NOW - timedelta(days=2))
    assert store.fetch_one(conn, "SELECT used_at FROM portal_tokens WHERE id = ?", (expired["id"],))["used_at"] is None
    with pytest.raises(guest_portal.PortalError, match="expired") as stale:
        guest_portal.open_session(
            conn, token=_token(expired), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
        )  # allow-secret: synthetic test fixture
    assert stale.value.status_code == 401
    conn.close()


def test_the_intake_consent_and_three_date_surface_is_noindex_and_self_contained(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    portal = guest_portal.create_app(
        conn=conn, secret=PORTAL_SECRET, enabled=True, vault=_vault()  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    with TestClient(portal) as client:
        landing = client.get("/guest/")
        assert landing.status_code == 200
        assert landing.headers["x-robots-tag"] == "noindex, nofollow"
        assert landing.headers["cache-control"] == "no-store"

        policy = landing.headers["content-security-policy"]
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "form-action 'self'"):
            assert directive in policy
        assert "'unsafe-inline'" not in policy

        # The document itself repeats the instruction a crawler is given.
        assert '<meta name="robots" content="noindex,nofollow">' in landing.text
        # All three steps are present, and each is a real form control.
        for marker in (
            'id="intake"',
            'id="consent-clauses"',
            'id="consent-accept"',
            'id="date-choices"',
            'id="submit"',
        ):
            assert marker in landing.text, marker
        for field in guest_portal.CARE_PROFILE_TEXT_FIELDS + guest_portal.CARE_PROFILE_LIST_FIELDS:
            assert f'id="{field.replace("_", "-")}"' in landing.text, field

        # Nothing is inline, so the policy above needs no hash exemption.
        assert "<style" not in landing.text and "<script>" not in landing.text
        assert landing.text.count("brand-mark") == 1

        for route, content_type in (
            ("/guest/app.js", "application/javascript"),
            ("/guest/styles.css", "text/css"),
            (branding.PORTAL_STYLESHEET_PATH, "text/css"),
        ):
            served = client.get(route)
            assert served.status_code == 200, route
            assert served.headers["content-type"].startswith(content_type)

        script = client.get("/guest/app.js").text
        # The fragment is read and then erased; the session never becomes durable.
        assert "location.hash" in script and "history.replaceState" in script
        assert "localStorage" not in script and "sessionStorage" not in script

        # The shipped tree and the packaged tree are the same bytes.
        for name in ("index.html", "app.js", "styles.css"):
            assert (ROOT / "dashboard" / "guest" / name).read_bytes() == (
                ROOT / "hospes" / "resources" / "dashboard" / "guest" / name
            ).read_bytes(), name
    conn.close()


def test_a_completed_intake_seals_what_the_guest_typed_and_writes_three_receipts(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    link = _link(conn)
    intake = _complete(conn, link)

    assert intake["receipts"] == ["guest_intake.completed", "consent.signed", "availability.provided"]
    receipts = {
        str(row["event_type"]): row
        for row in store.fetch_all(conn, "SELECT * FROM portal_receipts WHERE portal_intake_id = ?", (intake["id"],))
    }
    assert set(receipts) == {"guest_intake.completed", "consent.signed", "availability.provided"}
    assert all(row["subject_ref"] == f"portal-intake://{intake['id']}" for row in receipts.values())
    assert all(row["guest_id"] == GUEST and row["show_id"] == SHOW for row in receipts.values())

    # Every emitted event is declared vocabulary.
    declared = {
        event["name"] for event in json.loads((ROOT / "spec" / "events.json").read_text(encoding="utf-8"))["events"]
    }
    assert set(receipts) <= declared

    # The intake receipt names which fields were given, never their values.
    assert receipts["guest_intake.completed"]["details"]["fields"] == sorted(CARE_PROFILE)
    # The consent receipt pins the exact wording that was agreed to.
    consent_details = receipts["consent.signed"]["details"]
    assert consent_details["checksum"] == guest_portal.consent_checksum()
    assert consent_details["promotion_required"] is False
    assert consent_details["consent_types"] == [key for key, _ in guest_portal.CONSENT_CLAUSES]
    # The dates are legible: the production must act on them.
    assert receipts["availability.provided"]["details"]["preferred_dates"] == CHOSEN
    assert receipts["availability.provided"]["details"]["from_offered_slate"] is True

    # What the guest typed reaches the row only as an opaque handle, and the
    # plaintext appears nowhere in the database.
    assert intake["intake_ref"].startswith("private-field://")
    assert intake["consent_ref"].startswith("private-field://")
    assert intake["availability_ref"] == f"portal-availability://{intake['id']}"
    dump = json.dumps(
        [
            store.fetch_all(conn, "SELECT * FROM portal_intakes"),
            store.fetch_all(conn, "SELECT * FROM portal_receipts"),
            store.fetch_all(conn, "SELECT * FROM private_field_values"),
        ]
    )
    for secret_value in ("walnuts", "the 2019 lawsuit", "black coffee", "ah-REE"):
        assert secret_value not in dump, secret_value

    # An authorized operator can still unseal it.
    scope = encryption.PrivateFieldScope(
        tenant_id=TENANT,
        show_id=SHOW,
        category="correspondence",
        owner_table="portal_intakes",
        owner_record_id=intake["id"],
        field_name="intake",
    )
    assert _vault().reveal_json(conn, intake["intake_ref"], scope, actor_role="producer") == CARE_PROFILE

    # The three dates are ranked rows, not an opaque blob.
    choices = store.fetch_all(
        conn,
        "SELECT rank, preferred_date FROM portal_date_choices WHERE portal_intake_id = ? ORDER BY rank",
        (intake["id"],),
    )
    assert [(row["rank"], row["preferred_date"]) for row in choices] == list(enumerate(CHOSEN, start=1))
    conn.close()


def test_the_date_step_takes_exactly_three_dates_from_the_offered_slate(tmp_path: Path) -> None:
    conn = _database(tmp_path)

    for dates, message in (
        (["2026-09-01", "2026-09-02"], "exactly 3"),
        (["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"], "exactly 3"),
        (["2026-09-01", "2026-09-01", "2026-09-02"], "three different"),
        (["2026-09-01", "2026-09-02", "2026-12-25"], "chosen from the dates"),
        (["2026-09-01", "2026-09-02", "not-a-date"], "YYYY-MM-DD"),
        (["2026-09-01", "2026-09-02", "2026-02-30"], "real calendar date"),
    ):
        with pytest.raises(guest_portal.PortalError, match=message):
            _complete(conn, _link(conn), dates=dates)

    # A production that has not published availability still gets a working
    # portal: the guest proposes three instead of choosing from a slate.
    open_slate = _complete(conn, _link(conn, offered=[]), dates=["2026-10-01", "2026-10-02", "2026-10-03"])
    receipt = store.fetch_one(
        conn,
        "SELECT details FROM portal_receipts WHERE portal_intake_id = ? AND event_type = 'availability.provided'",
        (open_slate["id"],),
    )
    assert receipt["details"]["from_offered_slate"] is False
    conn.close()


def test_consent_is_affirmative_bounded_and_never_requires_promotion(tmp_path: Path) -> None:
    conn = _database(tmp_path)

    # Silence, a false tick, and a stale version are all refusals.
    for consent, message in (
        ({}, "must be accepted"),
        ({"accepted": False}, "must be accepted"),
        ({"accepted": "yes"}, "must be accepted"),
        ({"accepted": True, "version": "0.9"}, "stale"),
    ):
        opened = guest_portal.open_session(
            conn, token=_token(_link(conn)), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
        )  # allow-secret: synthetic test fixture
        with pytest.raises(guest_portal.PortalError, match=message):
            guest_portal.submit_intake(
                conn,
                _vault(),
                session=opened["session"],
                care_profile=CARE_PROFILE,
                consent=consent,
                preferred_dates=CHOSEN,
                now=NOW,
            )

    # The clauses the guest reads are the consent types the schema declares, and
    # promotion is never one of them.
    schema = json.loads((ROOT / "spec" / "consent.schema.json").read_text(encoding="utf-8"))
    declared = set(schema["properties"]["consent_type"]["enum"])
    assert {key for key, _ in guest_portal.CONSENT_CLAUSES} <= declared
    assert schema["properties"]["promotion_required"]["const"] is False

    intake = _complete(conn, _link(conn))
    record = _vault().reveal_json(
        conn,
        intake["consent_ref"],
        encryption.PrivateFieldScope(
            tenant_id=TENANT,
            show_id=SHOW,
            category="consent",
            owner_table="portal_intakes",
            owner_record_id=intake["id"],
            field_name="consent",
        ),
        actor_role="producer",
    )
    assert record["promotion_required"] is False and record["status"] == "granted"
    assert record["clauses"] == guest_portal.consent_clauses()

    # An unknown or oversized answer never reaches custody.
    opened = guest_portal.open_session(
        conn, token=_token(_link(conn)), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    for profile, message in (
        ({"favourite_colour": "green"}, "does not accept"),
        ({"notes": "x" * 501}, "characters or fewer"),
        ({"allergies": ["x" * 201]}, "characters or fewer"),
        ({"allergies": ["nuts"] * 21}, "exceed"),
        ("not-an-object", "must be an object"),
    ):
        with pytest.raises(guest_portal.PortalError, match=message):
            guest_portal.submit_intake(
                conn,
                _vault(),
                session=opened["session"],
                care_profile=profile,
                consent={"accepted": True, "version": guest_portal.CONSENT_VERSION},
                preferred_dates=CHOSEN,
                now=NOW,
            )
    conn.close()


def test_the_portal_refuses_private_answers_it_cannot_encrypt(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    opened = guest_portal.open_session(
        conn, token=_token(_link(conn)), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    with pytest.raises(guest_portal.PortalError, match="custody is not configured") as unconfigured:
        guest_portal.submit_intake(
            conn,
            None,
            session=opened["session"],
            care_profile=CARE_PROFILE,
            consent={"accepted": True, "version": guest_portal.CONSENT_VERSION},
            preferred_dates=CHOSEN,
            now=NOW,
        )
    assert unconfigured.value.status_code == 503
    assert store.fetch_all(conn, "SELECT id FROM portal_intakes") == []
    conn.close()


def test_the_operator_sees_portal_state_without_any_private_value(tmp_path: Path) -> None:
    conn = _database(tmp_path)

    assert guest_portal.portal_status(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST)["state"] == "not_sent"
    link = _link(conn)
    sent = guest_portal.portal_status(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST, now=NOW)
    assert sent["state"] == "sent" and sent["offered_dates"] == OFFERED and sent["preferred_dates"] == []

    guest_portal.open_session(
        conn, token=_token(link), secret=PORTAL_SECRET, now=NOW  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    assert (
        guest_portal.portal_status(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST, now=NOW)["state"] == "opened"
    )

    later = NOW + timedelta(minutes=1)
    _complete(conn, _link(conn, now=later), now=later)
    completed = guest_portal.portal_status(conn, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST, now=NOW)
    assert completed["state"] == "completed"
    assert completed["preferred_dates"] == CHOSEN
    assert completed["receipts"] == sorted(["guest_intake.completed", "consent.signed", "availability.provided"])
    # The badge costs no unsealing: no private value is in the payload at all.
    for secret_value in ("walnuts", "black coffee", "private-field://"):
        assert secret_value not in json.dumps(completed), secret_value

    # An unopened link that outlives its expiry reads as expired, not completed.
    other = store.connect(tmp_path / "expiry.sqlite3")
    platform.register_show(
        other, tenant_id=TENANT, show_id=SHOW, label="Flagship Show", config_ref=f"config/shows/{SHOW}.yaml"
    )
    guest_portal.create_portal_token(
        other,
        tenant_id=TENANT,
        show_id=SHOW,
        guest_id=GUEST,
        secret=PORTAL_SECRET,  # allow-secret: runtime signing material, not a value
        base_url=BASE_URL,
        ttl_days=1,
        enabled=True,
        now=NOW,
    )  # allow-secret: synthetic test fixture
    assert (
        guest_portal.portal_status(other, tenant_id=TENANT, show_id=SHOW, guest_id=GUEST, now=NOW + timedelta(days=2))[
            "state"
        ]
        == "expired"
    )
    other.close()

    # The show-wide read is one request for a whole slate.
    everyone = guest_portal.show_portal_status(conn, tenant_id=TENANT, show_id=SHOW, now=NOW)
    assert [row["guest_id"] for row in everyone] == [GUEST]
    conn.close()


def test_the_link_route_is_booked_only_and_the_portal_never_transmits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _database(tmp_path, "operator.sqlite3")
    timestamp = NOW.isoformat()
    for opportunity_id, status, source_key in (
        ("opp-booked", "BOOKED", GUEST),
        ("opp-early", "APPROVED", "other-guest"),
    ):
        store.insert(
            conn,
            "appearance_opportunities",
            {
                "id": opportunity_id,
                "tenant_id": TENANT,
                "network_id": "network-a",
                "show_id": SHOW,
                "guest_name": "Synthetic Guest",
                "why_guest": "A synthetic candidate exercising the portal link route.",
                "why_now": "The recording is confirmed and intake is outstanding.",
                "proposed_artifact": "An episode",
                "relationship_class": "C2",
                "status": status,
                "source_key": source_key,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
    conn.commit()
    conn.close()

    real_show = configuration.load_show(SHOW)
    monkeypatch.setattr(
        configuration,
        "load_show",
        lambda show_id: (
            real_show.__class__(**{**real_show.__dict__, "guest_interaction_mode": "portal"})
            if show_id == SHOW
            else configuration.load_show(show_id)
        ),
    )
    monkeypatch.setenv("HOSPES_PORTAL_SECRET", PORTAL_SECRET)  # allow-secret: synthetic test fixture
    monkeypatch.setenv("HOSPES_PORTAL_BASE_URL", BASE_URL)

    from conftest import synthetic_bearer_authenticator

    application = api.create_app(
        db_path=str(tmp_path / "operator.sqlite3"),
        runtime_kind="synthetic_test",
        csrf_required=False,
        field_vault=_vault(),
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {OPERATOR_TOKEN: ("ari_fixture", "producer", TENANT)}
        ),
    )
    headers = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "X-Session-Show": SHOW}  # allow-secret: synthetic test fixture
    with TestClient(_SessionShowASGI(application)) as client:
        # The dashboard learns whether the button may render at all.
        context = client.get("/v1/operator-context", headers=headers).json()
        assert context["guest_portal_configured"] is True

        # Before BOOKED there is no date to plan around, so there is no link.
        early = client.post("/v1/opportunities/opp-early/portal-link", headers=headers, json={})
        assert early.status_code == 422 and "BOOKED" in early.json()["detail"]

        minted = client.post(
            "/v1/opportunities/opp-booked/portal-link",
            headers=headers,
            json={"offered_dates": OFFERED},
        )
        assert minted.status_code == 201
        body = minted.json()
        assert body["url"].startswith(f"{BASE_URL}/guest/#token=")
        assert body["offered_dates"] == OFFERED
        assert minted.headers["Cache-Control"].startswith("no-store")

        status = client.get("/v1/portal-status", headers=headers).json()
        assert [row["guest_id"] for row in status] == [GUEST]
        assert status[0]["state"] == "sent"

        # There is no route that sends the link anywhere: a human carries it.
        paths = {str(getattr(route, "path", "")) for route in application.routes}
        assert not any("send" in path or "deliver" in path for path in paths)

    # Without the signing material the route is a visible 503, never a silent
    # unsigned link.
    monkeypatch.delenv("HOSPES_PORTAL_SECRET")
    unsigned = api.create_app(
        db_path=str(tmp_path / "operator.sqlite3"),
        runtime_kind="synthetic_test",
        csrf_required=False,
        field_vault=_vault(),
        _test_bearer_authenticator=synthetic_bearer_authenticator(
            {OPERATOR_TOKEN: ("ari_fixture", "producer", TENANT)}
        ),
    )
    with TestClient(_SessionShowASGI(unsigned)) as client:
        assert client.get("/v1/operator-context", headers=headers).json()["guest_portal_configured"] is False
        assert client.post("/v1/opportunities/opp-booked/portal-link", headers=headers, json={}).status_code == 503


def test_the_http_surface_carries_the_whole_guest_journey(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    link = _link(conn)
    portal = guest_portal.create_app(
        conn=conn, secret=PORTAL_SECRET, enabled=True, vault=_vault()  # allow-secret: runtime signing material, not a value
    )  # allow-secret: synthetic test fixture
    with TestClient(portal) as client:
        opened = client.post("/guest/session", json={"token": _token(link)})
        assert opened.status_code == 200
        session = opened.json()["session"]
        assert opened.json()["offered_dates"] == OFFERED

        # The link is spent by the exchange itself.
        assert client.post("/guest/session", json={"token": _token(link)}).status_code == 401
        # A malformed body is a bounded refusal, not a stack trace.
        assert client.post("/guest/session", json=["not", "an", "object"]).status_code == 422

        submitted = client.post(
            "/guest/intake",
            json={
                "session": session,
                "care_profile": CARE_PROFILE,
                "consent": {"accepted": True, "version": guest_portal.CONSENT_VERSION},
                "preferred_dates": CHOSEN,
            },
        )
        assert submitted.status_code == 200
        assert submitted.json()["receipts"] == ["guest_intake.completed", "consent.signed", "availability.provided"]
        # Replaying the submission finds a closed session.
        assert (
            client.post(
                "/guest/intake",
                json={
                    "session": session,
                    "care_profile": {},
                    "consent": {"accepted": True, "version": guest_portal.CONSENT_VERSION},
                    "preferred_dates": CHOSEN,
                },
            ).status_code
            == 401
        )
    conn.close()


def test_the_dashboard_gates_the_button_on_a_configured_portal() -> None:
    capabilities = (ROOT / "dashboard" / "assets" / "capabilities.mjs").read_text(encoding="utf-8")
    assert "canSendPortalLink" in capabilities
    assert "context.guest_portal_configured === true" in capabilities

    guide = json.loads((ROOT / "dashboard" / "assets" / "guide.json").read_text(encoding="utf-8"))
    entry = guide["capabilities"]["canSendPortalLink"]
    assert all(str(entry.get(field, "")).strip() for field in ("label", "what", "why", "requires"))

    app_js = (ROOT / "dashboard" / "assets" / "app.js").read_text(encoding="utf-8")
    # The button is offered only on a booked candidate, and the badge the issue
    # asks for is rendered from the status read.
    assert "'BOOKED'" in app_js and 'data-action="portal-link"' in app_js
    assert "Guest completed portal" in app_js
    assert "loadPortalStatus" in app_js
    # The one-time URL is never made durable in the browser.
    assert "localStorage" not in app_js and "sessionStorage" not in app_js

    api_js = (ROOT / "dashboard" / "assets" / "api.js").read_text(encoding="utf-8")
    assert "/portal-status" in api_js and "/portal-link" in api_js

    for name in ("api.js", "app.js", "capabilities.mjs", "guide.json", "styles.css"):
        assert (ROOT / "dashboard" / "assets" / name).read_bytes() == (
            ROOT / "hospes" / "resources" / "dashboard" / "assets" / name
        ).read_bytes(), name


def test_documentation_and_completion_contract_are_live() -> None:
    documentation = (ROOT / "docs" / "guest-portal.md").read_text(encoding="utf-8")
    for marker in (
        "/guest/#token=",
        "POST /guest/session",
        "POST /guest/intake",
        "portal_sessions",
        "portal_receipts",
        "portal_date_choices",
        "guest_intake.completed",
        "consent.signed",
        "availability.provided",
        "guest_interaction: portal",
        "HOSPES_PORTAL_SECRET",
        "noindex",
        "private-field://",
    ):
        assert marker in documentation, marker
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/guest-portal.md" in readme

    # The schema this issue takes lands as one additive migration.
    assert migrations.LATEST_VERSION >= 17
    named = {migration.name for migration in migrations.MIGRATIONS}
    assert "guest_portal_intake_and_receipts" in named

    issue = completion_registry.load_registry().issue(33)
    assert issue.predicate == "python -m pytest tests/issue_predicates/test_issue_33.py -q"
    assert issue.receipt_owner == "github://organvm/hospes/issues/33"
    assert set(issue.required_surfaces) == {
        "app",
        "api",
        "service",
        "ui",
        "security",
        "browser",
        "documentation",
        "receipts",
    }
    assert "issue:30" in issue.dependencies
    assert "substrate.encryption_artifacts" in issue.dependencies
