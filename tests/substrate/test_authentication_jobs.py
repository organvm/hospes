from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hospes import authentication, configuration, jobs, migrations, service, store
from hospes.api import create_app

TIMESTAMP = "2026-08-10T00:00:00+00:00"
ISSUER = "https://hospes-test.cloudflareaccess.com"
AUDIENCE = "0123456789abcdef0123456789abcdef"
KID_ONE = "signing-key-one"
KID_TWO = "signing-key-two"
INTERNAL_REF = "credential://hospes/internal-job-trigger"
INTERNAL_SECRET = "synthetic-internal-trigger-value-123456789"  # allow-secret: synthetic fixture
NETWORK_OPERATOR = service.HumanActor(
    "network-operator-a", service.HumanRole.NETWORK_OPERATOR, "tenant-a"
)


@pytest.fixture
def custody(tmp_path: Path):
    connection = store.connect(tmp_path / "auth-jobs.sqlite3")
    for tenant, show in (
        ("tenant-a", "show-a"),
        ("tenant-a", "show-b"),
        ("tenant-b", "show-a"),
    ):
        store.insert(
            connection,
            "show_registry",
            {
                "id": f"{tenant}-{show}",
                "tenant_id": tenant,
                "show_id": show,
                "label": f"{tenant} {show}",
                "config_ref": f"config://{tenant}/{show}",
                "status": "active",
                "created_at": TIMESTAMP,
                "updated_at": TIMESTAMP,
            },
        )
    connection.commit()
    yield connection, tmp_path
    connection.close()


@pytest.fixture(scope="module")
def signing_keys():
    first = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return first, second


def access_token(
    private_key: Any,
    *,
    subject: str = "subject-a",
    kid: str = KID_ONE,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    now: datetime | None = None,
    expires_in: int = 300,
    algorithm: str = "RS256",
) -> str:
    current = now or datetime.now(UTC)
    return jwt.encode(
        {
            "aud": [audience],
            "exp": current + timedelta(seconds=expires_in),
            "iat": current,
            "iss": issuer,
            "sub": subject,
            "email": "ignored-identity@example.test",
        },
        private_key,
        algorithm=algorithm,
        headers={"kid": kid},
    )


def register(
    connection,
    *,
    subject: str = "subject-a",
    tenant: str = "tenant-a",
    show: str | None = None,
    actor: str = "producer-a",
    role: str = "producer",
):
    provisioner = (
        NETWORK_OPERATOR
        if tenant == "tenant-a"
        else service.HumanActor(
            "network-operator-scope",
            service.HumanRole.NETWORK_OPERATOR,
            tenant,
        )
    )
    return authentication.register_identity_mapping(
        connection,
        subject=subject,
        tenant_id=tenant,
        show_id=show,
        actor_id=actor,
        actor_role=role,
        provisioner=provisioner,
    )


def job_spec(**overrides: Any) -> jobs.JobSpec:
    values = {
        "tenant_id": "tenant-a",
        "show_id": "show-a",
        "job_type": "analytics.fetch",
        "payload_ref": "private-field://11111111-1111-4111-8111-111111111111",
        "payload_checksum": hashlib.sha256(b"synthetic payload").hexdigest(),
        "idempotency_key": "analytics:fixture:2026-08-10",
        "created_by": "producer-a",
        "execution_kind": "scheduled",
        "operation": "read",
        "priority": 100,
        "max_attempts": 3,
        "timeout_seconds": 120,
        "cost_limit_minor": 25,
    }
    values.update(overrides)
    return jobs.JobSpec(**values)


def test_identity_mapping_hashes_subject_and_resolves_scopes(custody) -> None:
    connection, _tmp_path = custody
    row = register(connection)
    serialized = json.dumps(row, sort_keys=True)
    assert "subject-a" not in serialized
    assert row["subject_hash"] == authentication.subject_hash("subject-a")
    assert row["provisioned_by"] == NETWORK_OPERATOR.actor_id
    assert register(connection)["id"] == row["id"]

    identity = authentication.IdentityResolver().resolve(
        connection, subject="subject-a", requested_show="show-b"
    )
    assert identity.as_human_actor() == service.HumanActor(
        actor_id="producer-a",
        role=service.HumanRole.PRODUCER,
        tenant_id="tenant-a",
    )
    assert identity.show_id is None

    register(
        connection,
        subject="subject-show",
        show="show-a",
        actor="editor-a",
        role="editorial_owner",
    )
    scoped = authentication.IdentityResolver().resolve(
        connection, subject="subject-show", requested_show="show-a"
    )
    assert scoped.show_id == "show-a"
    with pytest.raises(authentication.AuthenticationScopeDenied):
        authentication.IdentityResolver().resolve(
            connection, subject="subject-show", requested_show="show-b"
        )
    with pytest.raises(authentication.AuthenticationScopeDenied):
        authentication.IdentityResolver().resolve(
            connection, subject="subject-show", requested_show=None
        )


