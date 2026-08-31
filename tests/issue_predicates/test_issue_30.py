"""Issue #30 predicate: white-label / brand configuration.

Close condition: logos, favicons, login, header, portal, custom-domain config,
per-show CSS variables, and browser assertions pass.

The browser assertions are the load-bearing half.  ``default-src 'self'``
forbids inline styles, so a per-show ``:root`` block injected into the document
is *dropped by the browser* unless the response policy names its exact hash --
the page still contains the variables and the product still renders in the
default palette.  These tests therefore assert what a browser would apply and
fetch, not merely what the markup says.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import yaml

from hospes import branding, completion_registry, configuration, guest_portal, platform, store

pytest.importorskip("fastapi", reason="the operator surface requires the optional 'api' extra")
from fastapi.testclient import TestClient  # noqa: E402

from hospes import operator  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TENANT = "hospes"
FLAGSHIP = "flagship"
FIELD = "field"
CLIENT = "client-x"
TOKEN = "issue30-operator-token-0123456789"  # allow-secret: synthetic test fixture
SESSION_SECRET = "issue30-session-secret-0123456789"  # allow-secret: synthetic test fixture
PORTAL_SECRET = "issue30-portal-secret-value"  # allow-secret: synthetic test fixture

SHOW_LABELS = {
    FLAGSHIP: "Flagship Show",
    FIELD: "Field Show",
    CLIENT: "Client: Podcast X",
}


def _brand(show_id: str) -> dict[str, Any]:
    return branding.load_brand(ROOT / "config" / "brands" / f"{show_id}.yaml")


def _seed(path: Path) -> None:
    conn = store.connect(path)
    for show_id, label in SHOW_LABELS.items():
        platform.register_show(
            conn,
            tenant_id=TENANT,
            show_id=show_id,
            label=label,
            config_ref=f"config/shows/{show_id}.yaml",
        )
    conn.commit()
    conn.close()


def _operator_app(path: Path) -> Any:
    return operator.create_operator_app(
        db_path=str(path),
        auth_token=TOKEN,  # allow-secret: synthetic test fixture
        actor_id="ari_fixture",
        role="relationship_owner",
        tenant_id=TENANT,
        session_secret=SESSION_SECRET,  # allow-secret: synthetic test fixture
    )


def _unlocked(path: Path) -> TestClient:
    client = TestClient(_operator_app(path))
    client.post("/operator/session", data={"token": TOKEN}, follow_redirects=False)
    return client


def _signed_show_cookie(show_id: str) -> str:
    digest = hmac.new(SESSION_SECRET.encode("utf-8"), show_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{show_id}.{digest}"


class _Subresources(HTMLParser):
    """Collect what a browser would fetch and apply from one document."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.style_chunks: list[str] = []
        self.marks = 0
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "style":
            self._in_style = True
        if tag in {"link", "img", "script"}:
            reference = values.get("href") if tag == "link" else values.get("src")
            if reference:
                self.urls.append(reference)
        if "brand-mark" in values.get("class", "").split():
            self.marks += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.style_chunks.append(data)

    @property
    def style(self) -> str:
        return "".join(self.style_chunks)


def _parsed(document: str) -> _Subresources:
    parser = _Subresources()
    parser.feed(document)
    return parser


