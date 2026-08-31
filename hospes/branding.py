"""White-label brand custody, rendering, and browser-policy helpers.

A brand is declarative.  ``config/brand.yaml`` is the installation default and
``config/brands/<show>.yaml`` overrides it per show, so a new client is an edit
to tracked YAML rather than a code change.

Nothing here trusts a brand file.  Colours, fonts, logo and favicon custody,
and the custom domain are each validated before they can reach a browser, and
the resulting variables are published under a content-security policy that
names the exact style hash rather than relaxing the policy to
``'unsafe-inline'``.  A brand that cannot be rendered safely fails closed with
:class:`BrandError` instead of degrading into an unbranded page.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from pathlib import Path
from typing import Any

import yaml

from .paths import CONFIG_DIR


_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_FONT = re.compile(r"^[A-Za-z0-9 ,.'\"_-]{1,160}$")
_ASSET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_URL_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,120}$")
_HOSTNAME = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

#: Brand assets live in their own tracked custody directory.  They are never
#: reachable through the dashboard static mount, so the only way a logo or a
#: favicon leaves the server is the brand route, which resolves the *active*
#: show rather than a caller-supplied path.
ASSET_ROOT = CONFIG_DIR / "brand-assets"
ASSET_PREFIX = f"config/{ASSET_ROOT.name}/"
ASSET_MEDIA_TYPES = {
    ".ico": "image/vnd.microsoft.icon",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
ASSET_KINDS = {"logo": "logo_path", "favicon": "favicon_path"}

LOGO_PATH = "/operator/brand/logo"
FAVICON_PATH = "/operator/brand/favicon.ico"
PORTAL_LOGO_PATH = "/guest/brand/logo"
PORTAL_FAVICON_PATH = "/guest/brand/favicon.ico"
PORTAL_STYLESHEET_PATH = "/guest/brand.css"

BASE_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'"
)
PORTAL_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
    "img-src 'self'; form-action 'self'; base-uri 'none'"
)

DEFAULT_BRAND: dict[str, Any] = {
    "version": 1,
    "show_name": "HOSPES",
    "logo_path": f"{ASSET_PREFIX}hospes-logo.svg",
    "favicon_path": f"{ASSET_PREFIX}hospes-favicon.svg",
    "primary_color": "#c77842",
    "secondary_color": "#17221f",
    "accent_color": "#e4c48a",
    "font_family": "ui-sans-serif, system-ui, sans-serif",
    "custom_domain": None,
}

REQUIRED_BRAND_FIELDS = (
    "show_name",
    "logo_path",
    "favicon_path",
    "primary_color",
    "secondary_color",
    "accent_color",
    "font_family",
)

_PORTAL_LAYOUT_CSS = (
    "html{color-scheme:dark}"
    "body{background:var(--brand-secondary);color:#ede9e0;"
    "font-family:var(--brand-font);line-height:1.5;margin:0;padding:32px 20px}"
    "main{margin:auto;max-width:520px}"
    ".brand-mark{background-image:var(--brand-logo);background-position:left center;"
    "background-repeat:no-repeat;background-size:contain;display:block;height:34px;"
    "margin-bottom:18px;width:180px}"
    "h1{font-size:26px;letter-spacing:-.02em;margin:0 0 12px}"
    "label{display:grid;font-size:12px;gap:4px;margin-bottom:12px}"
    "input{background:#00000040;border:1px solid var(--brand-accent);border-radius:8px;"
    "color:inherit;font:inherit;min-height:44px;padding:0 10px}"
    "button{background:var(--brand-primary);border:1px solid var(--brand-primary);"
    "border-radius:8px;color:#111;font:inherit;font-weight:700;min-height:44px;padding:0 16px}"
    "#status{color:var(--brand-accent);font-size:13px}"
)


class BrandError(ValueError):
    """Raised when a brand file would publish an unsafe or missing value."""


def merge_brand(value: Any) -> dict[str, Any]:
    """Layer one brand mapping over the shipped defaults.

    A partial brand file is a supported profile, not an error: an operator who
    only wants a new show name should not have to restate the whole palette.
    """
    merged = dict(DEFAULT_BRAND)
    if isinstance(value, dict):
        for key, item in value.items():
            if item is not None:
                merged[key] = item
    return merged


def load_brand(path: str | Path | None = None) -> dict[str, Any]:
    """Load one brand file, layered over the shipped defaults."""
    selected = Path(path) if path else CONFIG_DIR / "brand.yaml"
    if not selected.exists():
        return dict(DEFAULT_BRAND)
    try:
        value = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BrandError(f"cannot read brand file {selected.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise BrandError(f"brand file {selected.name} must contain a YAML object")
    return merge_brand(value)


def brand_asset(brand: dict[str, Any] | None, kind: str) -> tuple[Path, str]:
    """Resolve one declared brand asset without allowing path traversal."""
    field = ASSET_KINDS.get(kind)
    if field is None:
        raise BrandError(f"unsupported brand asset kind {kind!r}")
    reference = merge_brand(brand).get(field)
    if not isinstance(reference, str) or not reference.startswith(ASSET_PREFIX):
        raise BrandError(f"{field} must be a repository-relative {ASSET_PREFIX} asset path")
    relative = reference[len(ASSET_PREFIX) :]
    suffix = Path(relative).suffix.lower()
    if not _ASSET_NAME.fullmatch(relative) or suffix not in ASSET_MEDIA_TYPES:
        raise BrandError(f"{field} must name one tracked brand asset file")
    unresolved = ASSET_ROOT / relative
    if unresolved.is_symlink():
        raise BrandError(f"{field} cannot use a symbolic link")
    candidate = unresolved.resolve()
    if candidate.parent != ASSET_ROOT.resolve() or not candidate.is_file():
        raise BrandError(f"{field} is not a registered brand asset")
    return candidate, ASSET_MEDIA_TYPES[suffix]


def custom_domain(brand: dict[str, Any] | None) -> str | None:
    """Return the validated white-label hostname, or ``None`` when unset."""
    value = merge_brand(brand).get("custom_domain")
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or value != value.strip().lower()
        or len(value) > 253
        or not _HOSTNAME.fullmatch(value)
    ):
        raise BrandError("custom_domain must be a bare lowercase hostname such as guests.example.com")
    return value


def portal_base_url(brand: dict[str, Any] | None, fallback: str) -> str:
    """Prefer the brand's custom domain for guest-facing links."""
    domain = custom_domain(brand)
    if domain is not None:
        return f"https://{domain}"
    if not isinstance(fallback, str) or not fallback.strip():
        raise BrandError("a guest link requires a custom_domain or an explicit base url")
    return fallback.strip()


