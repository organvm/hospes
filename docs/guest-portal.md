# Guest portal (self-serve intake)

Guest coordination is otherwise manual email back and forth: a producer asks for
dietary needs in one thread, accessibility in another, chases a release form,
and negotiates dates in a fourth. The guest portal replaces that with one link.

The portal is **off by default** and is a separate application from the operator
surface. Turning it on is a per-show configuration choice, and every safety
property below is enforced in code rather than documented as a convention.

## The shape of the thing

```
operator (BOOKED candidate)
  └─ POST /v1/opportunities/<id>/portal-link      → one-time URL, shown once
       ↓  a human carries the link to the guest — HOSPES never sends it
guest (phone)
  └─ GET  /guest/#<one-time token>                → SPA, noindex
     POST /guest/session   { token }              → one-time exchange, short session
     POST /guest/intake    { session, … }         → intake + consent + three dates
       ↓
  receipts: guest_intake.completed · consent.signed · availability.provided
       ↓
operator dashboard: "Guest completed portal"
```

## Why the token rides in the fragment

The link is `https://<portal host>/guest/#token=<signed token>`. Everything after<!-- allow-secret: the shape of the link, not a value -->
`#` is a **URL fragment**: the browser never puts it in a request line, never
sends it in a `Referer` header, and no server or proxy writes it to an access
log. The client reads it once, exchanges it, and erases it from the address bar
with `history.replaceState`.

The token itself is an HMAC-SHA256-signed payload carrying only the tenant, show,
opaque guest id, expiry, and a nonce. The database stores its **SHA-256 digest**,
never the token, so a leaked `portal_tokens` table cannot be replayed into the
portal.

## One-time exchange, then a short session

`portal_sessions` holds `UNIQUE(portal_token_id)`. The first `POST /guest/session`
takes that row; a replayed fragment — from a screenshot, a shared screen, or a
synced browser history — finds it taken and is refused with `401`. That is what
makes the link genuinely one-time, and it happens **before** any data is entered,
so a stolen link cannot be silently used behind the guest's back.

What authorizes the submission is the session, not the link:

| Property | Value |
|---|---|
| Lifetime | 30 minutes maximum (`SESSION_TTL_MINUTES`) |
| Storage | SHA-256 digest only, in `portal_sessions` |
| Transport | JSON body, held in a closure — never `localStorage`, `sessionStorage`, or a cookie |
| End of life | Revoked the moment the intake lands, and on expiry |

A link lives up to **7 days** (`ttl_days`, 1–7); a session lives minutes. The
first is handed to a person, the second is held by an open tab.

## What the guest is asked

The intake vocabulary is `spec/care_profile.schema.json`, so what a guest types
lands in the field the production already reads. Every field is optional —
absence means "not provided", never "not applicable".

| Group | Fields |
|---|---|
| Text | `name_pronunciation`, `pronouns`, `preferred_beverage`, `notes` |
| Lists | `dietary_restrictions`, `allergies`, `accessibility_needs`, `topics_off_limits` |

The consent step is a **click-wrap**: five clauses drawn from the `consent_type`
enum in `spec/consent.schema.json` (`recording_release`, `likeness_use`,
`transcript_publication`, `clip_use`, `care_profile_storage`), an explicit
affirmative tick, and nothing pre-checked. The exact wording is checksummed into
the `consent.signed` receipt, so editing the copy later cannot rewrite what a
guest actually agreed to. `promotion_required` is pinned `false` in the record
rather than read from the payload — a guest is never required to promote the
show, and that is an invariant, not a setting.

The date step asks for exactly **three different dates**. When the operator
published availability, the three must come from that slate; when they did not,
the guest proposes three.

## Where the answers go

| Value | Custody |
|---|---|
| Care profile | Sealed with the tenant field vault, category `correspondence`; reaches `portal_intakes.intake_ref` only as a `private-field://` handle |
| Consent record | Sealed likewise, category `consent` → `portal_intakes.consent_ref` |
| Three dates | Legible rows in `portal_date_choices` — scheduling facts the production must act on, not private content |

The portal refuses to accept private answers it cannot encrypt: with no field
vault configured, `POST /guest/intake` fails closed with `503` rather than
storing a care profile in the clear.

## Receipts

One completed intake writes three rows to `portal_receipts`, keyed
`UNIQUE(portal_intake_id, event_type)`:

| Event | Details recorded |
|---|---|
| `guest_intake.completed` | Which care-profile *fields* were provided — never their values |
| `consent.signed` | Consent version, clause checksum, granted consent types, `promotion_required: false` |
| `availability.provided` | The three dates, and whether they came from an offered slate |

They live in their own ledger rather than `operational_receipts`, which is keyed
to an `appearance_opportunities` foreign key, carries no `show_id`, and already
gives `consent.signed` a narrower meaning with its own state transition. The
portal is scoped `(tenant, show, guest)` and is reached without an operator
identity, so it keeps its own table — the isolated boundary is the point.

## The operator side

A **Send portal link** button appears on a candidate card only when all of these
hold, and the dashboard derives that from `/v1/operator-context`:

- the candidate is `BOOKED` (`PORTAL_ELIGIBLE_STATES`),
- the role is producer, editorial owner, or relationship owner,
- the show's `guest_interaction` mode is `portal`,
- `HOSPES_PORTAL_SECRET`, `HOSPES_PORTAL_BASE_URL`, and private-field custody are configured.

The minted URL is rendered into the card **once** and never re-shown on refresh.
There is no send route: a human carries the link through a channel they already
have, exactly as with every other outbound path in HOSPES.

`GET /v1/portal-status` returns one row per guest that has been sent a link —
`not_sent → sent → opened → completed`, or `expired` — plus the three chosen
dates and which receipts landed. It never returns a private value, so the badge
costs no unsealing.

## Browser policy

The portal serves its own documents under `default-src 'none'; script-src
'self'; connect-src 'self'; style-src 'self'; img-src 'self'; form-action 'self';
base-uri 'none'`, and every response carries `x-robots-tag: noindex, nofollow`
and `cache-control: no-store`. Its markup, script, and stylesheet are tracked
files under `dashboard/guest/` (mirrored byte-identically into
`hospes/resources/dashboard/guest/`), so there is no inline script or style to
exempt. The brand — name, logo, favicon, palette — comes from the same tracked
YAML as every other surface; see [`white-label.md`](white-label.md).

## Configuration

```yaml
# config/shows/<show>.yaml
modes:
  guest_interaction: portal      # default: operator_packet
```

```bash
export HOSPES_PORTAL_SECRET=...        # HMAC signing material for the link
export HOSPES_PORTAL_BASE_URL=https://guests.example.com
export HOSPES_MASTER_KEY_B64=...       # private-field custody
```

A show may publish a guest-facing hostname with `custom_domain` in its brand
file; the link is then minted against that host instead of the base URL. Any of
these missing is a visible `503` naming what is unconfigured — never a silently
degraded link.