def test_identity_mapping_conflicts_revocation_and_invalid_inputs(custody) -> None:
    connection, _tmp_path = custody
    register(connection, subject="subject-conflict")
    with pytest.raises(authentication.AuthenticationScopeDenied, match="conflicts"):
        register(
            connection,
            subject="subject-conflict",
            actor="producer-other",
        )
    with pytest.raises(authentication.AuthenticationScopeDenied, match="registered"):
        register(connection, subject="unknown-scope", tenant="tenant-missing")
    with pytest.raises(ValueError, match="tenant_id"):
        register(connection, tenant="bad tenant")
    with pytest.raises(ValueError, match="actor_id"):
        register(connection, actor="x")
    with pytest.raises(ValueError, match="show_id"):
        register(connection, show="bad show")
    with pytest.raises(ValueError, match="actor_role"):
        register(connection, role="root")
    with pytest.raises(authentication.AuthenticationConfigurationError):
        authentication.register_identity_mapping(
            connection,
            subject="subject",
            tenant_id="tenant-a",
            actor_id="producer-a",
            actor_role="producer",
            provisioner=NETWORK_OPERATOR,
            provider="untrusted_provider",
        )
    with pytest.raises(authentication.AuthenticationScopeDenied, match="network operator"):
        authentication.register_identity_mapping(
            connection,
            subject="unauthorized-subject",
            tenant_id="tenant-a",
            actor_id="producer-a",
            actor_role="producer",
            provisioner=service.HumanActor(
                "producer-a", service.HumanRole.PRODUCER, "tenant-a"
            ),
        )
    with pytest.raises(authentication.AuthenticationDenied):
        authentication.subject_hash("")

    row = store.fetch_one(
        connection,
        "SELECT * FROM identity_mappings WHERE subject_hash = ?",
        (authentication.subject_hash("subject-conflict"),),
    )
    revoked = authentication.revoke_identity_mapping(
        connection,
        mapping_id=row["id"],
        revoker=NETWORK_OPERATOR,
        revocation_ref="receipt://identity/revocation",
    )
    assert revoked["revoked_by"] == NETWORK_OPERATOR.actor_id
    assert revoked["revocation_ref"] == "receipt://identity/revocation"
    assert authentication.revoke_identity_mapping(
        connection,
        mapping_id=row["id"],
        revoker=NETWORK_OPERATOR,
        revocation_ref="receipt://identity/revocation",
    )["revoked_at"] == revoked["revoked_at"]
    with pytest.raises(authentication.AuthenticationScopeDenied):
        authentication.IdentityResolver().resolve(
            connection, subject="subject-conflict", requested_show=None
        )
    with pytest.raises(authentication.AuthenticationScopeDenied, match="same-tenant"):
        authentication.revoke_identity_mapping(
            connection,
            mapping_id=row["id"],
            revoker=service.HumanActor(
                "network-operator-b",
                service.HumanRole.NETWORK_OPERATOR,
                "tenant-b",
            ),
            revocation_ref="receipt://identity/cross-tenant",
        )


def test_access_jwt_validates_signature_claims_and_internal_mapping(
    custody, signing_keys
) -> None:
    connection, _tmp_path = custody
    private, second = signing_keys
    register(connection, subject="jwt-subject", role="network_operator")
    authenticator = authentication.AccessJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys=authentication.StaticSigningKeyProvider(
            {KID_ONE: private.public_key(), KID_TWO: second.public_key()}
        ),
    )
    access_assertion = access_token(private, subject="jwt-subject")
    identity = authenticator.authenticate(
        connection, access_assertion, requested_show="show-a"
    )
    assert identity.role is service.HumanRole.NETWORK_OPERATOR
    assert identity.provider == "cloudflare_access"

    invalid_tokens = [
        access_token(second, subject="jwt-subject"),
        access_token(private, subject="jwt-subject", issuer="https://other.cloudflareaccess.com"),
        access_token(private, subject="jwt-subject", audience="aaaaaaaaaaaaaaaa"),
        access_token(private, subject="jwt-subject", expires_in=-60),
    ]
    for invalid in invalid_tokens:
        with pytest.raises(authentication.AuthenticationDenied):
            authenticator.authenticate(connection, invalid, requested_show="show-a")
    with pytest.raises(authentication.AuthenticationDenied, match="malformed"):
        authenticator.authenticate(connection, "x" * 32, requested_show=None)
    with pytest.raises(authentication.AuthenticationDenied, match="unavailable"):
        authenticator.authenticate(
            connection,
            access_token(private, subject="jwt-subject", kid="unknown-signing-key"),
            requested_show=None,
        )