def brand_variables(brand: dict[str, Any] | None = None, *, logo_url: str = LOGO_PATH) -> dict[str, str]:
    """Return the validated CSS custom properties a browser may receive."""
    values = merge_brand(brand)
    colors = {
        key: str(values.get(key, DEFAULT_BRAND[key])) for key in ("primary_color", "secondary_color", "accent_color")
    }
    if not all(_COLOR.fullmatch(value) for value in colors.values()):
        raise BrandError("brand colors must be six-digit hexadecimal values")
    font = str(values.get("font_family", DEFAULT_BRAND["font_family"]))
    if not _FONT.fullmatch(font):
        raise BrandError("brand font_family contains unsafe CSS characters")
    if not isinstance(logo_url, str) or not _URL_PATH.fullmatch(logo_url):
        raise BrandError("a brand logo url must be a same-origin absolute path")
    return {
        "--brand-primary": colors["primary_color"],
        "--brand-secondary": colors["secondary_color"],
        "--brand-accent": colors["accent_color"],
        "--brand-font": font,
        "--brand-logo": f'url("{logo_url}")',
    }


def brand_css(brand: dict[str, Any] | None = None, *, logo_url: str = LOGO_PATH) -> str:
    """Render the per-show ``:root`` variable block."""
    variables = brand_variables(brand, logo_url=logo_url)
    return ":root{" + ";".join(f"{key}:{value}" for key, value in variables.items()) + "}"


def style_source(css: str) -> str:
    """Return the CSP ``'sha256-…'`` source for one exact inline style body."""
    digest = hashlib.sha256(css.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def content_security_policy(css: str, *, base: str = BASE_CONTENT_SECURITY_POLICY) -> str:
    """Permit exactly the injected brand style and nothing else inline.

    ``default-src 'self'`` already forbids inline styles, so an injected
    ``<style>`` block is silently dropped by the browser unless the policy
    names it.  Naming the hash keeps the page branded without ever admitting
    ``'unsafe-inline'``.
    """
    return f"{base}; style-src 'self' {style_source(css)}"


def portal_stylesheet(brand: dict[str, Any] | None = None) -> str:
    """Return the guest portal's same-origin stylesheet body."""
    return brand_css(brand, logo_url=PORTAL_LOGO_PATH) + _PORTAL_LAYOUT_CSS


def render_branded_document(
    html_document: str,
    brand: dict[str, Any] | None = None,
    *,
    logo_url: str = LOGO_PATH,
) -> tuple[str, str]:
    """Return the branded document and the policy that lets a browser apply it."""
    values = merge_brand(brand if brand is not None else load_brand())
    css = brand_css(values, logo_url=logo_url)
    safe_name = html.escape(str(values.get("show_name", DEFAULT_BRAND["show_name"])))
    injected = html_document.replace("</head>", f"<style>{css}</style></head>", 1)
    return injected.replace("HOSPES", safe_name), content_security_policy(css)


def inject_brand(html_document: str, brand: dict[str, Any] | None = None) -> str:
    """Inject the brand variables and show name into one HTML document."""
    return render_branded_document(html_document, brand)[0]


def validate_brand(brand: dict[str, Any] | None) -> dict[str, Any]:
    """Fail closed on any brand a browser could not be served safely."""
    values = merge_brand(brand)
    name = values.get("show_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise BrandError("show_name must be a short non-empty label")
    brand_variables(values)
    for kind in sorted(ASSET_KINDS):
        brand_asset(values, kind)
    custom_domain(values)
    return values


__all__ = [
    "ASSET_KINDS",
    "ASSET_MEDIA_TYPES",
    "ASSET_PREFIX",
    "ASSET_ROOT",
    "BASE_CONTENT_SECURITY_POLICY",
    "BrandError",
    "DEFAULT_BRAND",
    "FAVICON_PATH",
    "LOGO_PATH",
    "PORTAL_CONTENT_SECURITY_POLICY",
    "PORTAL_FAVICON_PATH",
    "PORTAL_LOGO_PATH",
    "PORTAL_STYLESHEET_PATH",
    "REQUIRED_BRAND_FIELDS",
    "brand_asset",
    "brand_css",
    "brand_variables",
    "content_security_policy",
    "custom_domain",
    "inject_brand",
    "load_brand",
    "merge_brand",
    "portal_base_url",
    "portal_stylesheet",
    "render_branded_document",
    "style_source",
    "validate_brand",
]