def test_per_show_css_variables_and_the_style_hash_reach_the_browser(tmp_path: Path) -> None:
    database = tmp_path / "issue30-variables.sqlite3"
    _seed(database)
    with _unlocked(database) as client:
        for show_id in (FIELD, CLIENT):
            brand = _brand(show_id)
            page = client.get(f"/operator/?show={show_id}")
            assert page.status_code == 200, page.text

            # Every declared brand value is published as a CSS variable.
            parsed = _parsed(page.text)
            assert parsed.style == branding.brand_css(brand)
            for variable, value in (
                ("--brand-primary", brand["primary_color"]),
                ("--brand-secondary", brand["secondary_color"]),
                ("--brand-accent", brand["accent_color"]),
                ("--brand-font", brand["font_family"]),
                ("--brand-logo", 'url("/operator/brand/logo")'),
            ):
                assert f"{variable}:{value}" in parsed.style

            # ...and the policy names that exact block, so a browser applies it.
            policy = page.headers["content-security-policy"]
            assert branding.style_source(parsed.style) in policy
            assert "'unsafe-inline'" not in policy
            assert "default-src 'self'" in policy

            # The header carries the show's own mark and the show's own name.
            assert parsed.marks == 1
            assert brand["show_name"] in page.text
            assert "HOSPES · private partnership operations" not in page.text

        # Two shows never share a palette, and therefore never share a hash.
        field_policy = client.get(f"/operator/?show={FIELD}").headers["content-security-policy"]
        client_policy = client.get(f"/operator/?show={CLIENT}").headers["content-security-policy"]
        assert field_policy != client_policy


def test_logo_and_favicon_routes_serve_the_active_show(tmp_path: Path) -> None:
    database = tmp_path / "issue30-assets.sqlite3"
    _seed(database)
    with _unlocked(database) as client:
        for show_id in (FLAGSHIP, FIELD, CLIENT):
            brand = _brand(show_id)
            assert client.get(f"/operator/?show={show_id}").status_code == 200
            for kind, route in (
                ("logo", "/operator/brand/logo"),
                ("favicon", "/operator/brand/favicon.ico"),
            ):
                asset_path, media_type = branding.brand_asset(brand, kind)
                served = client.get(route)
                assert served.status_code == 200, route
                assert served.headers["content-type"].startswith(media_type)
                assert served.content == asset_path.read_bytes()
                assert served.headers["cache-control"] == "no-store"

        # Each show ships its own marks, so the route is not a constant.
        client.get(f"/operator/?show={FIELD}")
        field_logo = client.get("/operator/brand/logo").content
        field_favicon = client.get("/operator/brand/favicon.ico").content
        assert field_logo != field_favicon
        client.get(f"/operator/?show={CLIENT}")
        assert client.get("/operator/brand/logo").content != field_logo


def test_brand_routes_fail_closed_before_login_and_on_an_unowned_show(tmp_path: Path) -> None:
    database = tmp_path / "issue30-scope.sqlite3"
    _seed(database)
    default_logo = branding.brand_asset(branding.load_brand(), "logo")[0].read_bytes()
    field_logo = branding.brand_asset(_brand(FIELD), "logo")[0].read_bytes()
    assert default_logo != field_logo

    with TestClient(_operator_app(database)) as anonymous:
        # The login page must be branded, so the route answers -- but only ever
        # with the installation default, never with a tenant's show mark.
        served = anonymous.get("/operator/brand/logo")
        assert served.status_code == 200
        assert served.content == default_logo

    with _unlocked(database) as client:
        assert client.get(f"/operator/?show={FIELD}").status_code == 200
        assert client.get("/operator/brand/logo").content == field_logo

        # A tampered show cookie is not a show scope.
        client.cookies.set(operator.SHOW_COOKIE, f"{FIELD}.deadbeef")
        assert client.get("/operator/brand/logo").content == default_logo

        # Neither is a correctly signed cookie for a show that is not running.
        client.cookies.set(operator.SHOW_COOKIE, _signed_show_cookie("ghost"))
        assert client.get("/operator/brand/logo").content == default_logo
        assert client.get("/operator/brand/favicon.ico").status_code == 200

        # The signature itself is what admits the show scope back.
        client.cookies.set(operator.SHOW_COOKIE, _signed_show_cookie(FIELD))
        assert client.get("/operator/brand/logo").content == field_logo