def test_access_authenticator_configuration_and_algorithm_fail_closed(
    custody, signing_keys
) -> None:
    connection, _tmp_path = custody
    private, _second = signing_keys
    with pytest.raises(authentication.AuthenticationConfigurationError, match="issuer"):
        authentication.AccessJWTAuthenticator(
            issuer="http://hospes-test.cloudflareaccess.com",
            audience=AUDIENCE,
            keys=authentication.StaticSigningKeyProvider({}),
        )
    with pytest.raises(authentication.AuthenticationConfigurationError, match="audience"):
        authentication.AccessJWTAuthenticator(
            issuer=ISSUER,
            audience="bad",
            keys=authentication.StaticSigningKeyProvider({}),
        )
    with pytest.raises(ValueError, match="leeway"):
        authentication.AccessJWTAuthenticator(
            issuer=ISSUER,
            audience=AUDIENCE,
            keys=authentication.StaticSigningKeyProvider({}),
            leeway_seconds=121,
        )
    authenticator = authentication.AccessJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys=authentication.StaticSigningKeyProvider({KID_ONE: private.public_key()}),
    )
    hs_token = jwt.encode(
        {
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "iat": datetime.now(UTC),
            "iss": ISSUER,
            "sub": "subject",
        },
        "synthetic-secret-value-at-least-32-bytes",  # allow-secret: synthetic fixture
        algorithm="HS256",
        headers={"kid": KID_ONE},
    )
    with pytest.raises(authentication.AuthenticationDenied, match="algorithm"):
        authenticator.authenticate(connection, hs_token, requested_show=None)


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


def public_jwk(key: Any, kid: str) -> dict[str, Any]:
    value = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    value.update({"alg": "RS256", "kid": kid, "use": "sig"})
    return value


def test_remote_jwks_loads_rotation_set_and_caches(monkeypatch, signing_keys) -> None:
    first, second = signing_keys
    calls: list[str] = []
    payload = json.dumps(
        {"keys": [public_jwk(first, KID_ONE), public_jwk(second, KID_TWO)]}
    ).encode()

    def fake_urlopen(request, timeout):
        calls.append(f"{request.full_url}:{timeout}")
        return FakeResponse(payload)

    monkeypatch.setattr(authentication.urllib.request, "urlopen", fake_urlopen)
    provider = authentication.RemoteCloudflareJWKS(ISSUER, ttl_seconds=300)
    assert provider.key_for(KID_ONE) is not None
    assert provider.key_for(KID_TWO) is not None
    assert len(calls) == 1
    assert provider.url.endswith("/cdn-cgi/access/certs")


@pytest.mark.parametrize(
    "domain",
    [
        "http://hospes-test.cloudflareaccess.com",
        "https://example.com",
        "https://hospes-test.cloudflareaccess.com/path",
        "https://hospes-test.cloudflareaccess.com:8443",
    ],
)
def test_remote_jwks_rejects_unsafe_domains(domain: str) -> None:
    with pytest.raises(authentication.AuthenticationConfigurationError):
        authentication.RemoteCloudflareJWKS(domain)


