"""Localhost-only operator surface for HOSPES.

The dashboard never receives the API bearer token.  A human supplies that token
through a POST-only login form; the server exchanges it for an HttpOnly,
SameSite-strict process-local session cookie.  Authenticated dashboard requests
under ``/operator/api`` are then translated to the existing ``/v1`` API. The
actor, role, and tenant boundary remains process-bound and is never recovered
from browser headers. Mutations also require a same-origin CSRF token.

There is intentionally no configurable bind host.  This process is a local
operator surface and always listens on ``127.0.0.1``.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections.abc import Mapping
from html import escape as escape_html
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from . import branding
from .paths import DASHBOARD_DIR

try:  # keep core CLI importable without the optional API extra
    from fastapi import Request as _FastAPIRequest
except ImportError:  # pragma: no cover - exercised only without the optional extra
    _FastAPIRequest = Any  # type: ignore[misc,assignment]

def _dedupe_audit_events(events):
    """Deduplicate consecutive identical audit events, returning (event, count) pairs."""
    if not events:
        return []
    deduped = []
    for event in events:
        key = (
            event.get("created_at", ""),
            event.get("event_type", ""),
            event.get("actor_id", ""),
            event.get("actor_role", ""),
        )
        if deduped and deduped[-1][2] == key:
            deduped[-1] = (deduped[-1][0], deduped[-1][1] + 1, key)
        else:
            deduped.append((event, 1, key))
    return [(ev, count) for ev, count, _key in deduped]


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TOKEN_ENV = "HOSPES_OPERATOR_TOKEN"
SESSION_COOKIE = "hospes_operator_session"
SHOW_COOKIE = "hospes_operator_show"
_MAX_LOGIN_BODY = 8_192
_DASHBOARD_DIR = DASHBOARD_DIR


class OperatorConfigError(ValueError):
    """Raised when the operator boundary is incomplete or unsafe."""


def _required(value: str | None, env: Mapping[str, str], env_name: str, label: str) -> str:
    resolved = (value or env.get(env_name) or "").strip()
    if not resolved:
        raise OperatorConfigError(f"{label} is required (--{label} or {env_name})")
    return resolved


def resolve_identity(
    *,
    actor: str | None,
    role: str | None,
    tenant: str | None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, str]:
    """Resolve the explicit human boundary from flags or environment."""
    source = os.environ if env is None else env
    return (
        _required(actor, source, "HOSPES_OPERATOR_ACTOR", "actor"),
        _required(role, source, "HOSPES_OPERATOR_ROLE", "role"),
        _required(tenant, source, "HOSPES_OPERATOR_TENANT", "tenant"),
    )


def load_auth_token(
    token_env: str = DEFAULT_TOKEN_ENV,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Read the bearer secret from an environment variable, never argv."""
    source = os.environ if env is None else env
    if not token_env or not token_env.replace("_", "A").isalnum():
        raise OperatorConfigError("token environment variable name is invalid")
    token = source.get(token_env, "")
    if len(token) < 16 or token.strip() != token or any(ch.isspace() for ch in token):
        raise OperatorConfigError(f"{token_env} must contain a non-whitespace operator token of at least 16 characters")
    return token


def validate_port(port: int) -> int:
    if not 1 <= port <= 65_535:
        raise OperatorConfigError("port must be between 1 and 65535")
    return port


def _validate_operator_database_path(db_path: str | None) -> Path | None:
    if not db_path:
        return None
    database = Path(db_path).expanduser()
    absolute = database.absolute()
    if any(path.is_symlink() for path in (absolute, *absolute.parents)):
        raise OperatorConfigError("operator database path cannot use symlinks")
    if database.exists() and not database.is_file():
        raise OperatorConfigError("operator database path must be a regular file")
    for sidecar_suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database}{sidecar_suffix}")
        if sidecar.is_symlink():
            raise OperatorConfigError("operator database sidecar path is unsafe")
    return database