def test_login_page_is_branded_by_the_installation_default(tmp_path: Path) -> None:
    database = tmp_path / "issue30-login.sqlite3"
    _seed(database)
    default_brand = branding.load_brand()
    with TestClient(_operator_app(database)) as client:
        page = client.get("/operator/login")
        assert page.status_code == 200
        parsed = _parsed(page.text)
        assert parsed.marks == 1, "the login shell shows the brand mark"
        assert "/operator/brand/favicon.ico" in parsed.urls
        assert "data:," not in page.text
        assert parsed.style == branding.brand_css(default_brand)
        assert branding.style_source(parsed.style) in page.headers["content-security-policy"]
        assert str(default_brand["show_name"]) in page.text


def test_browser_fetches_every_branded_subresource_under_the_response_policy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "issue30-browser.sqlite3"
    _seed(database)
    stylesheet = (ROOT / "dashboard" / "assets" / "styles.css").read_text(encoding="utf-8")

    # A variable nothing reads is decoration, not a white label.
    for variable in (
        "--brand-primary",
        "--brand-secondary",
        "--brand-accent",
        "--brand-font",
        "--brand-logo",
    ):
        assert f"var({variable})" in stylesheet, f"{variable} is injected but never consumed"

    with _unlocked(database) as client:
        for path in (f"/operator/?show={CLIENT}", "/operator/login"):
            page = client.get(path)
            assert page.status_code == 200, path
            parsed = _parsed(page.text)
            referenced = list(parsed.urls) + re.findall(r'url\("([^"]+)"\)', parsed.style)
            assert referenced, path

            for url in referenced:
                # Nothing a branded page loads may leave the origin: the policy
                # is 'self', so a cross-origin reference is a blocked request.
                assert not url.startswith(("http://", "https://", "//", "data:")), url
                resolved = url if url.startswith("/") else "/operator/" + url.removeprefix("./")
                fetched = client.get(resolved)
                assert fetched.status_code == 200, resolved
                assert fetched.headers["content-type"], resolved

            policy = page.headers["content-security-policy"]
            assert branding.style_source(parsed.style) in policy
            for directive in (
                "base-uri 'none'",
                "frame-ancestors 'none'",
                "object-src 'none'",
                "form-action 'self'",
            ):
                assert directive in policy