def test_remote_jwks_rejects_bad_responses(monkeypatch, signing_keys) -> None:
    first, _second = signing_keys
    provider = authentication.RemoteCloudflareJWKS(ISSUER)
    for payload in (
        b"not-json",
        json.dumps({"keys": []}).encode(),
        json.dumps({"keys": [{**public_jwk(first, KID_ONE), "alg": "HS256"}]}).encode(),
        b"x" * (authentication.MAX_JWKS_BYTES + 1),
    ):
        monkeypatch.setattr(
            authentication.urllib.request,
            "urlopen",
            lambda *_args, payload=payload, **_kwargs: FakeResponse(payload),
        )
        with pytest.raises(authentication.AuthenticationError):
            provider._fetch()
    with pytest.raises(ValueError, match="TTL"):
        authentication.RemoteCloudflareJWKS(ISSUER, ttl_seconds=1)
    with pytest.raises(ValueError, match="timeout"):
        authentication.RemoteCloudflareJWKS(ISSUER, timeout_seconds=30)
    with pytest.raises(authentication.AuthenticationDenied, match="invalid"):
        provider.key_for("short")
    monkeypatch.setattr(
        authentication.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    with pytest.raises(authentication.AuthenticationConfigurationError, match="unavailable"):
        provider._fetch()


def test_environment_backed_authentication_configuration(monkeypatch) -> None:
    authenticator = authentication.AccessJWTAuthenticator.from_environment(
        {
            "HOSPES_ACCESS_TEAM_DOMAIN": ISSUER,
            "HOSPES_ACCESS_AUDIENCE": AUDIENCE,
        }
    )
    assert authenticator.issuer == ISSUER
    with pytest.raises(authentication.AuthenticationConfigurationError, match="credentials"):
        authentication.AccessJWTAuthenticator.from_environment({})

    provider = authentication.EnvironmentSecretProvider(
        credential_ref=INTERNAL_REF,
        env_name="HOSPES_INTERNAL_JOB_TRIGGER_TOKEN",
        env={"HOSPES_INTERNAL_JOB_TRIGGER_TOKEN": INTERNAL_SECRET},
    )
    assert provider.resolve(INTERNAL_REF) == INTERNAL_SECRET
    with pytest.raises(authentication.AuthenticationConfigurationError):
        provider.resolve("credential://hospes/other-trigger")
    with pytest.raises(authentication.AuthenticationConfigurationError):
        authentication.EnvironmentSecretProvider(
            credential_ref=INTERNAL_REF,
            env_name="HOSPES_INTERNAL_JOB_TRIGGER_TOKEN",
            env={},
        ).resolve(INTERNAL_REF)

    encoded = base64.b64encode(b"z" * 32).decode("ascii")
    assert authentication.csrf_secret_from_environment(
        {"HOSPES_CSRF_SECRET_B64": encoded}
    ) == b"z" * 32
    for env in (
        {},
        {"HOSPES_CSRF_SECRET_B64": "not-base64"},
        {"HOSPES_CSRF_SECRET_B64": base64.b64encode(b"short").decode()},
    ):
        with pytest.raises(authentication.AuthenticationConfigurationError):
            authentication.csrf_secret_from_environment(env)
    assert authentication.iso_from_epoch(0) == "1970-01-01T00:00:00+00:00"

    monkeypatch.setenv("HOSPES_AUTH_MODE", "invalid")
    with pytest.raises(authentication.AuthenticationConfigurationError, match="AUTH_MODE"):
        create_app(":memory:")


def test_api_security_controls_cannot_be_disabled_outside_synthetic_runtime() -> None:
    actor = service.HumanActor("producer-a", service.HumanRole.PRODUCER, "tenant-a")
    synthetic = authentication.StaticBearerAuthenticator(
        {"synthetic-bearer-token-value": actor}
    )
    with pytest.raises(authentication.AuthenticationConfigurationError, match="synthetic_test"):
        create_app(":memory:", _test_bearer_authenticator=synthetic)
    with pytest.raises(authentication.AuthenticationConfigurationError, match="CSRF"):
        create_app(":memory:", bound_actor=actor, csrf_required=False)
    with pytest.raises(authentication.AuthenticationConfigurationError, match="CSRF signing"):
        create_app(":memory:", bound_actor=actor, csrf_secret=b"")
    with pytest.raises(authentication.AuthenticationConfigurationError, match="one operator"):
        create_app(
            ":memory:",
            bound_actor=actor,
            runtime_kind="synthetic_test",
            _test_bearer_authenticator=synthetic,
        )


def test_csrf_tokens_bind_identity_expiry_and_double_submit() -> None:
    actor = service.HumanActor("producer-a", service.HumanRole.PRODUCER, "tenant-a")
    identity = authentication.process_bound_operator(actor)
    protector = authentication.CSRFProtector(b"c" * 32, max_age_seconds=600)
    csrf_proof = protector.issue(identity, now=1_000)
    protector.validate(identity, csrf_proof, now=1_599)

    other = authentication.process_bound_operator(
        service.HumanActor("producer-b", service.HumanRole.PRODUCER, "tenant-a")
    )
    midpoint = len(csrf_proof) // 2
    tampered = (
        csrf_proof[:midpoint]
        + ("A" if csrf_proof[midpoint] != "A" else "B")
        + csrf_proof[midpoint + 1 :]
    )
    for candidate in (tampered, "invalid", ""):
        with pytest.raises(authentication.CSRFDenied):
            protector.validate(identity, candidate, now=1_100)
    with pytest.raises(authentication.CSRFDenied):
        protector.validate(other, csrf_proof, now=1_100)
    with pytest.raises(authentication.CSRFDenied, match="expired"):
        protector.validate(identity, csrf_proof, now=1_601)
    with pytest.raises(authentication.CSRFDenied):
        protector.validate(identity, csrf_proof, now=900)
    with pytest.raises(authentication.AuthenticationConfigurationError):
        authentication.CSRFProtector(b"short")
    with pytest.raises(ValueError, match="lifetime"):
        authentication.CSRFProtector(b"c" * 32, max_age_seconds=60)
    with pytest.raises(authentication.CSRFDenied):
        authentication._b64url_decode("x" * 1025)
    with pytest.raises(authentication.CSRFDenied):
        authentication._b64url_decode("%%invalid%%")


def test_synthetic_bearer_authenticator_binds_identity_to_token() -> None:
    actor = service.HumanActor("producer-a", service.HumanRole.PRODUCER, "tenant-a")
    authenticator = authentication.StaticBearerAuthenticator(
        {"synthetic-bearer-token-value": actor}
    )
    identity = authenticator.authenticate("synthetic-bearer-token-value")
    assert identity.as_human_actor() == actor
    assert identity.provider == "synthetic_test_bearer"
    for token in (None, "short", "synthetic-bearer-token-wrong"):
        with pytest.raises(authentication.AuthenticationDenied):
            authenticator.authenticate(token)


def test_internal_trigger_and_request_helpers() -> None:
    provider = authentication.StaticSecretProvider({INTERNAL_REF: INTERNAL_SECRET})
    trigger = authentication.InternalTriggerAuthenticator(provider)
    trigger.verify(INTERNAL_SECRET)
    with pytest.raises(authentication.AuthenticationDenied):
        trigger.verify("wrong-value")
    with pytest.raises(authentication.AuthenticationConfigurationError):
        authentication.InternalTriggerAuthenticator(provider, credential_ref="raw-secret")
    with pytest.raises(authentication.AuthenticationConfigurationError):
        authentication.StaticSecretProvider({}).resolve(INTERNAL_REF)

    request = SimpleNamespace(
        headers={authentication.ACCESS_HEADER: "header-token"},
        cookies={authentication.ACCESS_COOKIE: "cookie-token"},
        path_params={"show_id": "show-a"},
    )
    assert authentication.access_token_from_request(request) == "header-token"
    assert authentication.request_show_scope(request) == "show-a"
    request.headers = {}
    assert authentication.access_token_from_request(request) == "cookie-token"
    request.path_params = {"show_id": "bad show"}
    with pytest.raises(authentication.AuthenticationScopeDenied):
        authentication.request_show_scope(request)


def test_access_api_ignores_identity_headers_and_requires_csrf(tmp_path: Path, signing_keys) -> None:
    private, _second = signing_keys
    database = tmp_path / "access-api.sqlite3"
    bootstrap = store.connect(database)
    store.insert(
        bootstrap,
        "show_registry",
        {
            "id": "tenant-a-show-a",
            "tenant_id": "tenant-a",
            "show_id": "show-a",
            "label": "Show A",
            "config_ref": "config://tenant-a/show-a",
            "status": "active",
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        },
    )
    bootstrap.commit()
    register(bootstrap, subject="api-subject")
    bootstrap.close()
    authenticator = authentication.AccessJWTAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys=authentication.StaticSigningKeyProvider({KID_ONE: private.public_key()}),
    )
    app = create_app(
        str(database),
        access_authenticator=authenticator,
        csrf_secret=b"a" * 32,
        csrf_cookie_secure=False,
        runtime_kind="synthetic_test",
    )
    client = TestClient(app)
    access_assertion = access_token(private, subject="api-subject")
    access_headers = {
        authentication.ACCESS_HEADER: access_assertion,
        "X-Hospes-Actor": "spoofed-actor",
        "X-Hospes-Role": "host",
        "X-Hospes-Tenant": "tenant-b",
    }
    context = client.get("/v1/operator-context", headers=access_headers)
    assert context.status_code == 200
    assert context.json()["actor_id"] == "producer-a"
    assert context.json()["tenant_id"] == "tenant-a"

    payload = {
        "tenant_id": "tenant-a",
        "network_id": "network-a",
        "show_id": "show-a",
        "guest_name": "Synthetic Guest",
        "why_guest": "A synthetic guest proves JWT-derived authority remains scoped.",
        "why_now": "The authentication substrate is under focused verification.",
        "proposed_artifact": "An authentication receipt",
        "relationship_class": "C1",
    }
    denied = client.post("/v1/opportunities", headers=access_headers, json=payload)
    assert denied.status_code == 403
    csrf = client.get("/v1/session/csrf", headers=access_headers)
    assert csrf.status_code == 200
    token_value = csrf.json()["csrf_token"]
    created = client.post(
        "/v1/opportunities",
        headers={**access_headers, authentication.CSRF_HEADER: token_value},
        json=payload,
    )
    assert created.status_code == 201, created.text
    assert app.state.conn.execute(
        "SELECT tenant_id FROM appearance_opportunities"
    ).fetchone()[0] == "tenant-a"


