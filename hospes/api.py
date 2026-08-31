"""Optional HTTP surface for the guest-operations service layer.

Ported from the overnight ``agent/hospes-core-api`` slice (PR #2). FastAPI is
kept as an **optional** extra so the local CLI and core tests never require it:

    pip install -e '.[api]'          # installs fastapi + uvicorn
    uvicorn hospes.api:app --reload  # then the HTTP surface is available

If FastAPI is not installed, importing this module raises a clear
``ApiExtraNotInstalled`` error; :data:`FASTAPI_AVAILABLE` lets callers (and the
test suite) detect the extra without triggering it. All domain logic lives in
:mod:`hospes.service`; this module is a thin adapter that:

* authenticates local process-bound operators or validates Cloudflare Access
  JWTs and resolves their internal scope; browser identity headers are ignored;
* opens a provider-neutral database connection via :mod:`hospes.store`;
* translates :class:`hospes.service.DomainError` to HTTP status codes.

There is deliberately **no send/deliver route** — the system drafts, a human
sends.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import analytics, authentication, branding, clearances, configuration, contact_roster
from . import encryption, guest_crm, guest_portal, jobs, migrations, notifications, partnerships
from . import pilot_service, platform, service, sponsors, store, touchpoints
from . import distribution, network_dashboard, network_graph, providers, research_agent
from .pilot_models import PilotDecisionInput, PilotError

try:  # optional extra
    from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, Security
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when the extra is absent
    FASTAPI_AVAILABLE = False


class ApiExtraNotInstalled(RuntimeError):
    """Raised when the HTTP surface is used without the ``api`` optional extra."""


_INSTALL_HINT = (
    "The HOSPES HTTP API requires the optional 'api' extra. Install it with:\n"
    "    pip install -e '.[api]'\n"
    "The service layer (hospes.service) is fully usable without it."
)

ACTOR_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")


def create_app(
    db_path: str | None = None,
    auth_token: str | None = None,
    *,
    bound_actor: service.HumanActor | None = None,
    raw_v1_enabled: bool = True,
    runtime_kind: str = "authority",
    demo_scenario: str | None = None,
    query_only: bool = False,
    scenario_label: str | None = None,
    access_authenticator: authentication.AccessJWTAuthenticator | None = None,
    csrf_secret: bytes | None = None,
    csrf_required: bool = True,
    csrf_cookie_secure: bool | None = None,
    _test_bearer_authenticator: authentication.StaticBearerAuthenticator | None = None,
    internal_trigger_authenticator: authentication.InternalTriggerAuthenticator | None = None,
    job_handlers: Mapping[str, jobs.JobHandler] | None = None,
    field_vault: encryption.FieldVault | None = None,
    research_transport: Any | None = None,
) -> Any:
    """Build the FastAPI application over the provider-neutral service store.

    ``db_path`` overrides the store location (default: ``out/hospes.sqlite3`` or
    ``HOSPES_DB``). Local authority requires a process-bound actor plus operator
    token. Tunnel/hosted authority requires a validated Access JWT mapped through
    ``identity_mappings``. Browser-supplied actor, role, and tenant headers are
    never consulted.
    """
    if not FASTAPI_AVAILABLE:
        raise ApiExtraNotInstalled(_INSTALL_HINT)

    selected_auth_mode = os.environ.get("HOSPES_AUTH_MODE", "local")
    if selected_auth_mode not in {"local", "cloudflare_access"}:
        raise authentication.AuthenticationConfigurationError("HOSPES_AUTH_MODE must be local or cloudflare_access")
    if access_authenticator is None and selected_auth_mode == "cloudflare_access":
        access_authenticator = authentication.AccessJWTAuthenticator.from_environment()
    if _test_bearer_authenticator is not None and runtime_kind != "synthetic_test":
        raise authentication.AuthenticationConfigurationError(
            "synthetic bearer authentication requires the synthetic_test runtime"
        )
    identity_sources = sum(
        source is not None for source in (bound_actor, access_authenticator, _test_bearer_authenticator)
    )
    if identity_sources > 1:
        raise authentication.AuthenticationConfigurationError("exactly one operator identity source may be configured")
    if not isinstance(csrf_required, bool):
        raise authentication.AuthenticationConfigurationError("CSRF enforcement must be a boolean")
    if not csrf_required and runtime_kind != "synthetic_test":
        raise authentication.AuthenticationConfigurationError(
            "CSRF enforcement may be disabled only in the synthetic_test runtime"
        )
    if access_authenticator is not None and csrf_cookie_secure is False and runtime_kind != "synthetic_test":
        raise authentication.AuthenticationConfigurationError("Access mode requires secure CSRF cookies")
    if access_authenticator is not None and csrf_secret is None:
        csrf_secret = authentication.csrf_secret_from_environment()
    if internal_trigger_authenticator is None and os.environ.get("HOSPES_INTERNAL_JOB_TRIGGER_TOKEN"):
        internal_trigger_authenticator = authentication.InternalTriggerAuthenticator(
            authentication.EnvironmentSecretProvider(
                credential_ref="credential://hospes/internal-job-trigger",
                env_name="HOSPES_INTERNAL_JOB_TRIGGER_TOKEN",
            )
        )

    csrf_protector = authentication.CSRFProtector(csrf_secret if csrf_secret is not None else secrets.token_bytes(32))
    bearer_scheme = HTTPBearer(auto_error=False)
    application = FastAPI(
        title="HOSPES",
        version="1.0.0",
        description=(
            "Human-gated podcast guest operations. This API creates correspondence "
            "drafts; it intentionally has no delivery endpoint."
        ),
    )
    # One long-lived connection is fine for a single-process, local-first store;
    # sqlite serializes writes internally and we opened it check_same_thread=False.
    application.state.db_path = store.resolve_db_path(db_path)
    application.state.conn = store.connect(db_path, query_only=query_only, migrate=not query_only)
    application.state.auth_token = auth_token or os.environ.get("HOSPES_OPERATOR_TOKEN")
    application.state.bound_actor = bound_actor
    application.state.raw_v1_enabled = raw_v1_enabled
    application.state.runtime_kind = runtime_kind
    application.state.demo_scenario = demo_scenario
    application.state.query_only = query_only
    application.state.scenario_label = scenario_label
    application.state.access_authenticator = access_authenticator
    application.state.test_bearer_authenticator = _test_bearer_authenticator
    application.state.csrf_required = csrf_required
    application.state.csrf_cookie_secure = (
        access_authenticator is not None if csrf_cookie_secure is None else bool(csrf_cookie_secure)
    )
    application.state.csrf_protector = csrf_protector
    application.state.internal_trigger_authenticator = internal_trigger_authenticator
    application.state.job_handlers = dict(job_handlers or {})
    application.state.research_transport = research_transport
    # Research is a first-class job type, so the internal trigger drains it too.
    # setdefault keeps an explicitly supplied handler authoritative.
    application.state.job_handlers.setdefault(
        research_agent.JOB_TYPE,
        research_agent.job_handler(application.state.conn, transport=research_transport),
    )
    if field_vault is None and os.environ.get("HOSPES_MASTER_KEY_B64"):
        field_vault = encryption.FieldVault(encryption.TenantKeyManager(encryption.EnvironmentMasterKeyProvider()))
    application.state.field_vault = field_vault
    # The portal signs its own links and publishes them under its own base
    # url. Both are runtime material, so they arrive from the environment
    # rather than tracked configuration, and their absence is a visible 503
    # rather than a silent unsigned link.
    application.state.portal_secret = os.environ.get("HOSPES_PORTAL_SECRET")
    application.state.portal_base_url = os.environ.get("HOSPES_PORTAL_BASE_URL")
    application.router.add_event_handler("shutdown", application.state.conn.close)

    def get_conn(request: Request) -> store.DatabaseConnection:
        return request.app.state.conn

    def get_human_actor(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    ) -> service.HumanActor:
        process_actor: service.HumanActor | None = request.app.state.bound_actor
        if (
            process_actor is not None
            and not request.app.state.raw_v1_enabled
            and not request.scope.get("hospes.operator_proxy")
        ):
            raise HTTPException(status_code=404, detail="raw API is disabled in operator mode")
        expected_token: Optional[str] = request.app.state.auth_token

        def require_local_token() -> None:
            if expected_token is None:
                raise HTTPException(status_code=503, detail="HOSPES_OPERATOR_TOKEN is not configured")
            if (
                credentials is None
                or credentials.scheme.lower() != "bearer"
                or not secrets.compare_digest(credentials.credentials, expected_token)
            ):
                raise HTTPException(status_code=401, detail="valid human authorization is required")

        try:
            access: authentication.AccessJWTAuthenticator | None = request.app.state.access_authenticator
            if access is not None:
                access_assertion = authentication.access_token_from_request(request)
                requested_show = authentication.request_show_scope(request)
                identity = access.authenticate(
                    request.app.state.conn,
                    access_assertion or "",
                    requested_show=requested_show,
                )
                if requested_show is not None:
                    request.scope["hospes.operator_show"] = requested_show
            elif process_actor is not None:
                require_local_token()
                identity = authentication.process_bound_operator(process_actor)
            elif request.app.state.test_bearer_authenticator is not None:
                presented_bearer = (
                    credentials.credentials
                    if credentials is not None and credentials.scheme.lower() == "bearer"
                    else None
                )
                identity = request.app.state.test_bearer_authenticator.authenticate(presented_bearer)
            else:
                raise authentication.AuthenticationConfigurationError(
                    "operator identity is not process-bound or Access-mapped"
                )
            if request.app.state.csrf_required and request.method.upper() in authentication.MUTATING_METHODS:
                csrf_header = request.headers.get(authentication.CSRF_HEADER)
                csrf_cookie = request.cookies.get(authentication.CSRF_COOKIE)
                if not csrf_header or not csrf_cookie or not secrets.compare_digest(csrf_header, csrf_cookie):
                    raise authentication.CSRFDenied()
                request.app.state.csrf_protector.validate(identity, csrf_header)
        except authentication.AuthenticationError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        request.state.hospes_identity = identity
        return identity.as_human_actor()

    # Shared dependency defaults. Using Depends() as a DEFAULT VALUE (rather than
    # an Annotated local alias) keeps them resolvable under
    # `from __future__ import annotations`, where all annotations are strings.
    conn_dep = Depends(get_conn)
    actor_dep = Depends(get_human_actor)

    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (
            service.DomainError,
            partnerships.PartnershipError,
            PilotError,
            analytics.AnalyticsError,
            configuration.ConfigurationError,
            jobs.JobError,
            platform.PlatformError,
            providers.ProviderError,
            contact_roster.ContactRosterError,
            touchpoints.TouchpointError,
            clearances.ClearanceError,
            guest_crm.GuestHistoryError,
            notifications.NotificationError,
            guest_portal.PortalError,
        ) as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    def _identity_show(request: Request) -> str | None:
        identity: authentication.AuthenticatedOperator = request.state.hospes_identity
        active_show = request.scope.get("hospes.operator_show")
        if active_show is not None:
            if (
                not isinstance(active_show, str)
                or not authentication.OPAQUE_ID.fullmatch(active_show)
                or (identity.show_id is not None and identity.show_id != active_show)
            ):
                raise HTTPException(
                    status_code=403,
                    detail="the operator identity is unavailable in this show scope",
                )
            return active_show
        return identity.show_id

    def _require_identity_show(request: Request, show_id: str) -> None:
        scoped_show = _identity_show(request)
        if scoped_show is not None and scoped_show != show_id:
            raise HTTPException(
                status_code=403,
                detail="the operator identity is unavailable in this show scope",
            )

    def _require_opportunity_show(
        request: Request,
        conn: store.DatabaseConnection,
        opportunity_id: str,
        actor: service.HumanActor,
    ) -> dict[str, Any]:
        opportunity = _call(service.get_opportunity, conn, opportunity_id, actor)
        _require_identity_show(request, str(opportunity["show_id"]))
        return opportunity

    def _require_partnership_show(
        request: Request,
        conn: store.DatabaseConnection,
        partnership_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        return _call(
            partnerships._partnership,
            conn,
            partnership_id,
            tenant_id,
            _identity_show(request),
        )

    def _require_network_operator(actor: service.HumanActor) -> str:
        """Admit the network-portfolio routes for the network operator alone.

        The show-scope guard used everywhere else is deliberately not applied
        here: the portfolio is a tenant-wide question, so its scope is the
        authenticated identity's tenant and its gate is the role. Every other
        role is refused, including one that operates a show inside the tenant.
        """
        if actor.role is not service.HumanRole.NETWORK_OPERATOR:
            raise HTTPException(
                status_code=403,
                detail="only a network operator may read the network portfolio",
            )
        return actor.role.value

    def _private_response(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"

    @application.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "auth_configured": bool(
                application.state.access_authenticator is not None
                or (application.state.auth_token is not None and application.state.bound_actor is not None)
            ),
            "auth_mode": (
                "cloudflare_access"
                if application.state.access_authenticator is not None
                else "local_process"
                if application.state.bound_actor is not None
                else "unconfigured"
            ),
        }

    @application.get("/v1/session/csrf")
    def csrf_session(
        request: Request,
        response: Response,
        actor=actor_dep,
    ) -> dict[str, Any]:
        del actor
        identity: authentication.AuthenticatedOperator = request.state.hospes_identity
        csrf_proof = request.app.state.csrf_protector.issue(identity)
        response.set_cookie(
            authentication.CSRF_COOKIE,
            csrf_proof,
            max_age=request.app.state.csrf_protector.max_age_seconds,
            httponly=False,
            secure=bool(request.app.state.csrf_cookie_secure),
            samesite="strict",
            path="/",
        )
        return {
            "csrf_token": csrf_proof,
            "expires_in": request.app.state.csrf_protector.max_age_seconds,
        }

    @application.post("/internal/jobs/run")
    def run_internal_jobs(
        max_jobs: int = 1,
        worker_id: str = "hosted_internal",
        trigger: Optional[str] = Header(default=None, alias="X-Hospes-Internal-Trigger"),
        conn=conn_dep,
    ) -> dict[str, Any]:
        authenticator = application.state.internal_trigger_authenticator
        if authenticator is None:
            raise HTTPException(status_code=503, detail="internal job trigger is not configured")
        try:
            authenticator.verify(trigger)
            completed = jobs.run_available(
                conn,
                worker_id=worker_id,
                handlers=application.state.job_handlers,
                max_jobs=max_jobs,
            )
        except (authentication.AuthenticationError, jobs.JobError) as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        return {
            "processed": len(completed),
            "jobs": [{"job_ref": f"job://{row['id']}", "status": row["status"]} for row in completed],
        }

    @application.get("/v1/operator-context")
    def operator_context(request: Request, conn=conn_dep, actor=actor_dep) -> dict[str, Any]:
        ledger = migrations.applied_migrations(conn)
        active_show = _identity_show(request)
        context = {
            "actor_id": actor.actor_id,
            "role": actor.role.value,
            "tenant_id": actor.tenant_id,
            "show_id": active_show,
            "schema_version": migrations.current_version(conn),
            "last_verification": ledger[-1]["applied_at"] if ledger else None,
            "raw_v1_enabled": bool(application.state.raw_v1_enabled),
            "runtime_kind": application.state.runtime_kind,
            "demo_scenario": application.state.demo_scenario,
            "query_only": bool(application.state.query_only),
            "private_field_custody_configured": application.state.field_vault is not None,
            "guest_portal_configured": _portal_configured(active_show),
            "reply_classifications": sorted(service.REPLY_CLASSIFICATIONS),
            "guest_history_dispositions": sorted(guest_crm.DISPOSITIONS),
        }
        if application.state.runtime_kind == "synthetic_demo":
            context["scenario_label"] = application.state.scenario_label
        else:
            context["database"] = str(application.state.db_path)
        return context

    @application.post("/v1/opportunities", status_code=201)
    def create_opportunity(
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        parsed = _call(service.OpportunityCreate.from_dict, payload)
        _require_identity_show(request, parsed.show_id)
        return _call(service.create_opportunity, conn, parsed, actor)

    @application.get("/v1/opportunities")
    def list_opportunities(
        request: Request,
        state: Optional[str] = None,
        owner: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        return _call(
            service.list_opportunities,
            conn,
            actor.tenant_id,
            state=state,
            owner=owner,
            show_id=_identity_show(request),
        )

    @application.get("/v1/approval-queue")
    def approval_queue(
        request: Request,
        response: Response,
        owner: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        rows = _call(
            service.approval_queue,
            conn,
            actor.tenant_id,
            owner=owner,
            show_id=_identity_show(request),
        )
        vault = application.state.field_vault
        if vault is not None and actor.role.value in contact_roster.AUTHORIZED_CONTACT_ROLES:
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
            for row in rows:
                row["contact_roster"] = _call(
                    contact_roster.reveal_contact_roster,
                    conn,
                    vault,
                    tenant_id=actor.tenant_id,
                    show_id=row["show_id"],
                    opportunity_id=row["id"],
                    actor_role=actor.role.value,
                )
        return rows

    @application.get("/v1/opportunities/{opportunity_id}")
    def opportunity_detail(
        opportunity_id: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        detail = _call(service.opportunity_detail, conn, opportunity_id, actor)
        _require_identity_show(request, detail["show_id"])
        return detail

    @application.get("/v1/opportunities/{opportunity_id}/contact-roster")
    def opportunity_contact_roster(
        opportunity_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        opportunity = _call(service.get_opportunity, conn, opportunity_id, actor)
        _require_identity_show(request, opportunity["show_id"])
        vault = application.state.field_vault
        if vault is None:
            raise HTTPException(status_code=503, detail="contact roster custody is not configured")
        roster = _call(
            contact_roster.reveal_contact_roster,
            conn,
            vault,
            tenant_id=actor.tenant_id,
            show_id=opportunity["show_id"],
            opportunity_id=opportunity_id,
            actor_role=actor.role.value,
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        return {
            "items": roster,
            "invitation_prefill": contact_roster.invitation_prefill(roster),
        }

    def _show_config(show_id: str | None) -> Optional[configuration.ShowConfig]:
        if not show_id:
            return None
        try:
            return configuration.load_show(show_id)
        except configuration.ConfigurationError:
            return None

    def _portal_enabled(show_id: str | None) -> bool:
        """A show opts into the portal; it is never on by default."""
        show = _show_config(show_id)
        return show is not None and show.guest_interaction_mode == "portal"

    def _portal_brand(show_id: str | None) -> Optional[dict[str, Any]]:
        show = _show_config(show_id)
        if show is None or show.brand_ref is None:
            return None
        return _call(branding.load_brand, configuration.show_resource_path(show.brand_ref, "brand"))

    def _portal_configured(show_id: str | None) -> bool:
        """Report whether this show could actually mint a link right now."""
        return bool(
            _portal_enabled(show_id)
            and application.state.portal_secret
            and application.state.portal_base_url
            and application.state.field_vault is not None
        )

    def _portal_secret() -> str:
        secret = application.state.portal_secret  # allow-secret: runtime signing material, not a value
        if not secret:
            raise HTTPException(status_code=503, detail="the guest portal signing secret is not configured")
        return secret

    @application.post("/v1/opportunities/{opportunity_id}/portal-link", status_code=201)
    def opportunity_portal_link(
        opportunity_id: str,
        request: Request,
        response: Response,
        payload: Optional[dict[str, Any]] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        """Mint a one-time guest link. The operator sends it; HOSPES never does."""
        opportunity = _call(service.get_opportunity, conn, opportunity_id, actor)
        _require_identity_show(request, opportunity["show_id"])
        body = payload or {}
        ttl_days = body.get("ttl_days", guest_portal.DEFAULT_LINK_TTL_DAYS)
        if not isinstance(ttl_days, int) or isinstance(ttl_days, bool):
            raise HTTPException(status_code=422, detail="ttl_days must be a whole number of days")
        link = _call(
            guest_portal.link_for_opportunity,
            conn,
            opportunity,
            secret=_portal_secret(),  # allow-secret: runtime signing material, not a value
            base_url=str(body.get("base_url") or application.state.portal_base_url or ""),
            enabled=_portal_enabled(opportunity["show_id"]),
            brand=_portal_brand(opportunity["show_id"]),
            offered_dates=body.get("offered_dates"),
            ttl_days=ttl_days,
            created_by=actor.actor_id,
            created_by_role=actor.role.value,
        )
        # The URL carries the one-time token; it is shown once and never cached.
        _private_response(response)
        return link

    @application.get("/v1/portal-status")
    def portal_status(request: Request, conn=conn_dep, actor=actor_dep) -> list[dict[str, Any]]:
        """Portal state for every guest in the active show — no private values."""
        active_show = _identity_show(request)
        if not active_show:
            raise HTTPException(status_code=403, detail="a portal status read requires an active show scope")
        return _call(guest_portal.show_portal_status, conn, tenant_id=actor.tenant_id, show_id=active_show)

    @application.get("/v1/opportunities/{opportunity_id}/touchpoints")
    def opportunity_touchpoints(
        opportunity_id: str,
        request: Request,
        response: Response,
        channel: Optional[str] = None,
        initiator: Optional[str] = None,
        from_at: Optional[str] = None,
        to_at: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        opportunity = _call(service.get_opportunity, conn, opportunity_id, actor)
        _require_identity_show(request, opportunity["show_id"])
        vault = application.state.field_vault
        if vault is None:
            raise HTTPException(status_code=503, detail="touchpoint note custody is not configured")
        _private_response(response)
        return _call(
            touchpoints.list_touchpoints,
            conn,
            vault,
            tenant_id=actor.tenant_id,
            show_id=opportunity["show_id"],
            opportunity_id=opportunity_id,
            channel=channel,
            initiator=initiator,
            from_at=from_at,
            to_at=to_at,
            limit=limit,
            actor_role=actor.role.value,
        )

    @application.get("/v1/opportunities/{opportunity_id}/guest-history")
    def opportunity_guest_history(
        opportunity_id: str,
        request: Request,
        response: Response,
        season: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        opportunity = _require_opportunity_show(request, conn, opportunity_id, actor)
        _private_response(response)
        return _call(
            service.opportunity_guest_history,
            conn,
            opportunity["id"],
            actor,
            season=season,
            limit=limit,
            field_vault=application.state.field_vault,
        )

    @application.put("/v1/opportunities/{opportunity_id}/do-not-contact")
    def do_not_contact(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.DoNotContactUpdate.from_dict, payload)
        result = _call(service.set_guest_do_not_contact, conn, opportunity_id, parsed, actor)
        _private_response(response)
        return result

    @application.put("/v1/opportunities/{opportunity_id}/thesis-contact")
    def thesis_contact(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.ThesisContactUpdate.from_dict, payload)
        return _call(service.attach_thesis_contact, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/decisions")
    def decision(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.DecisionCreate.from_dict, payload)
        return _call(service.record_decision, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/correspondence-drafts", status_code=201)
    def correspondence_draft(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.DraftCreate.from_dict, payload)
        return _call(service.create_draft, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/draft-preview")
    def draft_preview(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.DraftCreate.from_dict, payload)
        return _call(service.preview_draft, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/studio-routing", status_code=201)
    def studio_routing(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.StudioRoutingCreate.from_dict, payload)
        return _call(service.route_to_studio, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/briefs", status_code=201)
    def episode_brief(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.BriefCreate.from_dict, payload)
        return _call(service.create_brief, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/asset-packages", status_code=201)
    def asset_package(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.AssetPackageCreate.from_dict, payload)
        return _call(service.declare_assets, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/receipts", status_code=201)
    def operational_receipt(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.ReceiptCreate.from_dict, payload)
        return _call(service.record_receipt, conn, opportunity_id, parsed, actor)

    @application.post("/v1/opportunities/{opportunity_id}/commitments", status_code=201)
    def commitment(
        opportunity_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_opportunity_show(request, conn, opportunity_id, actor)
        parsed = _call(service.CommitmentCreate.from_dict, payload)
        return _call(service.create_commitment, conn, opportunity_id, parsed, actor)

    @application.get("/v1/followups")
    def followups(
        request: Request,
        due_before: datetime,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        if due_before.tzinfo is None or due_before.utcoffset() is None:
            raise HTTPException(status_code=422, detail="due_before must include a timezone")
        return service.list_followups(
            conn,
            actor.tenant_id,
            due_before.astimezone(timezone.utc),
            show_id=_identity_show(request),
        )

    @application.post("/v1/commitments/{commitment_id}/complete")
    def complete_commitment(
        commitment_id: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        commitment = store.fetch_one(conn, "SELECT opportunity_id FROM commitments WHERE id = ?", (commitment_id,))
        if commitment is None:
            raise HTTPException(status_code=404, detail="commitment not found")
        _require_opportunity_show(request, conn, str(commitment["opportunity_id"]), actor)
        return _call(service.complete_commitment, conn, commitment_id, actor)

    @application.get("/v1/partnerships")
    def list_partnerships(request: Request, conn=conn_dep, actor=actor_dep) -> list[dict[str, Any]]:
        return partnerships.list_partnerships(conn, actor.tenant_id, _identity_show(request))

    @application.get("/v1/partnerships/{partnership_id}/command-center")
    def partnership_command_center(
        partnership_id: str,
        request: Request,
        surface: str | None = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        value = _call(
            partnerships.command_center,
            conn,
            partnership_id,
            actor.tenant_id,
            _identity_show(request),
        )
        if surface is None:
            return value
        if surface != "overview":
            raise HTTPException(status_code=422, detail="command-center surface must be overview")
        return {
            key: value[key]
            for key in (
                "partnership",
                "summary",
                "coverage",
                "engine_capabilities",
                "agenda",
            )
        } | {"recent_events": value["recent_events"][:12]}

    @application.post("/v1/partnerships/{partnership_id}/items", status_code=201)
    def partnership_item(
        partnership_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        parsed = _call(partnerships.PartnershipItemInput.from_dict, payload)
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            partnerships.create_item,
            conn,
            partnership_id,
            parsed,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.put("/v1/partnerships/{partnership_id}/items/{item_id}")
    def revise_partnership_item(
        partnership_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        expected_revision = payload.get("expected_revision")
        item_payload = {key: value for key, value in payload.items() if key != "expected_revision"}
        parsed = _call(partnerships.PartnershipItemInput.from_dict, item_payload)
        return _call(
            partnerships.update_item,
            conn,
            partnership_id,
            item_id,
            parsed,
            expected_revision=expected_revision,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/partnerships/{partnership_id}/items/{item_id}/supersede")
    def supersede_partnership_item(
        partnership_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            partnerships.supersede_item,
            conn,
            partnership_id,
            item_id,
            expected_revision=payload.get("expected_revision"),
            successor_item_id=payload.get("successor_item_id"),
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/partnerships/{partnership_id}/resources", status_code=201)
    def partnership_resource(
        partnership_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            partnerships.link_resource,
            conn,
            partnership_id,
            payload,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/partnerships/{partnership_id}/reviews", status_code=201)
    def partnership_review(
        partnership_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            partnerships.record_review,
            conn,
            partnership_id,
            payload,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.put("/v1/partnerships/{partnership_id}/pilot-slots/{slot}")
    def pilot_slot(
        partnership_id: str,
        slot: int,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            partnerships.select_pilot_candidate,
            conn,
            partnership_id,
            payload.get("opportunity_id"),
            slot,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/partnerships/{partnership_id}/pilot-runs", status_code=201)
    def start_pilot_run(
        partnership_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            pilot_service.start_run,
            conn,
            partnership_id,
            payload,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.get("/v1/partnerships/{partnership_id}/pilot-runs/{run_id}/plan")
    def pilot_plan(
        partnership_id: str,
        run_id: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            pilot_service.get_plan,
            conn,
            partnership_id,
            run_id,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/partnerships/{partnership_id}/pilot-runs/{run_id}/decisions")
    def pilot_decision(
        partnership_id: str,
        run_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        parsed = _call(PilotDecisionInput.from_mapping, payload)
        _require_partnership_show(request, conn, partnership_id, actor.tenant_id)
        return _call(
            pilot_service.record_decision,
            conn,
            partnership_id,
            run_id,
            parsed,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.get("/v1/capabilities")
    def capabilities(actor=actor_dep) -> list[dict[str, Any]]:
        return [item.__dict__ for item in configuration.capability_report()]

    @application.get("/v1/shows")
    def shows(request: Request, conn=conn_dep, actor=actor_dep) -> list[dict[str, Any]]:
        rows = _call(platform.list_shows, conn, tenant_id=actor.tenant_id)
        identity: authentication.AuthenticatedOperator = request.state.hospes_identity
        if identity.show_id is not None:
            rows = [row for row in rows if row["show_id"] == identity.show_id]
        configured = {
            show.show_id: configuration.show_profile(show) for show in configuration.list_show_configs(actor.tenant_id)
        }
        active_show = _identity_show(request)
        return [
            {
                **row,
                "active": row["show_id"] == active_show,
                "profile": configured.get(row["show_id"]),
            }
            for row in rows
        ]

    @application.get("/v1/shows/{show_id}/suggestions")
    def suggestions(
        show_id: str,
        request: Request,
        network_id: Optional[str] = None,
        relationship_class: Optional[str] = None,
        max_social_cost: Optional[int] = None,
        limit: int = 20,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        return _call(
            platform.suggest_guests,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            network_id=network_id,
            relationship_class=relationship_class,
            max_social_cost=max_social_cost,
            limit=limit,
        )

    @application.get("/v1/shows/{show_id}/touchpoints")
    def show_touchpoints(
        show_id: str,
        request: Request,
        response: Response,
        guest_id: Optional[str] = None,
        opportunity_id: Optional[str] = None,
        partnership_id: Optional[str] = None,
        channel: Optional[str] = None,
        initiator: Optional[str] = None,
        from_at: Optional[str] = None,
        to_at: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        vault = application.state.field_vault
        if vault is None:
            raise HTTPException(status_code=503, detail="touchpoint note custody is not configured")
        _private_response(response)
        return _call(
            touchpoints.list_touchpoints,
            conn,
            vault,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            guest_id=guest_id,
            opportunity_id=opportunity_id,
            partnership_id=partnership_id,
            channel=channel,
            initiator=initiator,
            from_at=from_at,
            to_at=to_at,
            limit=limit,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/touchpoints", status_code=201)
    def touchpoint(
        show_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        vault = application.state.field_vault
        if vault is None:
            raise HTTPException(status_code=503, detail="touchpoint note custody is not configured")
        result = _call(
            partnerships.record_informal_touchpoint,
            conn,
            payload,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            field_vault=vault,
        )
        _private_response(response)
        return result

    @application.get("/v1/shows/{show_id}/guest-history")
    def show_guest_history(
        show_id: str,
        request: Request,
        response: Response,
        guest_id: Optional[str] = None,
        opportunity_id: Optional[str] = None,
        season: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            service.show_guest_history,
            conn,
            show_id,
            actor,
            guest_id=guest_id,
            opportunity_id=opportunity_id,
            season=season,
            limit=limit,
            field_vault=application.state.field_vault,
        )

    @application.post("/v1/shows/{show_id}/guest-history", status_code=201)
    def record_guest_history(
        show_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        result = _call(
            service.record_guest_history,
            conn,
            show_id,
            payload,
            actor,
            field_vault=application.state.field_vault,
        )
        _private_response(response)
        return result

    @application.get("/v1/shows/{show_id}/network-map")
    def network_map(
        show_id: str,
        guest: str,
        request: Request,
        response: Response,
        depth: int = 2,
        format: str = "json",
        target: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> Any:
        _require_identity_show(request, show_id)
        selected_format = format.strip().lower()
        value = _call(
            network_graph.graph,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            root_guest_id=guest,
            depth=depth,
            target_guest_ids=[target] if target else None,
        )
        _private_response(response)
        if selected_format == "json":
            return value
        rendered = _call(network_graph.render_graph, value, selected_format)
        media_type = {
            "csv": "text/csv",
            "mermaid": "text/plain",
            "graphviz": "text/vnd.graphviz",
            "dot": "text/vnd.graphviz",
        }.get(selected_format, "text/plain")
        return Response(
            content=rendered,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
            },
        )

    @application.get("/v1/shows/{show_id}/notifications")
    def notification_center(
        show_id: str,
        request: Request,
        unread_only: bool = False,
        notification_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.list_notifications,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            recipient_role=actor.role.value,
            unread_only=unread_only,
            notification_type=notification_type,
            severity=severity,
            limit=limit,
        )

    @application.get("/v1/shows/{show_id}/notifications/summary")
    def notification_summary(
        show_id: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.summary,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            recipient_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/notifications/read-all")
    def notification_read_all(
        show_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.mark_all_read,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            actor_id=actor.actor_id,
            notification_type=payload.get("notification_type"),
        )

    @application.post("/v1/shows/{show_id}/notifications/{notification_id}/read")
    def notification_read(
        show_id: str,
        notification_id: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.mark_read,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            notification_id=notification_id,
            actor_role=actor.role.value,
            actor_id=actor.actor_id,
        )

    @application.post(
        "/v1/shows/{show_id}/notifications/{notification_id}/previews",
        status_code=201,
    )
    def notification_preview(
        show_id: str,
        notification_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        result = _call(
            notifications.preview_delivery,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            notification_id=notification_id,
            channel=payload.get("channel"),
            target_ref=payload.get("target_ref"),
            idempotency_key=payload.get("idempotency_key"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )
        _private_response(response)
        return result

    @application.get("/v1/shows/{show_id}/notification-previews")
    def notification_previews(
        show_id: str,
        request: Request,
        notification_id: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.list_deliveries,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            notification_id=notification_id,
            limit=limit,
        )

    @application.post("/v1/shows/{show_id}/notification-previews/{preview_id}/authorize")
    def authorize_notification_preview(
        show_id: str,
        preview_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        show = _call(configuration.load_show, show_id)
        return _call(
            notifications.authorize_delivery,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            delivery_id=preview_id,
            outbound_mode=show.outbound_mode,
            authorized_by=actor.actor_id,
            authorization_ref=payload.get("authorization_ref"),
            idempotency_key=payload.get("idempotency_key"),
        )

    @application.post(
        "/v1/shows/{show_id}/notification-previews/{preview_id}/receipts",
        status_code=201,
    )
    def record_notification_receipt(
        show_id: str,
        preview_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.record_delivery_receipt,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            delivery_id=preview_id,
            receipt_ref=payload.get("receipt_ref"),
        )

    @application.get("/v1/shows/{show_id}/my-queue")
    def my_queue(
        show_id: str,
        request: Request,
        include_done: bool = False,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.my_queue,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            actor_id=actor.actor_id,
            include_done=include_done,
            limit=limit,
        )

    @application.get("/v1/shows/{show_id}/queue-tasks")
    def list_queue_tasks(
        show_id: str,
        request: Request,
        assignee_role: Optional[str] = None,
        status: Optional[str] = None,
        include_done: bool = False,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.list_assignments,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            assignee_role=assignee_role,
            status=status,
            include_done=include_done,
            limit=limit,
        )

    @application.post("/v1/shows/{show_id}/queue-tasks", status_code=201)
    def create_queue_task(
        show_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.assign,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            entity_ref=payload.get("entity_ref"),
            assignment_type=payload.get("assignment_type"),
            assignee_role=payload.get("assignee_role"),
            title=payload.get("title"),
            assignee_actor_id=payload.get("assignee_actor_id"),
            due_at=payload.get("due_at"),
            created_by=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/queue-tasks/{task_id}/complete")
    def complete_queue_task(
        show_id: str,
        task_id: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            notifications.complete_assignment,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            assignment_id=task_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.get("/v1/shows/{show_id}/clearances")
    def clearance_list(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        status: Optional[str] = None,
        clearance_type: Optional[str] = None,
        due_before: Optional[str] = None,
        blocking_only: bool = False,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        rows = _call(
            clearances.list_clearances,
            conn,
            application.state.field_vault,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
            status=status,
            clearance_type=clearance_type,
            due_before=due_before,
            blocking_only=blocking_only,
            limit=limit,
        )
        _private_response(response)
        return rows

    def _sponsor_payload(payload: Any) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=422, detail="request payload must be an object")
        return payload

    @application.get("/v1/shows/{show_id}/sponsors")
    def list_show_sponsors(
        show_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.list_sponsors,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/sponsors", status_code=201)
    def create_show_sponsor(
        show_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        body = _sponsor_payload(payload)
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.create_sponsor,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            name=body.get("name"),
            contact_ref=body.get("contact_ref", body.get("contact")),
            terms_ref=body.get("terms_ref", body.get("terms")),
            category=body.get("category"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/sponsors/{sponsor_id}/claims", status_code=201)
    def create_sponsor_claim(
        show_id: str,
        sponsor_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        body = _sponsor_payload(payload)
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.record_claim,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            sponsor_id=sponsor_id,
            claim=body.get("claim"),
            source_url=body.get("source_url"),
            verified_date=body.get("verified_date"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/sponsors/{sponsor_id}/claims/{claim_id}/approval")
    def approve_sponsor_claim(
        show_id: str,
        sponsor_id: str,
        claim_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        body = _sponsor_payload(payload)
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.approve_claim,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            sponsor_id=sponsor_id,
            claim_id=claim_id,
            approved=body.get("approved", True),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.get("/v1/shows/{show_id}/ad-slots")
    def list_ad_slots(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.list_slots,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
        )

    @application.put("/v1/shows/{show_id}/ad-slots")
    def declare_ad_slots(
        show_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        body = _sponsor_payload(payload)
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.declare_slots,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=body.get("episode_id"),
            slots=body.get("slots", []),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/ad-slots/allocations", status_code=201)
    def assign_ad_slot(
        show_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        body = _sponsor_payload(payload)
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.assign_slot,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=body.get("episode_id"),
            slot_type=body.get("slot_type"),
            sponsor_id=body.get("sponsor_id"),
            status=body.get("status", "sold"),
            rate_minor=body.get("rate_minor"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/ad-slots/releases")
    def release_ad_slot(
        show_id: str,
        payload: dict[str, Any],
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        body = _sponsor_payload(payload)
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.release_slot,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=body.get("episode_id"),
            slot_type=body.get("slot_type"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.get("/v1/shows/{show_id}/revenue")
    def show_revenue(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        format: str = "json",
        conn=conn_dep,
        actor=actor_dep,
    ) -> Any:
        _require_identity_show(request, show_id)
        selected_format = format.strip().lower()
        if selected_format not in {"json", "csv"}:
            raise HTTPException(status_code=422, detail="format must be json or csv")
        if selected_format == "csv":
            rendered = _call(
                sponsors.accounting_csv,
                conn,
                tenant_id=actor.tenant_id,
                show_id=show_id,
                actor_role=actor.role.value,
                episode_id=episode_id,
            )
            return Response(
                content=rendered,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Cache-Control": "no-store, private",
                    "Pragma": "no-cache",
                    "Content-Disposition": 'attachment; filename="hospes-revenue.csv"',
                },
            )
        _private_response(response)
        return _call(
            sponsors.revenue_report,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
        )

    @application.get("/v1/shows/{show_id}/sponsorship-receipts")
    def sponsorship_receipts(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            sponsors.list_receipts,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
            limit=limit,
        )

    @application.get("/v1/shows/{show_id}/episodes/{episode_id}/publication-gate")
    def episode_publication_gate(
        show_id: str,
        episode_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _call(
            sponsors.list_slots,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
        )
        blockers = _call(
            platform.publish_blockers,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=episode_id,
        )
        _private_response(response)
        return {
            "show_id": show_id,
            "episode_id": episode_id,
            "publishable": not blockers,
            "blockers": blockers,
        }

    @application.post("/v1/shows/{show_id}/clearances", status_code=201)
    def clearance(
        show_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        parsed = _call(clearances.ClearanceInput.from_mapping, payload)
        result = _call(
            clearances.record_clearance,
            conn,
            application.state.field_vault,
            parsed,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )
        _private_response(response)
        return result

    @application.post("/v1/shows/{show_id}/clearances/{clearance_id}/decision")
    def clearance_decision(
        show_id: str,
        clearance_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        parsed = _call(clearances.ClearanceDecision.from_mapping, payload)
        result = _call(
            clearances.decide_clearance,
            conn,
            application.state.field_vault,
            parsed,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            clearance_id=clearance_id,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )
        _private_response(response)
        return result

    @application.get("/v1/shows/{show_id}/clearances/{clearance_id}/receipts")
    def clearance_receipt_timeline(
        show_id: str,
        clearance_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        rows = _call(
            clearances.clearance_receipts,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            clearance_id=clearance_id,
        )
        _private_response(response)
        return rows

    @application.get("/v1/shows/{show_id}/clearance-badges")
    def clearance_badges(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        rows = _call(
            clearances.episode_clearance_summary,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
        )
        _private_response(response)
        return rows

    @application.get("/v1/shows/{show_id}/clearance-report")
    def clearance_report(
        show_id: str,
        request: Request,
        episode_id: Optional[str] = None,
        status: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> Any:
        _require_identity_show(request, show_id)
        rendered = _call(
            clearances.clearance_report_csv,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
            status=status,
        )
        return Response(
            content=rendered,
            media_type="text/csv",
            headers={
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
                "Content-Disposition": 'attachment; filename="clearance-report.csv"',
            },
        )

    @application.get("/v1/shows/{show_id}/distributions")
    def list_distributions(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        platform_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.list_distributions,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
            platform_name=platform_name,
            status=status,
            limit=limit,
        )

    @application.post("/v1/shows/{show_id}/distributions", status_code=201)
    def distribution_draft(
        show_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.create_draft,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=payload.get("episode_id"),
            platform_name=payload.get("platform"),
            metadata=payload.get("metadata", {}),
            idempotency_key=payload.get("idempotency_key"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/distributions/preview")
    def distribution_preview(
        show_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        # A preview renders the package the operator is about to hand a
        # platform. It reads no record and writes none, so it needs no store.
        return _call(
            distribution.preview,
            payload.get("platform"),
            payload.get("metadata", {}),
        )

    @application.post("/v1/shows/{show_id}/distributions/{distribution_id}/authorize")
    def authorize_distribution(
        show_id: str,
        distribution_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        show = _call(configuration.load_show, show_id)
        _private_response(response)
        return _call(
            distribution.authorize_publish,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            distribution_id=distribution_id,
            outbound_mode=show.outbound_mode,
            authorized_by=actor.actor_id,
            authorization_ref=payload.get("authorization_ref"),
            idempotency_key=payload.get("idempotency_key"),
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/distributions/{distribution_id}/schedule")
    def schedule_distribution(
        show_id: str,
        distribution_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.schedule_publication,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            distribution_id=distribution_id,
            scheduled_at=payload.get("scheduled_at"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/distributions/{distribution_id}/publication-receipt")
    def record_publication_receipt(
        show_id: str,
        distribution_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.mark_published,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            distribution_id=distribution_id,
            external_id_ref=payload.get("external_id_ref", payload.get("external_id")),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/distributions/{distribution_id}/failures", status_code=201)
    def fail_distribution(
        show_id: str,
        distribution_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.record_failure,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            distribution_id=distribution_id,
            error_ref=payload.get("error_ref"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    @application.get("/v1/shows/{show_id}/distributions/{distribution_id}/receipts")
    def distribution_receipt_timeline(
        show_id: str,
        distribution_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.distribution_receipts,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            distribution_id=distribution_id,
        )

    @application.post("/v1/shows/{show_id}/clips", status_code=201)
    def queue_distribution_clip(
        show_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.queue_clip,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=payload.get("episode_id"),
            start_seconds=payload.get("start_seconds"),
            end_seconds=payload.get("end_seconds"),
            platform_name=payload.get("target_platform", payload.get("platform")),
            caption_ref=payload.get("caption_ref"),
            idempotency_key=payload.get("idempotency_key"),
            hashtags=payload.get("hashtags"),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
        )

    # The board and the receipt are named for the *record*, never the act: no
    # HOSPES route may carry an action verb it does not perform, which
    # tests/test_api.py asserts over the whole OpenAPI path set.
    @application.get("/v1/shows/{show_id}/distribution-board")
    def distribution_board(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.publication_board,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
            episode_id=episode_id,
        )

    @application.post("/v1/shows/{show_id}/distribution-adapters/verification", status_code=201)
    def verify_distribution_adapters(
        show_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            distribution.record_adapter_verification,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=actor.role.value,
        )

    @application.post("/v1/shows/{show_id}/research", status_code=201)
    def research_start(
        show_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            research_agent.start_job,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            guest_id=payload.get("guest_id"),
            actor_role=actor.role.value,
            provider=payload.get("provider", research_agent.MANUAL_PROVIDER),
            max_cost_minor=payload.get("max_cost_minor"),
            query=payload.get("query"),
            seeds=payload.get("seeds", []),
            requested_by=actor.actor_id,
        )

    @application.get("/v1/shows/{show_id}/research")
    def research_queue(
        show_id: str,
        request: Request,
        response: Response,
        guest_id: Optional[str] = None,
        status: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        value = _call(
            research_agent.list_jobs,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            guest_id=guest_id,
            status=status,
        )
        _private_response(response)
        return value

    @application.get("/v1/shows/{show_id}/research/{job_id}")
    def research_detail(
        show_id: str,
        job_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        value = _call(
            research_agent.get_job,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            job_id=job_id,
        )
        _private_response(response)
        return value

    @application.post("/v1/shows/{show_id}/research/{job_id}/run")
    def research_run(
        show_id: str,
        job_id: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            research_agent.run_job,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            job_id=job_id,
            worker_id=f"operator-{actor.actor_id}",
            actor_role=actor.role.value,
            transport=application.state.research_transport,
        )

    @application.post("/v1/shows/{show_id}/research/{job_id}/complete")
    def research_complete(
        show_id: str,
        job_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            research_agent.complete_job,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            job_id=job_id,
            actor_role=actor.role.value,
            brief=payload.get("brief", {}),
            citations=payload.get("citations", []),
            counterarguments=payload.get("counterarguments", []),
            cost_minor=int(payload.get("cost_minor", 0)),
        )

    @application.post("/v1/shows/{show_id}/research/{job_id}/annotate")
    def research_annotate(
        show_id: str,
        job_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            research_agent.annotate_job,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            job_id=job_id,
            actor_role=actor.role.value,
            verified_claim_indexes=payload.get("verified_claim_indexes", []),
            counterarguments=payload.get("counterarguments", []),
            segment_candidates=payload.get("segment_candidates", []),
            risk_flags=payload.get("risk_flags", []),
        )

    @application.post("/v1/shows/{show_id}/research/{job_id}/review")
    def research_review(
        show_id: str,
        job_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            research_agent.review_job,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            job_id=job_id,
            actor_role=actor.role.value,
            reviewer_id=actor.actor_id,
            review_ref=payload.get("review_ref"),
            approved=payload.get("approved"),
        )

    @application.post("/v1/shows/{show_id}/research/{job_id}/lock")
    def research_lock(
        show_id: str,
        job_id: str,
        request: Request,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        return _call(
            research_agent.lock_job,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            job_id=job_id,
            actor_role=actor.role.value,
            lock_ref=payload.get("lock_ref"),
        )

    # --- Analytics: read-only provider metrics, trends, receipts, export ----
    # There is deliberately no analytics write route beyond validated provider
    # imports and bounded reads: analytics observes distribution, it never
    # performs it.

    @application.get("/v1/shows/{show_id}/analytics")
    def analytics_metrics(
        show_id: str,
        request: Request,
        response: Response,
        episode_id: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            analytics.list_metrics,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=episode_id,
        )

    @application.get("/v1/shows/{show_id}/analytics/trends")
    def analytics_trends(
        show_id: str,
        request: Request,
        response: Response,
        limit: int = 12,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(analytics.trends, conn, tenant_id=actor.tenant_id, show_id=show_id, limit=limit)

    @application.get("/v1/shows/{show_id}/analytics/receipts")
    def analytics_receipts(
        show_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(analytics.list_receipts, conn, tenant_id=actor.tenant_id, show_id=show_id)

    @application.get("/v1/shows/{show_id}/analytics/export.csv")
    def analytics_export(
        show_id: str,
        request: Request,
        episode_id: Optional[str] = None,
        conn=conn_dep,
        actor=actor_dep,
    ) -> Any:
        _require_identity_show(request, show_id)
        body = _call(
            analytics.export_csv,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            episode_id=episode_id,
        )
        export = Response(content=body, media_type="text/csv; charset=utf-8")
        export.headers["Content-Disposition"] = f'attachment; filename="analytics-{show_id}.csv"'
        _private_response(export)
        return export

    @application.post("/v1/shows/{show_id}/analytics/imports", status_code=201)
    def analytics_import(
        show_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise HTTPException(status_code=422, detail="analytics import requires a non-empty rows list")
        written = _call(
            analytics.import_rows,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            provider=str(payload.get("provider", "")),
            rows=rows,
            source_receipt_ref=str(payload.get("source_receipt_ref", "")),
        )
        return {"imported": len(written), "metrics": written}

    @application.post("/v1/shows/{show_id}/analytics/fetches", status_code=201)
    def analytics_fetch(
        show_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            analytics.fetch,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            provider=payload.get("provider"),
        )

    @application.post("/v1/shows/{show_id}/analytics/schedules", status_code=201)
    def analytics_schedule(
        show_id: str,
        request: Request,
        response: Response,
        payload: dict[str, Any],
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        _require_identity_show(request, show_id)
        _private_response(response)
        return _call(
            analytics.schedule_fetch,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            created_by=actor.actor_id,
            provider=payload.get("provider"),
            period=payload.get("period"),
        )

    # --- Network portfolio: cross-show reads for the network operator -------
    # Scope is the authenticated identity's tenant, never a path or payload,
    # and the gate is the role rather than the active show: the portfolio is
    # the one surface whose whole purpose is to span shows.

    @application.get("/v1/network/portfolio")
    def network_portfolio(
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        role = _require_network_operator(actor)
        _private_response(response)
        return _call(network_dashboard.portfolio, conn, tenant_id=actor.tenant_id, actor_role=role)

    @application.get("/v1/network/shows/{show_id}")
    def network_show(
        show_id: str,
        request: Request,
        response: Response,
        conn=conn_dep,
        actor=actor_dep,
    ) -> dict[str, Any]:
        role = _require_network_operator(actor)
        _private_response(response)
        return _call(
            network_dashboard.show_detail,
            conn,
            tenant_id=actor.tenant_id,
            show_id=show_id,
            actor_role=role,
        )

    @application.get("/v1/network/receipts")
    def network_receipts(
        request: Request,
        response: Response,
        limit: int = 100,
        conn=conn_dep,
        actor=actor_dep,
    ) -> list[dict[str, Any]]:
        role = _require_network_operator(actor)
        _private_response(response)
        return _call(
            network_dashboard.list_report_receipts,
            conn,
            tenant_id=actor.tenant_id,
            actor_role=role,
            limit=limit,
        )

    @application.get("/v1/network/report.{report_format}")
    def network_report(
        report_format: str,
        request: Request,
        conn=conn_dep,
        actor=actor_dep,
    ) -> Any:
        role = _require_network_operator(actor)
        if report_format not in network_dashboard.REPORT_FORMATS:
            raise HTTPException(
                status_code=404,
                detail=f"report format must be one of {', '.join(network_dashboard.REPORT_FORMATS)}",
            )
        body = _call(
            network_dashboard.export_health_report,
            conn,
            tenant_id=actor.tenant_id,
            actor_role=role,
            actor_id=actor.actor_id,
            format=report_format,
        )
        media_type = {
            "html": "text/html; charset=utf-8",
            "json": "application/json",
            "pdf": "application/pdf",
        }[report_format]
        export = Response(content=body, media_type=media_type)
        export.headers["Content-Disposition"] = (
            f'attachment; filename="network-health-{actor.tenant_id}.{report_format}"'
        )
        _private_response(export)
        return export

    return application


if FASTAPI_AVAILABLE:
    # A module-level app for `uvicorn hospes.api:app`. Only built when the extra
    # is present so bare `import hospes.api` never fails without fastapi.
    app = create_app()
