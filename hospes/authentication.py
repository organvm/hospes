"""Process-bound and Cloudflare Access authentication primitives.

Browser-supplied actor, role, and tenant headers are never authority. Local
operator mode binds identity to the process. Tunnel and hosted modes validate
the Access application JWT at the origin and resolve its hashed subject through
the internal identity mapping table.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

from . import generation, service, store
from .configuration import CREDENTIAL_REF

try:  # supplied by the hosted extra
    import jwt
except ImportError:  # pragma: no cover - optional dependency boundary
    jwt = None


ACCESS_PROVIDER = "cloudflare_access"
ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
ACCESS_COOKIE = "CF_Authorization"
CSRF_HEADER = "X-Hospes-CSRF"
CSRF_COOKIE = "hospes_csrf"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
OPAQUE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
ACTOR_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
KID = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")
TEAM_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$"
)
AUDIENCE = re.compile(r"^[A-Za-z0-9._:-]{8,256}$")
OPAQUE_REFERENCE = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s]{3,512}$")
MAX_JWKS_BYTES = 256 * 1024


class AuthenticationError(RuntimeError):
    """Authentication failure safe to expose without token or identity data."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class AuthenticationConfigurationError(AuthenticationError):
    def __init__(self, detail: str):
        super().__init__(503, detail)


class AuthenticationDenied(AuthenticationError):
    def __init__(self, detail: str = "valid operator authentication is required"):
        super().__init__(401, detail)


class AuthenticationScopeDenied(AuthenticationError):
    def __init__(self, detail: str = "operator identity is unavailable in this scope"):
        super().__init__(403, detail)