def test_show_scoped_access_mapping_denies_unscoped_and_cross_show_api(
    tmp_path: Path, signing_keys
) -> None:
    private, _second = signing_keys
    database = tmp_path / "show-scoped-api.sqlite3"
    connection = store.connect(database)
    for show in ("show-a", "show-b"):
        store.insert(
            connection,
            "show_registry",
            {
                "id": f"tenant-a-{show}",
                "tenant_id": "tenant-a",
                "show_id": show,
                "label": show,
                "config_ref": f"config://tenant-a/{show}",
                "status": "active",
                "created_at": TIMESTAMP,
                "updated_at": TIMESTAMP,
            },
        )
    connection.commit()
    register(connection, subject="show-subject", show="show-a")
    connection.close()
    app = create_app(
        str(database),
        access_authenticator=authentication.AccessJWTAuthenticator(
            issuer=ISSUER,
            audience=AUDIENCE,
            keys=authentication.StaticSigningKeyProvider({KID_ONE: private.public_key()}),
        ),
        csrf_secret=b"s" * 32,
        csrf_cookie_secure=False,
        runtime_kind="synthetic_test",
    )
    client = TestClient(app)
    headers = {authentication.ACCESS_HEADER: access_token(private, subject="show-subject")}
    assert client.get("/v1/shows/show-a/suggestions", headers=headers).status_code == 200
    assert client.get("/v1/shows/show-b/suggestions", headers=headers).status_code == 403
    assert client.get("/v1/opportunities", headers=headers).status_code == 403