def create_operator_app(
    *,
    db_path: str | None,
    auth_token: str,
    actor_id: str,
    role: str,
    tenant_id: str,
    dashboard_dir: Path | None = None,
    session_secret: str | None = None,
    enable_raw_v1: bool = False,
    synthetic_demo: bool = False,
    secure_session_cookie: bool = False,
    bootstrap_nonce: str | None = None,
    bootstrap_expires_at: float | None = None,
    demo_pipeline_path: Path | None = None,
    demo_persona: str = "",
) -> Any:
    """Create the authenticated API plus its process-local dashboard."""
    # Keep the optional HTTP dependency lazy so core CLI commands still work
    # with the base package installation.
    try:
        from fastapi.responses import (
            FileResponse,
            HTMLResponse,
            JSONResponse,
            RedirectResponse,
        )
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - depends on optional install
        from .api import ApiExtraNotInstalled, _INSTALL_HINT

        raise ApiExtraNotInstalled(_INSTALL_HINT) from exc

    from . import (
        api,
        authentication,
        configuration,
        migrations,
        partnerships,
        service,
        store,
    )
    from .synthetic_demo import (
        RUNTIME_KIND,
        SyntheticDemoError,
        acquire_demo_lease,
        probe_synthetic_marker,
        validate_synthetic_bundle,
        validate_synthetic_database,
    )

    if synthetic_demo and enable_raw_v1:
        raise OperatorConfigError("synthetic demo mode cannot expose the raw /v1 API")
    if bootstrap_nonce is not None and (
        len(bootstrap_nonce) < 16
        or bootstrap_nonce.strip() != bootstrap_nonce
        or any(character.isspace() for character in bootstrap_nonce)
    ):
        raise OperatorConfigError("bootstrap nonce must be an opaque value of at least 16 characters")
    if (bootstrap_nonce is None) != (bootstrap_expires_at is None):
        raise OperatorConfigError("bootstrap nonce and expiry must be configured together")
    database = _validate_operator_database_path(db_path)
    if not api.ACTOR_ID.fullmatch(actor_id):
        raise OperatorConfigError("actor must be an opaque lowercase id accepted by the API")
    if not api.TENANT_ID.fullmatch(tenant_id):
        raise OperatorConfigError("tenant must be an opaque lowercase id accepted by the API")
    try:
        normalized_role = service.HumanRole(role).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in service.HumanRole)
        raise OperatorConfigError(f"role must be one of: {allowed}") from exc

    assets_root = dashboard_dir or _DASHBOARD_DIR
    index_path = assets_root / "index.html"
    login_path = assets_root / "login.html"
    static_path = assets_root / "assets"
    for required_path in (index_path, login_path, static_path):
        if not required_path.exists():
            raise OperatorConfigError(f"dashboard asset is missing: {required_path}")

    bound_actor = service.HumanActor(
        actor_id=actor_id,
        role=service.HumanRole(normalized_role),
        tenant_id=tenant_id,
    )
    runtime_kind = "authority"
    demo_scenario: str | None = None
    demo_lease = None
    if synthetic_demo:
        if database is None:
            raise OperatorConfigError("synthetic demo mode requires an explicit database")
        try:
            demo_lease = acquire_demo_lease(database.parent, shared=True)
            marker = validate_synthetic_database(database, tenant_id=tenant_id)
            validate_synthetic_bundle(database.parent)
        except SyntheticDemoError as exc:
            if demo_lease is not None:
                demo_lease.close()
            raise OperatorConfigError(str(exc)) from exc
        runtime_kind = RUNTIME_KIND
        demo_scenario = str(marker["scenario"])
    elif database is not None and database.is_file():
        try:
            marker = probe_synthetic_marker(database)
        except SyntheticDemoError as exc:
            raise OperatorConfigError(str(exc)) from exc
        if marker is not None:
            raise OperatorConfigError("a marked synthetic database requires --synthetic-demo")
    scenario_label = f"Synthetic {demo_scenario.replace('_', ' ')} specimen" if demo_scenario else None
    if bootstrap_nonce is not None and demo_scenario != "review_ready":
        if demo_lease is not None:
            demo_lease.close()
        raise OperatorConfigError("one-time bootstrap is available only for the review-ready synthetic specimen")
    resolved_demo_pipeline = demo_pipeline_path
    if resolved_demo_pipeline is not None:
        resolved_demo_pipeline = Path(resolved_demo_pipeline)
        if not resolved_demo_pipeline.is_file() or resolved_demo_pipeline.is_symlink():
            if demo_lease is not None:
                demo_lease.close()
            raise OperatorConfigError("synthetic demo pipeline must be a tracked regular file")
    cookie_secret = session_secret or secrets.token_urlsafe(32)
    if len(cookie_secret) < 16:
        if demo_lease is not None:
            demo_lease.close()
        raise OperatorConfigError("session secret must be at least 16 characters")
    try:
        app = api.create_app(
            db_path=str(database) if database is not None else None,
            auth_token=auth_token,
            bound_actor=bound_actor,
            raw_v1_enabled=enable_raw_v1,
            runtime_kind=runtime_kind,
            demo_scenario=demo_scenario,
            query_only=demo_scenario == "complete",
            scenario_label=scenario_label,
            csrf_secret=hashlib.sha256(cookie_secret.encode("utf-8")).digest(),
            csrf_cookie_secure=secure_session_cookie,
        )
    except Exception:
        if demo_lease is not None:
            demo_lease.close()
        raise
    operator_identity = authentication.process_bound_operator(bound_actor)

    def active_show_configs() -> tuple[configuration.ShowConfig, ...]:
        rows = store.fetch_all(
            app.state.conn,
            "SELECT show_id, label FROM show_registry WHERE tenant_id = ? AND status = 'active' ORDER BY show_id",
            (tenant_id,),
        )
        active_ids = {str(row["show_id"]) for row in rows}
        if synthetic_demo:
            return tuple(
                configuration.ShowConfig(
                    tenant_id=tenant_id,
                    show_id=str(row["show_id"]),
                    label=str(row["label"]),
                    enabled=True,
                    guest_interaction_mode="operator_packet",
                    outbound_mode="draft_only",
                    provider_precedence={},
                    roles=(),
                    dna_ref=None,
                    voice_ref=None,
                    template_ref=None,
                    brand_ref=None,
                    pilot_policy_ref=None,
                    display_order=index,
                    default=index == 0,
                    raw={"synthetic_demo": True},
                )
                for index, row in enumerate(rows)
            )
        configured = configuration.list_show_configs(tenant_id)
        if not configured:
            return ()
        return tuple(show for show in configured if show.show_id in active_ids)

    def show_brand(show_config: configuration.ShowConfig | None) -> dict[str, Any] | None:
        """Resolve one show's tracked brand, or None for the installation default."""
        if show_config is not None and show_config.brand_ref is not None:
            return branding.load_brand(
                configuration.show_resource_path(show_config.brand_ref, "brand")
            )
        return None

    def encode_show_cookie(show_id: str) -> str:
        digest = hmac.new(
            cookie_secret.encode("utf-8"),
            show_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{show_id}.{digest}"

    def decode_show_cookie(value: str | None) -> str | None:
        if not value or "." not in value:
            return None
        show_id, presented_digest = value.rsplit(".", 1)
        expected = encode_show_cookie(show_id).rsplit(".", 1)[1]
        if not authentication.OPAQUE_ID.fullmatch(show_id) or not secrets.compare_digest(presented_digest, expected):
            return None
        return show_id

    class OperatorSessionMiddleware:
        """Authenticate and translate same-origin dashboard API requests."""

        def __init__(self, application: Any) -> None:
            self.application = application

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") != "http" or not scope.get("path", "").startswith("/operator/api/"):
                await self.application(scope, receive, send)
                return

            headers = list(scope.get("headers", []))
            cookie_header = next(
                (value.decode("latin-1") for key, value in headers if key.lower() == b"cookie"),
                "",
            )
            cookies = {
                key.strip(): value.strip()
                for part in cookie_header.split(";")
                if "=" in part
                for key, value in [part.split("=", 1)]
            }
            presented = cookies.get(SESSION_COOKIE)
            if presented is None or not secrets.compare_digest(presented, cookie_secret):
                response = JSONResponse(
                    status_code=401,
                    content={"detail": "operator dashboard authentication is required"},
                )
                await response(scope, receive, send)
                return

            if demo_scenario == "complete" and str(scope.get("method", "GET")).upper() not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                response = JSONResponse(
                    status_code=409,
                    content={"detail": "the completed synthetic specimen is read-only"},
                )
                await response(scope, receive, send)
                return

            translated = dict(scope)
            query = parse_qs(
                bytes(scope.get("query_string", b"")).decode("ascii", "ignore"),
                keep_blank_values=True,
            )
            query_shows = query.get("show", [])
            header_show = next(
                (value.decode("ascii", "ignore") for key, value in headers if key.lower() == b"x-session-show"),
                None,
            )
            requested_show = header_show or (query_shows[0] if len(query_shows) == 1 else None)
            if len(query_shows) > 1 or (header_show is not None and query_shows and header_show != query_shows[0]):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "the operator show scope is unavailable"},
                )
                await response(scope, receive, send)
                return
            allowed_shows = {show.show_id for show in active_show_configs()}
            canonical_show = decode_show_cookie(cookies.get(SHOW_COOKIE))
            if (
                not allowed_shows
                or canonical_show not in allowed_shows
                or requested_show is None
                or requested_show != canonical_show
            ):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "the operator show scope is unavailable"},
                )
                await response(scope, receive, send)
                return
            translated["hospes.operator_show"] = canonical_show
            translated_path = "/v1/" + scope["path"][len("/operator/api/") :]
            translated["path"] = translated_path
            translated["raw_path"] = translated_path.encode("ascii")
            translated["hospes.operator_proxy"] = True
            translated["headers"] = [
                (key, value)
                for key, value in headers
                if key.lower() != b"authorization"
                and (not key.lower().startswith(b"x-hospes-") or key.lower() == b"x-hospes-csrf")
            ] + [
                (b"authorization", f"Bearer {auth_token}".encode("utf-8")),
            ]
            await self.application(translated, receive, send)

    app.add_middleware(OperatorSessionMiddleware)

    class SecurityHeadersMiddleware:
        """Attach the same browser protections to every operator response."""

        _HEADERS = {
            b"cache-control": b"no-store",
            b"content-security-policy": branding.BASE_CONTENT_SECURITY_POLICY.encode("ascii"),
            b"cross-origin-opener-policy": b"same-origin",
            b"cross-origin-resource-policy": b"same-origin",
            b"permissions-policy": (b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
            b"referrer-policy": b"no-referrer",
            b"strict-transport-security": b"max-age=31536000; includeSubDomains",
            b"x-content-type-options": b"nosniff",
            b"x-frame-options": b"DENY",
            b"x-robots-tag": b"noindex, nofollow, noarchive",
        }

        def __init__(self, application: Any) -> None:
            self.application = application

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            async def send_with_headers(message: dict[str, Any]) -> None:
                if message.get("type") == "http.response.start":
                    existing = {key.lower() for key, _value in message.get("headers", [])}
                    message["headers"] = list(message.get("headers", [])) + [
                        (key, value) for key, value in self._HEADERS.items() if key not in existing
                    ]
                await send(message)

            await self.application(scope, receive, send_with_headers)

    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/operator/assets", StaticFiles(directory=static_path), name="operator-assets")

    def has_session(request: _FastAPIRequest) -> bool:
        presented = request.cookies.get(SESSION_COOKIE)
        return presented is not None and secrets.compare_digest(presented, cookie_secret)

    def branded_response(content: str, brand: dict[str, Any] | None) -> Any:
        """Serve one branded document under a policy that names its style hash.

        ``default-src 'self'`` forbids inline styles, so a browser would drop
        the injected brand variables entirely without this header.
        """
        document, policy = branding.render_branded_document(content, brand)
        return HTMLResponse(
            document,
            headers={"Cache-Control": "no-store", "Content-Security-Policy": policy},
        )

    def html_asset(path: Path) -> Any:
        content = path.read_text(encoding="utf-8")
        if synthetic_demo:
            content = content.replace('data-runtime-kind=""', 'data-runtime-kind="synthetic"')
        if not synthetic_demo and path.suffix != ".html":
            return FileResponse(path, headers={"Cache-Control": "no-store"})
        return branded_response(content, None)

    def compact_overview_items(items: list[dict[str, Any]]) -> str:
        if not items:
            return '<p class="empty">None.</p>'
        rows = []
        for item in items:
            title = escape_html(str(item.get("title") or item.get("label") or ""))
            owner = item.get("owner")
            owner_text = f" · {escape_html(str(owner))}" if owner else ""
            rows.append(f"<li><b>{title}</b>{owner_text}</li>")
        return f"<ul>{''.join(rows)}</ul>"

    def dashboard_html(
        request: _FastAPIRequest,
        configured_shows: tuple[configuration.ShowConfig, ...],
        selected_show: str,
    ) -> Any:
        ledger = migrations.applied_migrations(app.state.conn)
        context_database = scenario_label or str(app.state.db_path)
        replacements: dict[str, str] = {
            "__HOSPES_CSRF__": escape_html(request.cookies.get(authentication.CSRF_COOKIE, ""), quote=True),
            "__HOSPES_CONTEXT_ACTOR__": escape_html(actor_id),
            "__HOSPES_CONTEXT_ROLE__": escape_html(normalized_role.replace("_", " ")),
            "__HOSPES_CONTEXT_TENANT__": escape_html(tenant_id),
            "__HOSPES_CONTEXT_DATABASE__": escape_html(context_database),
            "__HOSPES_CONTEXT_VERIFIED__": escape_html(str(ledger[-1]["applied_at"]) if ledger else "Unavailable"),
            "__HOSPES_SHOW_OPTIONS__": "",
            "__HOSPES_CANONICAL_SHOW__": escape_html(selected_show, quote=True),
            "__HOSPES_DEMO_PERSONA__": escape_html(demo_persona, quote=True),
            "__HOSPES_PARTNERSHIP_OPTIONS__": "",
            "__HOSPES_PARTNERSHIP_HEADING__": "No partnership loaded",
            "__HOSPES_PARTNERSHIP_PURPOSE__": ("Import a safe partnership template to begin."),
            "__HOSPES_STATUS__": "No partnership exists in this tenant.",
            "__HOSPES_TOTAL__": "0",
            "__HOSPES_ACTIVE__": "0",
            "__HOSPES_UNKNOWN__": "0",
            "__HOSPES_OVERDUE__": "0",
            "__HOSPES_COVERAGE__": "0/10",
            "__HOSPES_AGENDA__": '<p class="empty">No agenda is available.</p>',
            "__HOSPES_CAPABILITIES__": ('<p class="empty">No partnership capabilities are available.</p>'),
            "__HOSPES_AUDIT__": '<li class="empty">No events yet.</li>',
        }
        configured_by_id = {show.show_id: show for show in configured_shows}
        replacements["__HOSPES_SHOW_OPTIONS__"] = "".join(
            f'<option value="{escape_html(show.show_id, quote=True)}"'
            f"{' selected' if show.show_id == selected_show else ''}>"
            f"{escape_html(show.label)}</option>"
            for show in configured_shows
        )
        registry = partnerships.list_partnerships(app.state.conn, tenant_id, selected_show or None)
        if registry:
            selected = registry[0]
            center = partnerships.command_center(app.state.conn, str(selected["id"]), tenant_id, selected_show or None)
            replacements["__HOSPES_PARTNERSHIP_OPTIONS__"] = "".join(
                f'<option value="{escape_html(str(item["id"]), quote=True)}"'
                f"{' selected' if item['id'] == selected['id'] else ''}>"
                f"{escape_html(str(item['label']))}</option>"
                for item in registry
            )
            replacements["__HOSPES_PARTNERSHIP_HEADING__"] = escape_html(str(center["partnership"]["label"]))
            replacements["__HOSPES_PARTNERSHIP_PURPOSE__"] = escape_html(str(center["partnership"]["purpose"]))
            summary = center["summary"]
            replacements["__HOSPES_TOTAL__"] = str(summary["total"])
            replacements["__HOSPES_ACTIVE__"] = str(summary["active"])
            replacements["__HOSPES_UNKNOWN__"] = str(summary["unknown"])
            replacements["__HOSPES_OVERDUE__"] = str(summary["overdue"])
            coverage = center["coverage"]
            replacements["__HOSPES_COVERAGE__"] = f"{coverage['covered']}/{coverage['total']}"
            agenda = center["agenda"]
            agenda_groups = (
                ("Decisions required", agenda["decisions_required"]),
                ("Overdue obligations", agenda["overdue_obligations"]),
                ("Blockers", agenda["blockers"]),
                ("Risks", agenda["risks"]),
                ("Unknowns", agenda["unknowns"]),
            )
            agenda_html = "".join(
                f"<details{' open' if items else ''}><summary>{escape_html(label)} · "
                f"{len(items)}</summary>{compact_overview_items(items)}</details>"
                for label, items in agenda_groups
            )
            next_gate = agenda.get("next_recording_gate") or {}
            next_gate_label = escape_html(str(next_gate.get("label") or "All pilot gates satisfied"))
            replacements["__HOSPES_AGENDA__"] = (
                f'{agenda_html}<div class="next-gate"><span>Next recording gate</span><b>{next_gate_label}</b></div>'
            )
            replacements["__HOSPES_CAPABILITIES__"] = "".join(
                f'<div class="check {"met" if enabled else "missing"}">'
                f'<span aria-hidden="true">{"✓" if enabled else "×"}</span>'
                f"<b>{escape_html(key.replace('_', ' '))}</b></div>"
                for key, enabled in center["engine_capabilities"].items()
            )
            events = center["recent_events"][:12]
            deduped = _dedupe_audit_events(events)
            replacements["__HOSPES_AUDIT__"] = (
                "".join(
                    f"<li><time>{escape_html(str(event['created_at']))}</time>"
                    f"<b>{escape_html(str(event['event_type']).replace('.', ' · '))}"
                    f"{f' ({count}×)' if count > 1 else ''}</b>"
                    f'<span class="sensitive-identity">{escape_html(str(event["actor_id"]))}'
                    f" · {escape_html(str(event['actor_role']))}</span></li>"
                    for event, count in deduped
                )
                or '<li class="empty">No events yet.</li>'
            )
            replacements["__HOSPES_STATUS__"] = escape_html(
                f"Loaded {summary['total']} current items and "
                f"{coverage['covered']}/{coverage['total']} register domains."
            )

        content = index_path.read_text(encoding="utf-8")
        if synthetic_demo:
            content = content.replace("<body>", "<body data-runtime-kind=\"synthetic\">", 1)
        selected_config = configured_by_id.get(selected_show)
        marker_pattern = re.compile("|".join(re.escape(marker) for marker in replacements))
        content = marker_pattern.sub(lambda match: replacements[match.group(0)], content)
        return branded_response(content, show_brand(selected_config))

    def request_brand(request: _FastAPIRequest) -> dict[str, Any] | None:
        """Resolve the brand this caller may see.

        Before login there is no show scope, so only the installation default
        brand is served: the login page must be branded without letting an
        unauthenticated caller enumerate a tenant's shows or their marks.
        """
        if not has_session(request):
            return None
        canonical_show = decode_show_cookie(request.cookies.get(SHOW_COOKIE))
        if canonical_show is None:
            return None
        configured_by_id = {show.show_id: show for show in active_show_configs()}
        return show_brand(configured_by_id.get(canonical_show))

    def brand_asset_response(request: _FastAPIRequest, kind: str) -> Any:
        brand = request_brand(request) or branding.load_brand()
        try:
            asset_path, media_type = branding.brand_asset(brand, kind)
        except branding.BrandError as exc:
            raise api.HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            asset_path,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/operator/brand/logo", include_in_schema=False)
    def brand_logo(request: _FastAPIRequest) -> Any:
        return brand_asset_response(request, "logo")

    @app.get("/operator/brand/favicon.ico", include_in_schema=False)
    def brand_favicon(request: _FastAPIRequest) -> Any:
        return brand_asset_response(request, "favicon")

    bootstrap_lock = threading.Lock()
    bootstrap_consumed = False

    @app.get("/", include_in_schema=False)
    def root() -> Any:
        return RedirectResponse(url="/operator/", status_code=307)

    @app.get("/operator/login", include_in_schema=False)
    def login(request: _FastAPIRequest) -> Any:
        if has_session(request):
            return RedirectResponse(url="/operator/", status_code=303)
        return html_asset(login_path)

    @app.post("/operator/session", include_in_schema=False)
    async def establish_session(request: _FastAPIRequest) -> Any:
        body = await request.body()
        if len(body) > _MAX_LOGIN_BODY:
            return HTMLResponse("Login request is too large.", status_code=413)
        try:
            supplied = parse_qs(body.decode("utf-8"), keep_blank_values=True).get("token", [""])[0]
        except UnicodeDecodeError:
            supplied = ""
        if not supplied or not secrets.compare_digest(supplied, auth_token):
            return HTMLResponse("Operator authentication failed.", status_code=401)
        response = RedirectResponse(url="/operator/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            cookie_secret,
            max_age=8 * 60 * 60,
            httponly=True,
            samesite="strict",
            secure=secure_session_cookie,
            path="/",
        )
        response.set_cookie(
            authentication.CSRF_COOKIE,
            app.state.csrf_protector.issue(operator_identity),
            max_age=8 * 60 * 60,
            httponly=False,
            samesite="strict",
            secure=secure_session_cookie,
            path="/",
        )
        return response

    if bootstrap_nonce is not None:
        assert bootstrap_expires_at is not None

        @app.post("/operator/bootstrap", include_in_schema=False)
        async def bootstrap(request: _FastAPIRequest) -> Any:
            nonlocal bootstrap_consumed
            if request.url.hostname != LOOPBACK_HOST:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "bootstrap requires the exact loopback host"},
                )
            body = await request.body()
            if len(body) > _MAX_LOGIN_BODY:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "bootstrap request is too large"},
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                payload = {}
            supplied = payload.get("nonce", "") if isinstance(payload, dict) else ""
            with bootstrap_lock:
                accepted = (
                    not bootstrap_consumed
                    and time.time() <= bootstrap_expires_at
                    and isinstance(supplied, str)
                    and secrets.compare_digest(supplied, bootstrap_nonce)
                )
                if accepted:
                    bootstrap_consumed = True
            if not accepted:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "bootstrap token is invalid or expired"},
                )
            response = JSONResponse({"ok": True})
            response.set_cookie(
                SESSION_COOKIE,
                cookie_secret,
                max_age=8 * 60 * 60,
                httponly=True,
                samesite="strict",
                secure=secure_session_cookie,
                path="/",
            )
            response.set_cookie(
                authentication.CSRF_COOKIE,
                app.state.csrf_protector.issue(operator_identity),
                max_age=8 * 60 * 60,
                httponly=False,
                samesite="strict",
                secure=secure_session_cookie,
                path="/",
            )
            return response

    if demo_scenario == "review_ready" and resolved_demo_pipeline is not None:

        @app.get("/operator/demo-pipeline.csv", include_in_schema=False)
        def demo_pipeline(request: _FastAPIRequest) -> Any:
            if not has_session(request):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "operator dashboard authentication is required"},
                )
            return FileResponse(
                resolved_demo_pipeline,
                media_type="text/csv; charset=utf-8",
                filename="demo-pipeline.csv",
                headers={"Cache-Control": "no-store"},
            )

    @app.post("/operator/logout", include_in_schema=False)
    async def logout(request: _FastAPIRequest) -> Any:
        if not has_session(request):
            return HTMLResponse("Operator authentication failed.", status_code=401)
        body = await request.body()
        try:
            supplied = parse_qs(body.decode("utf-8"), keep_blank_values=True).get("csrf", [""])[0]
        except UnicodeDecodeError:
            supplied = ""
        csrf_cookie = request.cookies.get(authentication.CSRF_COOKIE)
        if not supplied or not csrf_cookie or not secrets.compare_digest(supplied, csrf_cookie):
            return HTMLResponse("CSRF validation failed.", status_code=403)
        try:
            app.state.csrf_protector.validate(operator_identity, supplied)
        except authentication.CSRFDenied:
            return HTMLResponse("CSRF validation failed.", status_code=403)
        response = RedirectResponse(url="/operator/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(SHOW_COOKIE, path="/")
        response.delete_cookie(authentication.CSRF_COOKIE, path="/")
        return response

    @app.get("/operator/", include_in_schema=False)
    def dashboard(request: _FastAPIRequest) -> Any:
        if not has_session(request):
            return RedirectResponse(url="/operator/login", status_code=303)
        configured_shows = active_show_configs()
        if not configured_shows:
            raise api.HTTPException(
                status_code=403,
                detail="no active operator show is available for this tenant",
            )
        configured_by_id = {show.show_id: show for show in configured_shows}
        requested_show = request.query_params.get("show")
        if requested_show is None:
            cookie_show = decode_show_cookie(request.cookies.get(SHOW_COOKIE))
            selected_show = (
                cookie_show
                if cookie_show in configured_by_id
                else next(
                    (show.show_id for show in configured_shows if show.default),
                    configured_shows[0].show_id if configured_shows else "",
                )
            )
            if selected_show:
                return RedirectResponse(
                    url="/operator/?" + urlencode({"show": selected_show}),
                    status_code=303,
                )
        else:
            selected_config = configured_by_id.get(requested_show)
            if selected_config is None:
                raise api.HTTPException(
                    status_code=403,
                    detail="the operator show scope is unavailable",
                )
            # Reflect only the canonical identifier loaded from tracked configuration,
            # never the request's spelling, into redirects, HTML, or cookies.
            selected_show = selected_config.show_id
        response = dashboard_html(request, configured_shows, selected_show)
        if selected_show:
            response.set_cookie(
                SHOW_COOKIE,
                encode_show_cookie(selected_show),
                max_age=8 * 60 * 60,
                httponly=True,
                samesite="strict",
                secure=secure_session_cookie,
                path="/",
            )
        return response

    app.state.operator_actor = actor_id
    app.state.operator_role = normalized_role
    app.state.operator_tenant = tenant_id
    app.state.runtime_kind = runtime_kind
    app.state.demo_scenario = demo_scenario
    app.state.synthetic_demo_lease = demo_lease
    app.state.secure_session_cookie = secure_session_cookie
    if demo_lease is not None:
        app.router.add_event_handler("shutdown", demo_lease.close)
    return app


def serve_operator(
    *,
    db_path: str | None,
    port: int,
    actor: str | None,
    role: str | None,
    tenant: str | None,
    token_env: str = DEFAULT_TOKEN_ENV,
    env: Mapping[str, str] | None = None,
    enable_raw_v1: bool = False,
    synthetic_demo: bool = False,
    secure_session_cookie: bool = False,
) -> None:
    """Validate configuration and run the localhost operator process."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on optional install
        from .api import ApiExtraNotInstalled, _INSTALL_HINT

        raise ApiExtraNotInstalled(_INSTALL_HINT) from exc

    source = os.environ if env is None else env
    resolved_db = db_path or source.get("HOSPES_DATABASE_URL") or source.get("HOSPES_DB")
    if not resolved_db:
        raise OperatorConfigError(
            "database is required for operator mode "
            "(--db, HOSPES_DATABASE_URL, or HOSPES_DB); "
            "out/hospes.sqlite3 is not an implicit live authority"
        )
    actor_id, normalized_role, tenant_id = resolve_identity(actor=actor, role=role, tenant=tenant, env=env)
    token = load_auth_token(token_env, env=env)
    app = create_operator_app(
        db_path=resolved_db,
        auth_token=token,
        actor_id=actor_id,
        role=normalized_role,
        tenant_id=tenant_id,
        enable_raw_v1=enable_raw_v1,
        synthetic_demo=synthetic_demo,
        secure_session_cookie=secure_session_cookie,
    )
    uvicorn.run(app, host=LOOPBACK_HOST, port=validate_port(port), log_level="info")