class CSRFDenied(AuthenticationError):
    def __init__(self, detail: str = "a valid CSRF token is required"):
        super().__init__(403, detail)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 1024:
        raise CSRFDenied()
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            (value + padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except Exception as exc:
        raise CSRFDenied() from exc


def subject_hash(subject: str, *, provider: str = ACCESS_PROVIDER) -> str:
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 512:
        raise AuthenticationDenied()
    return hashlib.sha256(f"{provider}\0{subject}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthenticatedOperator:
    actor_id: str
    role: service.HumanRole
    tenant_id: str
    show_id: str | None
    provider: str
    subject_digest: str
    mapping_id: str | None = None

    def as_human_actor(self) -> service.HumanActor:
        return service.HumanActor(
            actor_id=self.actor_id,
            role=self.role,
            tenant_id=self.tenant_id,
        )


@dataclass(frozen=True)
class StaticBearerAuthenticator:
    """Synthetic-test bearer mapping; identity is token-bound, never header-derived."""

    identities: Mapping[str, service.HumanActor]

    def authenticate(self, presented_bearer: str | None) -> AuthenticatedOperator:
        if not isinstance(presented_bearer, str) or len(presented_bearer) < 16:
            raise AuthenticationDenied()
        actor = next(
            (
                candidate
                for expected, candidate in self.identities.items()
                if secrets.compare_digest(presented_bearer, expected)
            ),
            None,
        )
        if actor is None:
            raise AuthenticationDenied()
        identity = process_bound_operator(actor)
        return AuthenticatedOperator(
            actor_id=identity.actor_id,
            role=identity.role,
            tenant_id=identity.tenant_id,
            show_id=None,
            provider="synthetic_test_bearer",
            subject_digest=identity.subject_digest,
        )


def process_bound_operator(actor: service.HumanActor) -> AuthenticatedOperator:
    return AuthenticatedOperator(
        actor_id=actor.actor_id,
        role=actor.role,
        tenant_id=actor.tenant_id,
        show_id=None,
        provider="local_process",
        subject_digest=subject_hash(
            f"{actor.tenant_id}:{actor.actor_id}", provider="local_process"
        ),
    )


def register_identity_mapping(
    conn: store.DatabaseConnection,
    *,
    subject: str,
    tenant_id: str,
    actor_id: str,
    actor_role: str | service.HumanRole,
    provisioner: service.HumanActor,
    show_id: str | None = None,
    provider: str = ACCESS_PROVIDER,
) -> dict[str, Any]:
    """Persist only a one-way subject digest after an attributable provisioning step."""
    if provider != ACCESS_PROVIDER:
        raise AuthenticationConfigurationError("identity provider is unsupported")
    if not OPAQUE_ID.fullmatch(str(tenant_id)):
        raise ValueError("tenant_id must be an opaque identifier")
    if not ACTOR_ID.fullmatch(str(actor_id)):
        raise ValueError("actor_id must be an opaque identifier")
    if (
        provisioner.role is not service.HumanRole.NETWORK_OPERATOR
        or provisioner.tenant_id != tenant_id
    ):
        raise AuthenticationScopeDenied(
            "identity mappings require a same-tenant network operator"
        )
    normalized_show = None
    if show_id is not None:
        if not OPAQUE_ID.fullmatch(str(show_id)):
            raise ValueError("show_id must be an opaque identifier")
        normalized_show = str(show_id)
    try:
        role = service.HumanRole(actor_role)
    except ValueError as exc:
        raise ValueError("actor_role is unsupported") from exc
    tenant_show = store.fetch_one(
        conn,
        "SELECT tenant_id, show_id FROM show_registry WHERE tenant_id = ? "
        + ("AND show_id = ?" if normalized_show else "LIMIT 1"),
        (tenant_id, normalized_show) if normalized_show else (tenant_id,),
    )
    if tenant_show is None:
        raise AuthenticationScopeDenied("identity mapping requires a registered tenant scope")
    digest = subject_hash(subject, provider=provider)
    existing = store.fetch_one(
        conn,
        "SELECT * FROM identity_mappings WHERE provider = ? AND subject_hash = ? "
        "AND show_id IS ?",
        (provider, digest, normalized_show),
    )
    if existing is not None:
        if (
            existing["tenant_id"] != tenant_id
            or existing["actor_id"] != actor_id
            or existing["actor_role"] != role.value
            or existing["revoked_at"] is not None
        ):
            raise AuthenticationScopeDenied("identity mapping conflicts with existing authority")
        return existing
    record = {
        "id": generation.new_id("identity_mapping"),
        "tenant_id": str(tenant_id),
        "show_id": normalized_show,
        "provider": provider,
        "subject_hash": digest,
        "actor_id": str(actor_id),
        "actor_role": role.value,
        "provisioned_by": provisioner.actor_id,
        "created_at": generation.now().isoformat(),
        "revoked_at": None,
        "revoked_by": None,
        "revocation_ref": None,
    }
    store.insert(conn, "identity_mappings", record)
    conn.commit()
    return record


def revoke_identity_mapping(
    conn: store.DatabaseConnection,
    *,
    mapping_id: str,
    revoker: service.HumanActor,
    revocation_ref: str,
) -> dict[str, Any]:
    """Revoke one mapping under same-tenant network-operator authority."""
    if not OPAQUE_ID.fullmatch(str(mapping_id)):
        raise ValueError("mapping_id must be an opaque identifier")
    if not OPAQUE_REFERENCE.fullmatch(str(revocation_ref)):
        raise ValueError("revocation_ref must be an opaque reference")
    row = store.fetch_one(
        conn,
        "SELECT * FROM identity_mappings WHERE id = ?",
        (mapping_id,),
    )
    if row is None:
        raise AuthenticationScopeDenied("identity mapping is unavailable")
    if (
        revoker.role is not service.HumanRole.NETWORK_OPERATOR
        or revoker.tenant_id != row["tenant_id"]
    ):
        raise AuthenticationScopeDenied(
            "identity mapping revocation requires a same-tenant network operator"
        )
    if row["revoked_at"] is not None:
        return row
    store.update(
        conn,
        "identity_mappings",
        str(mapping_id),
        {
            "revoked_at": generation.now().isoformat(),
            "revoked_by": revoker.actor_id,
            "revocation_ref": str(revocation_ref),
        },
    )
    conn.commit()
    revoked = store.fetch_one(
        conn,
        "SELECT * FROM identity_mappings WHERE id = ?",
        (mapping_id,),
    )
    if revoked is None:  # pragma: no cover - guarded by the update target
        raise AuthenticationScopeDenied("identity mapping is unavailable")
    return revoked


class IdentityResolver:
    """Resolve a verified external subject to one unambiguous internal role."""

    def resolve(
        self,
        conn: store.DatabaseConnection,
        *,
        subject: str,
        requested_show: str | None,
        provider: str = ACCESS_PROVIDER,
    ) -> AuthenticatedOperator:
        digest = subject_hash(subject, provider=provider)
        rows = store.fetch_all(
            conn,
            "SELECT * FROM identity_mappings WHERE provider = ? AND subject_hash = ? "
            "AND revoked_at IS NULL ORDER BY tenant_id, show_id, id",
            (provider, digest),
        )
        if requested_show is None:
            candidates = [row for row in rows if row["show_id"] is None]
        else:
            exact = [row for row in rows if row["show_id"] == requested_show]
            tenants = {str(row["tenant_id"]) for row in exact}
            tenant_wide = [
                row
                for row in rows
                if row["show_id"] is None
                and (not tenants or str(row["tenant_id"]) in tenants)
            ]
            candidates = exact or tenant_wide
        if len(candidates) != 1:
            raise AuthenticationScopeDenied()
        row = candidates[0]
        try:
            role = service.HumanRole(str(row["actor_role"]))
        except ValueError as exc:
            raise AuthenticationScopeDenied() from exc
        return AuthenticatedOperator(
            actor_id=str(row["actor_id"]),
            role=role,
            tenant_id=str(row["tenant_id"]),
            show_id=str(row["show_id"]) if row["show_id"] is not None else None,
            provider=provider,
            subject_digest=digest,
            mapping_id=str(row["id"]),
        )


@runtime_checkable
class SigningKeyProvider(Protocol):
    def key_for(self, kid: str) -> Any: ...


@dataclass(frozen=True)
class StaticSigningKeyProvider:
    keys: Mapping[str, Any]

    def key_for(self, kid: str) -> Any:
        try:
            return self.keys[kid]
        except KeyError as exc:
            raise AuthenticationDenied("Access signing key is unavailable") from exc


class RemoteCloudflareJWKS:
    """Bounded, rotation-aware loader for a Cloudflare team JWKS endpoint."""

    def __init__(
        self,
        team_domain: str,
        *,
        ttl_seconds: int = 300,
        timeout_seconds: float = 5.0,
    ) -> None:
        parsed = urlsplit(team_domain.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is not None
            or parsed.hostname is None
            or not TEAM_HOST.fullmatch(parsed.hostname)
        ):
            raise AuthenticationConfigurationError(
                "Access team domain must be an HTTPS cloudflareaccess.com origin"
            )
        if isinstance(ttl_seconds, bool) or not 30 <= ttl_seconds <= 3600:
            raise ValueError("JWKS TTL must be between 30 and 3600 seconds")
        if not 0.5 <= timeout_seconds <= 15:
            raise ValueError("JWKS timeout must be between 0.5 and 15 seconds")
        self.team_domain = f"https://{parsed.hostname}"
        self.url = f"{self.team_domain}/cdn-cgi/access/certs"
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> dict[str, Any]:
        if jwt is None:
            raise AuthenticationConfigurationError(
                "Access JWT validation requires the HOSPES hosted extra"
            )
        request = urllib.request.Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": "hospes/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(MAX_JWKS_BYTES + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise AuthenticationConfigurationError(
                "Access signing keys are temporarily unavailable"
            ) from exc
        if len(payload) > MAX_JWKS_BYTES:
            raise AuthenticationConfigurationError("Access signing key response is oversized")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthenticationConfigurationError(
                "Access signing key response is invalid"
            ) from exc
        raw_keys = document.get("keys") if isinstance(document, Mapping) else None
        if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 4:
            raise AuthenticationConfigurationError("Access signing key set is invalid")
        parsed_keys: dict[str, Any] = {}
        for raw in raw_keys:
            if (
                not isinstance(raw, Mapping)
                or raw.get("kty") != "RSA"
                or raw.get("alg") != "RS256"
                or raw.get("use") not in {None, "sig"}
                or not isinstance(raw.get("kid"), str)
                or not KID.fullmatch(raw["kid"])
            ):
                raise AuthenticationConfigurationError("Access signing key set is invalid")
            try:
                parsed_keys[raw["kid"]] = jwt.PyJWK.from_dict(dict(raw)).key
            except Exception as exc:
                raise AuthenticationConfigurationError(
                    "Access signing key set is invalid"
                ) from exc
        return parsed_keys

    def key_for(self, kid: str) -> Any:
        if not KID.fullmatch(str(kid)):
            raise AuthenticationDenied("Access token signing key is invalid")
        now = time.monotonic()
        with self._lock:
            if now - self._fetched_at >= self.ttl_seconds or kid not in self._keys:
                self._keys = self._fetch()
                self._fetched_at = now
            key = self._keys.get(kid)
        if key is None:
            raise AuthenticationDenied("Access signing key is unavailable")
        return key


class AccessJWTAuthenticator:
    """Validate Access signature/claims, then map the subject internally."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        keys: SigningKeyProvider,
        resolver: IdentityResolver | None = None,
        leeway_seconds: int = 30,
    ) -> None:
        normalized_issuer = issuer.rstrip("/")
        parsed = urlsplit(normalized_issuer)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or not TEAM_HOST.fullmatch(parsed.hostname)
            or parsed.path
        ):
            raise AuthenticationConfigurationError("Access issuer is invalid")
        if not AUDIENCE.fullmatch(str(audience)):
            raise AuthenticationConfigurationError("Access audience is invalid")
        if isinstance(leeway_seconds, bool) or not 0 <= leeway_seconds <= 120:
            raise ValueError("Access clock leeway must be between 0 and 120 seconds")
        self.issuer = normalized_issuer
        self.audience = audience
        self.keys = keys
        self.resolver = resolver or IdentityResolver()
        self.leeway_seconds = leeway_seconds

    @classmethod
    def remote(cls, *, team_domain: str, audience: str) -> AccessJWTAuthenticator:
        keys = RemoteCloudflareJWKS(team_domain)
        return cls(issuer=keys.team_domain, audience=audience, keys=keys)

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None
    ) -> AccessJWTAuthenticator:
        source = os.environ if env is None else env
        team_domain = source.get("HOSPES_ACCESS_TEAM_DOMAIN", "")
        audience = source.get("HOSPES_ACCESS_AUDIENCE", "")
        if not team_domain or not audience:
            raise AuthenticationConfigurationError(
                "Access team domain and audience credentials are unavailable"
            )
        return cls.remote(team_domain=team_domain, audience=audience)

    def authenticate(
        self,
        conn: store.DatabaseConnection,
        assertion_jwt: str,
        *,
        requested_show: str | None,
    ) -> AuthenticatedOperator:
        if jwt is None:
            raise AuthenticationConfigurationError(
                "Access JWT validation requires the HOSPES hosted extra"
            )
        if not isinstance(assertion_jwt, str) or not 32 <= len(assertion_jwt) <= 16384:
            raise AuthenticationDenied()
        try:
            header = jwt.get_unverified_header(assertion_jwt)
        except jwt.PyJWTError as exc:
            raise AuthenticationDenied("Access token is malformed") from exc
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise AuthenticationDenied("Access token algorithm is unsupported")
        key = self.keys.key_for(header["kid"])
        try:
            claims = jwt.decode(
                assertion_jwt,
                key=key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["aud", "exp", "iat", "iss", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationDenied("Access token validation failed") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str):
            raise AuthenticationDenied("Access token subject is invalid")
        return self.resolver.resolve(
            conn,
            subject=subject,
            requested_show=requested_show,
            provider=ACCESS_PROVIDER,
        )


class CSRFProtector:
    """Stateless CSRF token bound to a validated internal identity."""

    def __init__(self, signing_key: bytes, *, max_age_seconds: int = 8 * 60 * 60):
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise AuthenticationConfigurationError("CSRF signing secret is invalid")
        if not 300 <= max_age_seconds <= 24 * 60 * 60:
            raise ValueError("CSRF lifetime must be between five minutes and one day")
        self.signing_key = signing_key
        self.max_age_seconds = max_age_seconds

    @staticmethod
    def _binding(identity: AuthenticatedOperator) -> bytes:
        return json.dumps(
            {
                "actor": identity.actor_id,
                "provider": identity.provider,
                "role": identity.role.value,
                "subject": identity.subject_digest,
                "tenant": identity.tenant_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def issue(self, identity: AuthenticatedOperator, *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else int(now)
        body = issued_at.to_bytes(8, "big") + secrets.token_bytes(24)
        signature = hmac.new(
            self.signing_key, self._binding(identity) + body, hashlib.sha256
        ).digest()
        return _b64url(body + signature)

    def validate(
        self,
        identity: AuthenticatedOperator,
        presented_proof: str,
        *,
        now: int | None = None,
    ) -> None:
        payload = _b64url_decode(presented_proof)
        if len(payload) != 64:
            raise CSRFDenied()
        body, presented = payload[:32], payload[32:]
        expected = hmac.new(
            self.signing_key, self._binding(identity) + body, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(presented, expected):
            raise CSRFDenied()
        issued_at = int.from_bytes(body[:8], "big")
        current = int(time.time()) if now is None else int(now)
        if issued_at > current + 30 or current - issued_at > self.max_age_seconds:
            raise CSRFDenied("CSRF token is expired")


@runtime_checkable
class SecretProvider(Protocol):
    def resolve(self, credential_ref: str) -> str: ...


@dataclass(frozen=True)
class StaticSecretProvider:
    values: Mapping[str, str]

    def resolve(self, credential_ref: str) -> str:
        value = self.values.get(credential_ref)
        if not isinstance(value, str) or len(value) < 32:
            raise AuthenticationConfigurationError(
                "internal trigger credential is unavailable"
            )
        return value


@dataclass(frozen=True)
class EnvironmentSecretProvider:
    credential_ref: str
    env_name: str
    env: Mapping[str, str] | None = None

    def resolve(self, credential_ref: str) -> str:
        if credential_ref != self.credential_ref:
            raise AuthenticationConfigurationError(
                "internal trigger credential is unavailable"
            )
        source = os.environ if self.env is None else self.env
        value = source.get(self.env_name)
        if not isinstance(value, str) or len(value) < 32:
            raise AuthenticationConfigurationError(
                "internal trigger credential is unavailable"
            )
        return value


class InternalTriggerAuthenticator:
    """Verify a wall-owned internal wake token without storing or logging it."""

    def __init__(
        self,
        provider: SecretProvider,
        *,
        credential_ref: str = "credential://hospes/internal-job-trigger",
    ) -> None:
        if not CREDENTIAL_REF.fullmatch(credential_ref):
            raise AuthenticationConfigurationError(
                "internal trigger requires an opaque credential-wall reference"
            )
        self.provider = provider
        self.credential_ref = credential_ref

    def verify(self, presented: str | None) -> None:
        expected = self.provider.resolve(self.credential_ref)
        if (
            not isinstance(presented, str)
            or len(presented) > 1024
            or not secrets.compare_digest(presented, expected)
        ):
            raise AuthenticationDenied("internal job trigger authentication failed")


def access_token_from_request(request: Any) -> str | None:
    """Prefer Cloudflare's origin header, with its application cookie as fallback."""
    header = request.headers.get(ACCESS_HEADER)
    if header:
        return header
    return request.cookies.get(ACCESS_COOKIE)


def csrf_secret_from_environment(env: Mapping[str, str] | None = None) -> bytes:
    source = os.environ if env is None else env
    encoded = source.get("HOSPES_CSRF_SECRET_B64")
    if not encoded:
        raise AuthenticationConfigurationError("CSRF signing credential is unavailable")
    try:
        decoded_key = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception as exc:
        raise AuthenticationConfigurationError("CSRF signing credential is invalid") from exc
    if len(decoded_key) != 32:
        raise AuthenticationConfigurationError("CSRF signing credential is invalid")
    return decoded_key


def request_show_scope(request: Any) -> str | None:
    value = request.path_params.get("show_id") if request.path_params else None
    if value is None:
        value = request.headers.get("X-Session-Show")
    if value is None:
        return None
    if not OPAQUE_ID.fullmatch(str(value)):
        raise AuthenticationScopeDenied()
    return str(value)


def iso_from_epoch(value: int | float) -> str:
    """Stable helper used by authentication receipts and focused tests."""
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


__all__ = [
    "ACCESS_COOKIE",
    "ACCESS_HEADER",
    "ACCESS_PROVIDER",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "MUTATING_METHODS",
    "AccessJWTAuthenticator",
    "AuthenticatedOperator",
    "AuthenticationConfigurationError",
    "AuthenticationDenied",
    "AuthenticationError",
    "AuthenticationScopeDenied",
    "CSRFDenied",
    "CSRFProtector",
    "IdentityResolver",
    "InternalTriggerAuthenticator",
    "EnvironmentSecretProvider",
    "RemoteCloudflareJWKS",
    "SecretProvider",
    "SigningKeyProvider",
    "StaticSecretProvider",
    "StaticBearerAuthenticator",
    "StaticSigningKeyProvider",
    "access_token_from_request",
    "csrf_secret_from_environment",
    "process_bound_operator",
    "register_identity_mapping",
    "revoke_identity_mapping",
    "request_show_scope",
    "subject_hash",
]