def test_job_enqueue_is_idempotent_and_scheduled_policy_is_read_only(custody) -> None:
    connection, _tmp_path = custody
    first = jobs.enqueue(connection, job_spec())
    assert jobs.enqueue(connection, job_spec())["id"] == first["id"]
    assert first["status"] == "queued"
    assert first["payload_checksum"] == job_spec().payload_checksum
    with pytest.raises(jobs.JobConflict, match="idempotency"):
        jobs.enqueue(connection, job_spec(payload_checksum="0" * 64))
    with pytest.raises(jobs.JobValidationError, match="restricted"):
        jobs.enqueue(connection, job_spec(job_type="distribution.publish"))
    with pytest.raises(jobs.JobValidationError, match="only read"):
        jobs.enqueue(connection, job_spec(operation="prepare"))
    with pytest.raises(jobs.JobValidationError, match="only read or prepare"):
        jobs.enqueue(
            connection,
            job_spec(execution_kind="operator", operation="publish"),
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"tenant_id": "bad tenant"}, "tenant_id"),
        ({"job_type": "bad"}, "job_type"),
        ({"payload_ref": "plaintext"}, "payload_ref"),
        ({"payload_checksum": "bad"}, "payload_checksum"),
        ({"idempotency_key": "short"}, "idempotency_key"),
        ({"execution_kind": "daemon"}, "execution_kind"),
        ({"priority": -1}, "priority"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"cost_limit_minor": 2501}, "cost_limit_minor"),
        ({"correlation_id": "bad value"}, "correlation_id"),
        ({"show_id": "show-missing"}, "registered show"),
    ],
)
def test_job_enqueue_rejects_invalid_contracts(custody, override, message) -> None:
    connection, _tmp_path = custody
    with pytest.raises(jobs.JobValidationError, match=message):
        jobs.enqueue(connection, job_spec(**override))


def test_job_lease_heartbeat_complete_and_receipt(custody) -> None:
    connection, _tmp_path = custody
    jobs.enqueue(connection, job_spec())
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    lease = jobs.lease_next(
        connection,
        worker_id="worker-a",
        lease_seconds=30,
        now=now,
    )
    assert lease is not None
    assert lease.attempt == 1
    row = store.fetch_one(connection, "SELECT * FROM background_jobs WHERE id = ?", (lease.job_id,))
    assert lease.lease_token not in json.dumps(row)
    expiry = jobs.heartbeat(
        connection,
        lease,
        extension_seconds=45,
        now=now + timedelta(seconds=10),
    )
    assert expiry == (now + timedelta(seconds=55)).isoformat()
    completed = jobs.complete(
        connection,
        lease,
        jobs.JobExecutionResult("job-result://fixture/succeeded", cost_minor=10),
        now=now + timedelta(seconds=20),
    )
    assert completed["status"] == "succeeded"
    assert completed["cost_spent_minor"] == 10
    receipt = store.fetch_one(
        connection,
        "SELECT * FROM job_attempt_receipts WHERE job_id = ?",
        (lease.job_id,),
    )
    assert receipt["outcome"] == "succeeded"
    assert receipt["payload_checksum"] == lease.payload_checksum
    with pytest.raises(jobs.JobConflict, match="invalid"):
        jobs.complete(
            connection,
            lease,
            jobs.JobExecutionResult("job-result://fixture/reused"),
        )