def test_guest_portal_is_branded_and_its_policy_admits_its_own_assets(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue30-portal.sqlite3")
    brand = _brand(CLIENT)
    portal = guest_portal.create_app(
        conn=conn,
        secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
        enabled=True,
        brand=brand,
    )
    with TestClient(portal) as client:
        landing = client.get("/guest/")
        assert landing.status_code == 200
        parsed = _parsed(landing.text)
        assert parsed.marks == 1
        assert branding.PORTAL_STYLESHEET_PATH in parsed.urls
        assert branding.PORTAL_FAVICON_PATH in parsed.urls
        assert "Client: Podcast X guest intake" in landing.text

        policy = landing.headers["content-security-policy"]
        for directive in ("default-src 'none'", "style-src 'self'", "img-src 'self'"):
            assert directive in policy
        assert "'unsafe-inline'" not in policy

        # The portal styles itself from a same-origin sheet, so it needs no
        # inline style at all and emits none.
        assert parsed.style == ""
        sheet = client.get(branding.PORTAL_STYLESHEET_PATH)
        assert sheet.status_code == 200
        assert sheet.headers["content-type"].startswith("text/css")
        assert branding.brand_css(brand, logo_url=branding.PORTAL_LOGO_PATH) in sheet.text
        assert 'url("/guest/brand/logo")' in sheet.text

        for kind, route in (
            ("logo", branding.PORTAL_LOGO_PATH),
            ("favicon", branding.PORTAL_FAVICON_PATH),
        ):
            asset_path, media_type = branding.brand_asset(brand, kind)
            served = client.get(route)
            assert served.status_code == 200, route
            assert served.headers["content-type"].startswith(media_type)
            assert served.content == asset_path.read_bytes()

    disabled = guest_portal.create_app(
        conn=conn,
        secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
        enabled=False,
        brand=brand,
    )
    with TestClient(disabled) as client:
        for route in ("/guest/", branding.PORTAL_STYLESHEET_PATH, branding.PORTAL_LOGO_PATH):
            assert client.get(route).status_code == 404, route

    with pytest.raises(guest_portal.PortalError, match="brand is invalid"):
        guest_portal.create_app(
            conn=conn,
            secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
            enabled=True,
            brand={"primary_color": "red"},
        )
    conn.close()


def test_custom_domain_configuration_drives_the_guest_link_receipt(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "issue30-domain.sqlite3")
    branded = guest_portal.create_portal_token(
        conn,
        tenant_id=TENANT,
        show_id=CLIENT,
        guest_id="synthetic-guest",
        secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
        base_url="http://127.0.0.1:8766",
        enabled=True,
        brand=_brand(CLIENT),
    )
    assert branded["public_base_url"] == "https://guests.podcast-x.example"
    assert branded["url"].startswith("https://guests.podcast-x.example/guest/#token=")

    unbranded = guest_portal.create_portal_token(
        conn,
        tenant_id=TENANT,
        show_id=FIELD,
        guest_id="synthetic-guest",
        secret=PORTAL_SECRET,  # allow-secret: synthetic test fixture
        base_url="http://127.0.0.1:8766",
        enabled=True,
        brand=_brand(FIELD),
    )
    assert unbranded["public_base_url"] == "http://127.0.0.1:8766"
    assert unbranded["url"].startswith("http://127.0.0.1:8766/guest/#token=")

    # Each link leaves a durable, digest-only receipt row behind it.
    rows = store.fetch_all(
        conn,
        "SELECT id, show_id, token_digest, used_at FROM portal_tokens ORDER BY show_id",
    )
    assert [row["show_id"] for row in rows] == [CLIENT, FIELD]
    assert all(len(str(row["token_digest"])) == 64 and row["used_at"] is None for row in rows)
    assert all(str(row["token_digest"]) not in branded["url"] for row in rows)
    conn.close()

    for value in (
        "https://guests.podcast-x.example",
        "GUESTS.PODCAST-X.EXAMPLE",
        "guests.podcast-x.example/intake",
        "guests.podcast-x.example:8443",
        "-guests.example",
        "nodot",
        " guests.example",
    ):
        with pytest.raises(branding.BrandError, match="custom_domain"):
            branding.custom_domain({"custom_domain": value})

    assert branding.custom_domain({"custom_domain": None}) is None
    assert branding.portal_base_url({"custom_domain": None}, "https://fallback.example") == "https://fallback.example"
    with pytest.raises(branding.BrandError, match="base url"):
        branding.portal_base_url({"custom_domain": None}, "   ")


def test_brand_asset_custody_rejects_traversal_absolute_and_symlink_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for reference in (
        "config/brand-assets/../../etc/passwd",
        "/etc/passwd",
        "config/brand-assets/nested/logo.svg",
        "config/brand-assets/logo.txt",
        "config/brand-assets/missing-logo.svg",
        "dashboard/assets/styles.css",
        "",
    ):
        with pytest.raises(branding.BrandError):
            branding.brand_asset({"logo_path": reference}, "logo")

    with pytest.raises(branding.BrandError, match="unsupported brand asset kind"):
        branding.brand_asset(branding.load_brand(), "wordmark")

    root = tmp_path / "brand-assets"
    root.mkdir()
    outside = tmp_path / "outside.svg"
    outside.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (root / "linked.svg").symlink_to(outside)
    monkeypatch.setattr(branding, "ASSET_ROOT", root)
    with pytest.raises(branding.BrandError, match="symbolic link"):
        branding.brand_asset({"logo_path": "config/brand-assets/linked.svg"}, "logo")


def test_unsafe_colours_fonts_and_logo_urls_never_reach_a_stylesheet() -> None:
    for unsafe in (
        {"primary_color": "red"},
        {"secondary_color": "#12345"},
        {"accent_color": "rgb(0,0,0)"},
        {"font_family": "x; } body { background: url(https://evil.example)"},
    ):
        with pytest.raises(branding.BrandError):
            branding.brand_css(unsafe)
    with pytest.raises(branding.BrandError, match="same-origin absolute path"):
        branding.brand_css(None, logo_url="https://cdn.example/logo.svg")

    # A partial brand file is a supported profile, not a failure.
    partial = branding.merge_brand({"show_name": "The Ari Show", "primary_color": "#ff6b35"})
    assert partial["show_name"] == "The Ari Show"
    assert partial["accent_color"] == branding.DEFAULT_BRAND["accent_color"]
    assert "--brand-primary:#ff6b35" in branding.brand_css(partial)

    # The show name is escaped before it is rendered into any document.
    document, policy = branding.render_branded_document(
        "<html><head></head><body>HOSPES</body></html>",
        {"show_name": "<script>alert(1)</script>"},
    )
    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;" in document
    assert branding.style_source(branding.brand_css({"show_name": "x"})) in policy


def test_every_shipped_show_declares_its_own_validated_brand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert configuration.validate_configuration() == []
    shows = {show.show_id: show for show in configuration.list_show_configs(TENANT)}
    assert {FLAGSHIP, FIELD, CLIENT} <= set(shows)

    logos: set[bytes] = set()
    favicons: set[bytes] = set()
    for show_id, show in shows.items():
        assert show.brand_ref == f"config/brands/{show_id}.yaml"
        raw = yaml.safe_load(configuration.show_resource_path(show.brand_ref, "brand").read_text(encoding="utf-8"))
        for field_name in branding.REQUIRED_BRAND_FIELDS:
            assert isinstance(raw.get(field_name), str) and raw[field_name].strip(), field_name
        branding.validate_brand(raw)
        logos.add(branding.brand_asset(raw, "logo")[0].read_bytes())
        favicons.add(branding.brand_asset(raw, "favicon")[0].read_bytes())
    assert len(logos) == len(shows), "each show ships its own logo"
    assert len(favicons) == len(shows), "each show ships its own favicon"

    # An incomplete or unsafe show brand fails the shipped configuration gate.
    brands_root = tmp_path / "brands"
    brands_root.mkdir()
    monkeypatch.setitem(configuration._SHOW_RESOURCE_ROOTS, "brand", brands_root)
    authored = dict(yaml.safe_load((ROOT / "config" / "brands" / f"{CLIENT}.yaml").read_text(encoding="utf-8")))

    incomplete = {key: value for key, value in authored.items() if key != "logo_path"}
    (brands_root / f"{CLIENT}.yaml").write_text(yaml.safe_dump(incomplete), encoding="utf-8")
    with pytest.raises(configuration.ConfigurationError, match="missing its required"):
        configuration.show_profile(shows[CLIENT])

    unsafe = dict(authored, custom_domain="https://guests.podcast-x.example")
    (brands_root / f"{CLIENT}.yaml").write_text(yaml.safe_dump(unsafe), encoding="utf-8")
    with pytest.raises(configuration.ConfigurationError, match="brand resource is invalid"):
        configuration.show_profile(shows[CLIENT])


def test_documentation_and_completion_contract_are_live() -> None:
    documentation = (ROOT / "docs" / "white-label.md").read_text(encoding="utf-8")
    for marker in (
        "config/brand.yaml",
        "config/brands/<show>.yaml",
        "config/brand-assets/",
        "custom_domain",
        "--brand-logo",
        "/operator/brand/logo",
        "/operator/brand/favicon.ico",
        "/guest/brand.css",
        "style-src 'self'",
        "sha256",
    ):
        assert marker in documentation, marker
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/white-label.md" in readme

    issue = completion_registry.load_registry().issue(30)
    assert issue.predicate == "python -m pytest tests/issue_predicates/test_issue_30.py -q"
    assert issue.receipt_owner == "github://organvm/hospes/issues/30"
    assert set(issue.required_surfaces) == {
        "service",
        "ui",
        "security",
        "browser",
        "documentation",
        "receipts",
    }
    assert "issue:23" in issue.dependencies
