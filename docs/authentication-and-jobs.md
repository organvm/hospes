# Authentication and durable jobs

HOSPES has two production identity boundaries. Local mode binds one operator identity at process
launch and keeps its bearer credential server-side behind the localhost operator session. Tunnel
and hosted modes accept only a Cloudflare Access application token at the origin. Browser-supplied
actor, role, and tenant headers are never authority in either mode.

## Cloudflare Access origin validation

Hosted authentication prefers `Cf-Access-Jwt-Assertion` and accepts `CF_Authorization` only as the
documented application-cookie fallback. Before a request reaches domain code, HOSPES requires:

- an RS256 token with a bounded size and a recognized `kid`;
- a valid signature from the team JWKS endpoint at `/cdn-cgi/access/certs`;
- the exact configured team issuer and Access application audience;
- `sub`, `iat`, and `exp` claims, including expiry and future-issued checks; and
- one unrevoked internal identity mapping for the requested tenant/show scope.

The JWKS loader accepts only HTTPS `*.cloudflareaccess.com` origins, bounds response size and
latency, caches the current rotation set, and refreshes when a new key id appears. The external
subject is stored only as a provider-scoped SHA-256 digest. Email and custom browser identity
headers are ignored. Ambiguous, revoked, unscoped, and cross-show mappings fail closed.
Mappings may be provisioned or revoked only by a same-tenant network operator; the database stores
that internal actor plus an opaque revocation receipt and enforces global subject/scope uniqueness.

The hosted process reads runtime values from credential-backed environment injection:

```text
HOSPES_AUTH_MODE=cloudflare_access
HOSPES_ACCESS_TEAM_DOMAIN=https://TEAM.cloudflareaccess.com
HOSPES_ACCESS_AUDIENCE=<Access application audience>
HOSPES_CSRF_SECRET_B64=<base64-encoded 32-byte wall-owned key>
```

The tracked runtime file contains only credential-wall references. No credential values, Access
subjects, contact identities, or raw JWTs belong in Git, logs, receipts, or public projections.

## CSRF and local sessions

`GET /v1/session/csrf` issues a random, HMAC-authenticated token bound to the validated provider,
subject digest, tenant, role, and actor. State-changing browser requests must present the same
token in the `hospes_csrf` SameSite cookie and `X-Hospes-CSRF` header before HOSPES validates the
signature and age. Hosted cookies are Secure. The local operator derives the CSRF signing key from
its process session secret and removes both session and CSRF cookies at logout.

## Durable job queue

Jobs are scoped to an existing tenant/show and bind their immutable payload reference and SHA-256
checksum to an idempotency key. Workers receive a random lease token once; only its digest is
stored. Every lease is bounded by both a short renewable lease and the job's absolute timeout.
Retries, attempt count, projected cost, and actual cost are policy-limited.

PostgreSQL workers use `FOR UPDATE SKIP LOCKED`; local SQLite workers serialize claims with
`BEGIN IMMEDIATE`. Each completion, retry, terminal failure, or lease timeout produces one
immutable `job_attempt_receipts` row. Handler exceptions are reduced to an opaque error reference,
so provider diagnostics and private payload data do not enter the queue record or API response.

Scheduled jobs may run only `analytics.*` or `research.*` reads. Operator/internal work may read or
prepare, but no background operation may send or publish. The authenticated hosted wake endpoint is
`POST /internal/jobs/run`; its wall-owned trigger value is injected as
`HOSPES_INTERNAL_JOB_TRIGGER_TOKEN` and is never stored. Local registered handlers run through:

```bash
python3 -m hospes jobs run --db /private/path/hospes.sqlite3 --worker operator_worker
```

An empty handler registry is a clean no-op and never consumes queued work. Provider-specific issue
implementations register only their own bounded handlers and return opaque result references.