def test_job_retry_terminal_failure_cost_and_token_guards(custody) -> None:
    connection, _tmp_path = custody
    jobs.enqueue(connection, job_spec(max_attempts=2, cost_limit_minor=5))
    now = datetime(2026, 8, 10, 13, tzinfo=UTC)
    first = jobs.lease_next(connection, worker_id="worker-a", now=now)
    assert first is not None
    forged = jobs.JobLease(**{**first.__dict__, "lease_token": "forged-token"})
    with pytest.raises(jobs.JobConflict, match="invalid"):
        jobs.heartbeat(connection, forged, now=now + timedelta(seconds=1))
    with pytest.raises(jobs.JobConflict, match="cost limit"):
        jobs.complete(
            connection,
            first,
            jobs.JobExecutionResult("job-result://fixture/too-expensive", cost_minor=6),
            now=now + timedelta(seconds=1),
        )
    retried = jobs.fail(
        connection,
        first,
        error_ref="error://provider/rate-limit",
        retryable=True,
        cost_minor=2,
        retry_after_seconds=5,
        now=now + timedelta(seconds=2),
    )
    assert retried["status"] == "queued"
    assert retried["attempts"] == 1
    assert jobs.lease_next(
        connection, worker_id="worker-b", now=now + timedelta(seconds=3)
    ) is None
    second = jobs.lease_next(
        connection, worker_id="worker-b", now=now + timedelta(seconds=8)
    )
    assert second is not None and second.attempt == 2
    terminal = jobs.fail(
        connection,
        second,
        error_ref="error://provider/unavailable",
        retryable=True,
        cost_minor=1,
        now=now + timedelta(seconds=9),
    )
    assert terminal["status"] == "failed"
    assert terminal["cost_spent_minor"] == 3
    receipts = store.fetch_all(
        connection,
        "SELECT attempt, outcome FROM job_attempt_receipts WHERE job_id = ? ORDER BY attempt",
        (second.job_id,),
    )
    assert receipts == [
        {"attempt": 1, "outcome": "retry"},
        {"attempt": 2, "outcome": "failed"},
    ]


def test_expired_job_is_requeued_then_terminally_timed_out(custody) -> None:
    connection, _tmp_path = custody
    jobs.enqueue(connection, job_spec(max_attempts=2, timeout_seconds=10))
    now = datetime(2026, 8, 10, 14, tzinfo=UTC)
    first = jobs.lease_next(
        connection, worker_id="worker-timeout", lease_seconds=10, now=now
    )
    assert first is not None
    second = jobs.lease_next(
        connection,
        worker_id="worker-retry",
        lease_seconds=10,
        now=now + timedelta(seconds=11),
    )
    assert second is not None and second.attempt == 2
    assert jobs.lease_next(
        connection,
        worker_id="worker-final",
        now=now + timedelta(seconds=22),
    ) is None
    row = store.fetch_one(connection, "SELECT status FROM background_jobs WHERE id = ?", (first.job_id,))
    assert row["status"] == "failed"
    assert [
        item["outcome"]
        for item in store.fetch_all(
            connection,
            "SELECT outcome FROM job_attempt_receipts WHERE job_id = ? ORDER BY attempt",
            (first.job_id,),
        )
    ] == ["timeout", "timeout"]


def test_run_available_executes_handlers_and_redacts_handler_failures(custody) -> None:
    connection, _tmp_path = custody
    jobs.enqueue(connection, job_spec(idempotency_key="analytics:run:success"))
    jobs.enqueue(
        connection,
        job_spec(
            job_type="research.fetch",
            idempotency_key="research:run:failure",
            payload_checksum=hashlib.sha256(b"research").hexdigest(),
        ),
    )

    def succeed(lease: jobs.JobLease) -> jobs.JobExecutionResult:
        return jobs.JobExecutionResult(f"job-result://{lease.job_id}/done")

    def explode(_lease: jobs.JobLease) -> jobs.JobExecutionResult:
        raise RuntimeError("private provider diagnostic")

    results = jobs.run_available(
        connection,
        worker_id="worker-runner",
        handlers={"analytics.fetch": succeed, "research.fetch": explode},
        max_jobs=2,
    )
    assert [row["status"] for row in results] == ["succeeded", "queued"]
    assert results[1]["last_error_ref"] == "error://jobs/handler-failure"
    assert jobs.run_available(
        connection, worker_id="worker-empty", handlers={}, max_jobs=1
    ) == []


def test_run_available_rejects_handler_results_outside_job_policy(custody) -> None:
    connection, _tmp_path = custody
    jobs.enqueue(
        connection,
        job_spec(
            idempotency_key="analytics:run:over-cost",
            cost_limit_minor=1,
        ),
    )

    def over_cost(_lease: jobs.JobLease) -> jobs.JobExecutionResult:
        return jobs.JobExecutionResult("job-result://fixture/over-cost", cost_minor=2)

    result = jobs.run_once(
        connection,
        worker_id="worker-policy",
        handlers={"analytics.fetch": over_cost},
    )
    assert result is not None
    assert result["status"] == "failed"
    assert result["last_error_ref"] == "error://jobs/result-rejected"


def test_job_filters_validation_and_cli_contract(custody, capsys) -> None:
    connection, tmp_path = custody
    jobs.enqueue(connection, job_spec())
    assert len(
        jobs.list_jobs(
            connection, tenant_id="tenant-a", show_id="show-a", status="queued"
        )
    ) == 1
    with pytest.raises(jobs.JobValidationError, match="status"):
        jobs.list_jobs(connection, tenant_id="tenant-a", show_id="show-a", status="done")
    with pytest.raises(jobs.JobValidationError, match="tenant_id"):
        jobs.list_jobs(connection, tenant_id="bad tenant", show_id="show-a")
    with pytest.raises(jobs.JobValidationError, match="worker_id"):
        jobs.lease_next(connection, worker_id="bad worker")
    with pytest.raises(jobs.JobValidationError, match="lease_seconds"):
        jobs.lease_next(connection, worker_id="worker-a", lease_seconds=1)
    with pytest.raises(jobs.JobValidationError, match="job_types"):
        jobs.lease_next(connection, worker_id="worker-a", job_types=("bad",))
    with pytest.raises(jobs.JobValidationError, match="max_jobs"):
        jobs.run_available(connection, worker_id="worker-a", handlers={}, max_jobs=0)

    from hospes.__main__ import local_job_handlers, main

    # The local worker can only lease job types it has a handler for, so the
    # handler map *is* its authority. Every registered handler must be a read.
    handlers = local_job_handlers(connection)
    assert handlers
    assert all(
        job_type.startswith(jobs.SCHEDULED_JOB_PREFIXES) for job_type in handlers
    ), "a local worker handler outside the read namespaces would grant send authority"

    cli_db = tmp_path / "cli-jobs.sqlite3"
    assert main(["jobs", "run", "--db", str(cli_db), "--worker", "worker-cli"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "processed": 0,
        "registered_handlers": len(handlers),
        "worker": "worker-cli",
    }


def test_internal_trigger_api_runs_only_authenticated_registered_jobs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "internal-trigger.sqlite3"
    connection = store.connect(database)
    store.insert(
        connection,
        "show_registry",
        {
            "id": "tenant-a-show-a",
            "tenant_id": "tenant-a",
            "show_id": "show-a",
            "label": "Show A",
            "config_ref": "config://tenant-a/show-a",
            "status": "active",
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        },
    )
    connection.commit()
    jobs.enqueue(connection, job_spec())
    connection.close()

    def handler(lease: jobs.JobLease) -> jobs.JobExecutionResult:
        return jobs.JobExecutionResult(f"job-result://{lease.job_id}/internal")

    trigger = authentication.InternalTriggerAuthenticator(
        authentication.StaticSecretProvider({INTERNAL_REF: INTERNAL_SECRET})
    )
    app = create_app(
        str(database),
        internal_trigger_authenticator=trigger,
        job_handlers={"analytics.fetch": handler},
    )
    client = TestClient(app)
    assert client.post("/internal/jobs/run").status_code == 401
    response = client.post(
        "/internal/jobs/run",
        headers={"X-Hospes-Internal-Trigger": INTERNAL_SECRET},
    )
    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert set(response.json()["jobs"][0]) == {"job_ref", "status"}


def test_runtime_authentication_and_job_configuration_is_typed(tmp_path: Path) -> None:
    runtime = configuration.load_runtime()
    assert runtime.authentication.local_identity == "process_bound"
    assert runtime.authentication.identity_mapping == "database_subject_hash"
    assert runtime.jobs.scheduled_operations == ("read",)
    assert runtime.jobs.scheduled_outbound is False
    raw = runtime.raw
    raw["runtime"]["authentication"]["deny_by_default"] = False
    invalid = tmp_path / "runtime-invalid.yaml"
    import yaml

    invalid.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(configuration.ConfigurationError, match="deny_by_default"):
        configuration.load_runtime(invalid)


def test_migration_eleven_adds_job_policy_and_receipt_constraints(custody) -> None:
    connection, _tmp_path = custody
    assert migrations.current_version(connection) == migrations.LATEST_VERSION
    assert any(
        item["version"] == 11 and item["name"] == "authenticated_leased_jobs"
        for item in migrations.applied_migrations(connection)
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(background_jobs)")
    }
    assert {
        "execution_kind",
        "operation",
        "payload_checksum",
        "policy_version",
        "lease_started_at",
        "result_ref",
        "completed_at",
    } <= columns
    identity_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(identity_mappings)")
    }
    assert {"provisioned_by", "revoked_by", "revocation_ref"} <= identity_columns
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(job_attempt_receipts)"
    ).fetchall()
    assert {row[2] for row in foreign_keys} == {"background_jobs"}
