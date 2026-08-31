# HOSPES

**The invisible guest, production, and distribution desk installed around a host.**

> *hospes* (Latin): guest AND host. The word contains both roles — the system serves both.
> Blueprint working name `conversation-operations-system` is an alias; this repo is HOSPES.

---

## What it is

HOSPES is not another AI agent. It is a **composition boundary** — the podcast-specific domain
kernel that assembles an already-built organ constellation into one coherent product.

You built the organs. HOSPES assembles the organism.

The intelligence is backstage. The host, the guest, the relationship, and the performance remain
entirely human. HOSPES drafts; humans decide and send.

---

## The three-layer model

```
┌─────────────────────────────────────────────────────────┐
│  FLAGSHIP SHOW                                          │
│  Host + Producer • studio LA / NYC / Austin • hidden mic  │
├─────────────────────────────────────────────────────────┤
│  FIELD SHOW                                             │
│  Producer solo • other people's spaces • GoPro kit       │
│  Place → Object → Intervention → Artifact               │
├─────────────────────────────────────────────────────────┤
│  REUSABLE OS                                            │
│  Show DNA config • multi-tenant • sellable to network   │
└─────────────────────────────────────────────────────────┘
```

Each layer is a `Show DNA` instance (`dna/show.schema.json`). The OS layer means the same
system, cleanly tenanted, runs for outside podcasters in the network — managed-service-first.

---

## Composition, not greenfield

The existing GitHub estate already contains most of the horizontal machinery:

| Domain | Source organ |
|---|---|
| Relationship CRM + network graph | `application-pipeline` |
| Correspondence drafts + inbox triage | `universal-mail--automation` |
| Booking interface + reminder patterns | `mirror-mirror` |
| Canonical media archive | `media-ark` |
| Transcript atomization + clip assembly | `materia-collider` |
| Episode production templates | `praxis-perpetua` + `Object Lessons` |
| Content fragmentation + distribution | `content-engine--asset-amplifier` |
| Voice/persona registry + validation | `vox--architectura-gubernatio` |
| Production capture profiles | `multi-camera--livestream--framework` |
| Human-approval + audit machinery | `aerarium` |

HOSPES composes these through **adapters** (`adapters/`) — it never forks or merges a source repo.

---

## Status — acceptance slice: GREEN

The baseline commit established the domain packet. This slice implements it end-to-end; every
clause is enforced by `tests/test_almost_working.py` and the `done.sh` predicate:

1. create an appearance opportunity — `hospes/pipeline.py`
2. attach a thesis and relationship-safe contact route — required fields + `spec/permission-matrix.yaml`
3. approve, reject, protect, or annotate — `hospes/approvals.py` + `dashboard/index.html`
4. generate a correspondence draft without sending from tracked
   `config/outreach_templates.yaml` and `config/voice.yaml` — `hospes/drafts.py` + `hospes/voice.py`
5. route an accepted guest to LA, NYC, or Austin — `hospes/routing.py`
6. produce a research and segment brief — `hospes/briefs.py`
7. declare the recording asset package — `hospes/assets.py`
8. track commitments and follow-ups — `hospes/commitments.py`

`config/domain_kernel.yaml` remains the seed domain contract; `spec/` is its elaborated,
machine-validated form (schemas, states, events, permissions). Baseline copies of the design
packages live under `docs/blueprint/` and `docs/product/`; verbatim provenance sets under `upstream/`.

---

## Quickstart

```bash
# 1 — Install
cd /Users/4jp/Workspace/hospes
pip install -e '.[test,api]'

# 2 — Run the demo (idempotent; safe to run twice)
PYTHONPATH=. python3 -m hospes demo

# 3 — Validate all specs
PYTHONPATH=. python3 -m hospes validate

# 4 — Run the goal predicate (green = done)
./done.sh

# 5 — Start the live localhost operator (token is read from env, never argv)
pip install -e '.[api]'
export HOSPES_DB="$HOME/Library/Application Support/HOSPES/private_pilot/hospes.sqlite3"
IFS= read -r -s -p "Operator token: " HOSPES_OPERATOR_TOKEN; printf '\n'
export HOSPES_OPERATOR_TOKEN
python3 -m hospes operator --db "$HOSPES_DB" \
  --actor example_operator --role producer --tenant private_pilot
unset HOSPES_OPERATOR_TOKEN
```

The dashboard opens at `http://127.0.0.1:8765/operator/`. Its live authority is SQLite; the
separately labeled CSV mode is an in-memory demo/export fallback only.

To simply *see* HOSPES with no setup at all, one command builds an ephemeral marked bundle,
serves it on a loopback port, and opens it with a one-time 60-second bootstrap link:

```bash
PYTHONPATH=. python3 -m hospes demo --open
```

To start a *new* show with no YAML editing at all, `hospes init` asks eight
questions and leaves a workspace that is already validated and demonstrated:

```bash
mkdir new-podcast
PYTHONPATH=. python3 -m hospes init --root new-podcast
```

The wizard generates the show DNA, show contract, partnership record, brand,
analytics, and notification configuration plus a canonical-header
`data/example-pipeline.csv`, validates that workspace with the engine's own validators,
runs the demo scoped to it, and appends its receipts to `<root>/out/audit.log`.
Rerunning is refused rather than destructive (`--merge` is the explicit
fill-the-gaps rerun), and the optional GitHub repository is *requested*, never
created, until an explicit human authorization receipt is supplied. The eight
answers, the generated files, and the authorization gate are documented in
[`docs/onboarding-wizard.md`](docs/onboarding-wizard.md).

## Guided demo

Anyone can open the full demonstration and be walked through it — one command, no setup:

```bash
python3 -m hospes demo --open --persona newcomer   # owner | ari | investor | newcomer
```

It builds an ephemeral marked bundle, opens a browser on a one-time nonce, and destroys
everything on exit. A **guided tour** rail runs beside the live cockpit: 14 beats that spotlight
real elements, a depth selector (`pitch` / `tutorial` / `practitioner` / `expert`) changeable
mid-tour, a `?` tooltip on every instrumented control, and a capability legend computed from the
live session — so it can never explain a control the surface does not offer.

Every persona carries full `relationship_owner` authority; only the narration depth differs. The
demo exists to show the human gates working, and a viewer who cannot reach a gate cannot judge it.

The whole thing is declared in [`dashboard/assets/guide.json`](dashboard/assets/guide.json) and
held in parity with the code by `scripts/check_guide_registry.py` — a capability that ships
without an explanation fails the gate. Details in
[`docs/synthetic-demo.md`](docs/synthetic-demo.md).

## Demo video

The walkthrough records itself. `scripts/record-demo.sh` boots the marked synthetic bundle,
films the operator surface with Playwright, and renders an mp4:

```bash
HOSPES_DEMO_CUT=proof    scripts/record-demo.sh   # fast, uncaptioned evidence cut
HOSPES_DEMO_CUT=pitch    scripts/record-demo.sh   # paced, captioned walkthrough
HOSPES_DEMO_CUT=tutorial scripts/record-demo.sh   # films the shipped guided tour
```

Videos land in `artifacts/demo-video/` (gitignored). The **recorder is the durable artifact**,
not the file — this repo tracks no binaries, and any cut is reproducible from a clean checkout.

The recorder never stubs an endpoint. It films whatever the synthetic bundle actually renders,
and a beat whose data is genuinely absent is skipped and named in the run manifest rather than
faked. It also refuses to record at all unless the visible `SYNTHETIC DEMO — NOT AUTHORITY`
marker is present and the tenant is a demo tenant, so it can never be pointed at Pilot authority.
Both the operator token and the private-field master key are minted per run and never persisted.

For the isolated, resettable Host walkthrough, build two marked synthetic stores:

```bash
python3 -m hospes seed-synthetic-demo \
  --output-dir "$HOME/Library/Application Support/HOSPES/private_pilot/demo" \
  --replace-demo
```

The interactive review and SQLite-query-only 14/14 specimen are installed as one
locked, journaled marker-v2/build-receipt-v2 generation. Deterministic fixture
identity is scoped to seeding; live operation retains the UTC clock and UUID4.
The optional exact-readback Cloudflare Access launch is documented in
[`docs/synthetic-demo.md`](docs/synthetic-demo.md). They never touch canonical
Pilot authority.

Private candidates enter through an external CSV that is never copied into Git:

```bash
python3 -m hospes import-candidates /private/path/candidates.csv \
  --tenant private_pilot --network example_network --show flagship_private_pilot \
  --actor example_operator --role producer
```

The optional `contact_roster` CSV column is a private JSON object with
`publicist`, `manager`, `agent`, and `direct` entries. Its raw names, email
addresses, phone numbers, and notes require the credential-wall-backed
`HOSPES_MASTER_KEY_B64` and are encrypted before persistence. Public API
contracts expose only opaque references; authorized operator sessions may
reveal a verified route transiently for invitation prefill. See
[`docs/contact-roster.md`](docs/contact-roster.md).

Authenticated operators can preserve an informal text, email, Instagram,
in-person, phone, or hallway conversation from Pilot Workbench with **Log
chat**. HOSPES derives the initiator from the session, encrypts the private note,
and projects it into guest and Executive Overview timelines without changing
workflow state. The scoped API, filter contract, and custody boundary are in
[`docs/informal-touchpoints.md`](docs/informal-touchpoints.md).

A show meets a guest more than once. Every opportunity carries an opaque
cross-season `guest_id`, a terminal decision on a season-labelled opportunity
writes that season into `guest_history`, and the approval card renders the last
real interaction as **Previously: S2E3 — SOFT_DECLINE (2024-03-15)**. The
relationship owner — and only the relationship owner — can set or lift a
do-not-contact directive from that same card; once set, HOSPES refuses the
guest at import, draft preview, draft persistence, and outreach receipts, and
withholds them from suggestions and nurture cadence. The identity model, the
bounded disposition vocabulary, the directive's legacy fallback, and the
Complete Register timeline are documented in
[`docs/guest-crm.md`](docs/guest-crm.md).

Public archive alumni can be prepared as an import-compatible review stream
without discovering or enabling a contact route:

```bash
python3 -m hospes suggest-guests --min-gap-years 2 --max-social-cost 3 \
  | python3 -m hospes import-candidates - \
      --tenant private_pilot --network example_network --show flagship_private_pilot \
      --actor example_operator --role producer
```

The imported route remains explicitly unverified and unusable until a human
operator verifies it. Filters, public tour provenance, the versioned CSV
contract, and deterministic review behavior are documented in
[`docs/guest-suggestions.md`](docs/guest-suggestions.md).

Trace a tenant/show-scoped route from a public name or opaque alias and export
the same redacted projection in Mermaid, Graphviz, JSON, or CSV:

```bash
python3 -m hospes network-map --guest "Theo Von" --depth 2 --format mermaid
python3 -m hospes network-map --guest "Theo Von" --depth 2 --format csv
```

The map combines scoped database edges, `config/network_edges.yaml`, and the
checkout-level public RSS archive. C2/C3 alumni on reachable shortest paths are
highlighted. Private aliases render only as `Private relationship`; their
custody references never enter graph output. The configuration, archive
adapter, dashboard review panel, and export contracts are documented in
[`docs/network-map.md`](docs/network-map.md).

One dashboard operates every show in the estate. URL and session show state
drive isolated reloads, `GET /v1/shows` powers the switcher with per-show
profiles, and each show resolves its own DNA, voice, templates, branding, and
pilot policy through a traversal-safe resource resolver with negative
cross-show authorization. The show-state contract, per-show resource roots,
and deny paths are documented in [`docs/multi-show.md`](docs/multi-show.md).

Every episode carries a rights checklist. Pilot Workbench's **Clearance** tab
lists, adds, edits, and filters music, clip, IP, footage, and guest-likeness
items, shows a per-episode badge such as `⚠ 2 clearances pending`, and exports
the report as CSV. Rights holder and licence terms are encrypted at rest when
field custody is configured; without it the operator supplies an opaque external
custody reference and the record says so. Every status decision requires an
opaque evidence reference and appends an immutable receipt, and publication is
denied for **every** status other than `cleared` — pending and denied alike — at
draft, authorization, and mark-published. The schema, gate, receipts, and API
are documented in [`docs/rights-clearance.md`](docs/rights-clearance.md).

The Revenue view tracks sponsor and ad inventory: declared slots per episode, who holds them, sold versus available, recognized revenue in integer minor units, and a five-column accounting CSV. Cited sponsor claims stay unapproved until an editorial or relationship owner approves them, and a committed slot left unsold or an unapproved claim blocks publication through the same predicate that enforces rights clearance — both rules per-show configurable. The tables, lifecycle, authorization matrix, and HTTP surface are documented in [`docs/sponsor-inventory.md`](docs/sponsor-inventory.md).

Each show is also white-labelled from tracked YAML. `config/brand.yaml` is the
installation default and `config/brands/<show>.yaml` overrides the show name,
logo, favicon, palette, typeface, and custom domain; the operator serves those
assets at `/operator/brand/logo` and `/operator/brand/favicon.ico`, injects the
`--brand-*` CSS variables under a content-security policy that names their
exact style hash, and brands the login page and guest portal from the same
source. Re-branding is an edit and a restart, never a code change — the fields,
routes, browser policy, and custom-domain contract are documented in
[`docs/white-label.md`](docs/white-label.md).

A lifecycle transition that creates work for another role says so. Approving a
candidate notifies the producer, a prepared brief notifies the host, a confirmed
booking dates the producer's prep task, and a completed guest recording raises a
critical clip task for the editor. The cockpit header carries an **Alerts** bell
with an unread badge and a filterable notification centre, and the **My Queue**
tab lists that role's tasks by due date with overdue work marked. Emission is
idempotent and transactional, only the addressed role may mark its own
notifications read, and email or webhook output is a checksummed, critical-only
preview that requires a human authorization receipt and an immutable delivery
receipt — HOSPES still never sends. See
[`docs/team-notifications.md`](docs/team-notifications.md).

Read what happened after an episode shipped. Analytics is a **read organ**: it
verifies every configured provider, records a provider receipt, and imports each
provider's declared export — never sending, publishing, or substituting one
provider for another:

```bash
python3 -m hospes fetch-analytics --tenant hospes --show flagship
python3 -m hospes analytics trends --show flagship --limit 12
python3 -m hospes analytics export --show flagship --out analytics.csv
```

Every stored metric validates against `spec/analytics.schema.json` and carries
the `source_receipt_ref` of the provider receipt that authorized its read.
Scheduled pulls are enqueued as `analytics.fetch` with `operation=read`, so
measurement can never acquire publish authority. A provider without a declared
export stays visibly `unconfigured` and one behind an unprovisioned credential
wall stays `blocked`. The cockpit's Analytics view charts the last episodes,
lists per-episode downloads, retention, and top-clip views, and exports
reporting CSV. The record shape, provider states, trend aggregation, API, and
export-safety rules are documented in
[`docs/analytics.md`](docs/analytics.md).

A producer or editorial owner can click **Research** on an approval card and get
a cited brief instead of an afternoon of searching. Brave, SerpAPI, SearXNG, and
an allowlisted crawler each run one cost-capped query as a durable background
job, keep only citations from the operator's own source allowlist, record every
refused source as a visible risk flag, and write a provider receipt naming the
source, endpoint, and spend. Nothing reaches an episode until a human verifies a
claim, records a counterargument, approves the review, and locks the brief,
which is then frozen against its own content checksum. The source and cost
policy, the lifecycle, the receipts, and the deny paths are documented in
[`docs/research-agent.md`](docs/research-agent.md).

A booked guest can also complete their own intake. The guest portal is off by
default and runs as a separate application: the operator mints a one-time link
whose token rides in the URL fragment, a human carries it to the guest, and the
browser exchanges it exactly once for a session measured in minutes. The guest
fills in a care profile, ticks a click-wrap consent whose wording is checksummed
into the receipt, and picks three dates from the production's availability;
submitting writes `guest_intake.completed`, `consent.signed`, and
`availability.provided`, and the dashboard shows "Guest completed portal". The
answers are sealed with the tenant field vault and never reach the operator as
plaintext. The boundary, token model, session, receipts, and configuration are
documented in [`docs/guest-portal.md`](docs/guest-portal.md).

The Publish view carries the distribution pipeline: three validated package families — an RSS item, YouTube metadata, and a short-form clip queue — with a preview that renders the exact artifact each platform receives. HOSPES publishes nothing. A named human authorizes the publication, scheduling requires that receipt first, both the rights and sponsor gates re-run at mark-published, and every delivery attempt — succeeded or failed — appends one immutable delivery receipt at attempt *n+1*, so a retry is always distinguishable from a first try. A missing credential stays a visible `unconfigured` adapter state, and an auto-publish webhook stays declared but unarmed. The packages, lifecycle, evidence tables, adapters, and HTTP surface are documented in [`docs/publishing-pipeline.md`](docs/publishing-pipeline.md).

A network operator runs a portfolio rather than a show. The **Network** view is
the one surface that reads across the shows of a tenant: pipeline health per
show (candidates, approved, booked), booking velocity over the trailing 28
days, sponsor revenue taken from the sponsor organ rather than recomputed, and
the guest-overlap matrix that names every guest two shows are both pursuing.
Any row drills into that show's own dashboard. The whole portfolio exports as a
PDF, HTML, or JSON health report, and each export appends an attributable
receipt carrying the SHA-256 of the exact document handed over. The role gate,
tenant scoping, milestone counting, report escaping, and receipt trail are
documented in [`docs/network-dashboard.md`](docs/network-dashboard.md).

Seed the reusable Partnership Command Center before the first Host review:

```bash
python3 -m hospes import-partnership config/partnerships/example-private-pilot.yaml \
  --tenant private_pilot --actor example_operator --role producer
```

After the schema-v7 custody checkpoint, import the immutable Pilot 1 policy using the internal
partnership id returned by that command:

```bash
python3 -m hospes import-pilot-policy config/pilot_policies/example-pilot-1.yaml \
  --partnership PARTNERSHIP_ID --tenant private_pilot \
  --actor example_operator --role producer
```

Policy imports are checksummed and idempotent. Candidate records, correspondence, calendar
contents, and contact details are not part of the policy document.

The **Partnership Cockpit** then gives Host and Producer three connected views: an executive agenda,
the Pilot Workbench, and a versioned Complete Register spanning engine, plans, roles, agreements,
deals, obligations, decisions, receipts, risks, and explicitly unknown items. The schema is
partnership-neutral: another collaboration gets its own tenant and template, not a fork of the
interface. Contracts, signatures, private terms, and financial details remain with their canonical
external owners; HOSPES keeps bounded summaries and opaque references only.

---

## Service layer / API

Beyond the file-based demo, HOSPES exposes a **stateful, human-gated service layer** for guest
operations (private candidate import → thesis/contact → human decision → draft → opaque external
receipts → booking/consent/preflight gates → recording/media receipts). It is a thin
layer over the same engine: lifecycle statuses are validated against the canonical state machine
(`hospes/states.py`), the protected-relationship (C4/C5) rule and the DRAFT-NOT-SENT banner come
from `hospes/drafts.py`, and — like the rest of HOSPES — **it drafts, it never sends** (there is no
delivery endpoint).

The versioned Pilot execution interface adds three human-gated routes:

- `POST /v1/partnerships/{id}/pilot-runs` starts a run only after Host review, a valid three-person
  slate, three verified routes, and an agreed opaque calendar window exist.
- `GET /v1/partnerships/{id}/pilot-runs/{run_id}/plan` returns the current revision, ranked action,
  alternatives, latest-safe timing, constraints, rationale, and evidence.
- `POST /v1/partnerships/{id}/pilot-runs/{run_id}/decisions` records a revision-checked `wait`,
  `follow_up`, `promote`, `activate_set`, `fallback_rehearsal`, or `pause` decision.

`activate_set` carries one or more internal assignment ids and individual `not_before` timestamps;
the planner derives single, staggered, parallel, or mixed timing from evidence rather than storing
one global outreach doctrine. Multiple overlapping asks require independent route and owner ids
plus a relationship-owner decision. The interface can draft initial and follow-up correspondence,
but it has no send operation and persists only reviewed template metadata.

- **`hospes/service.py`** — the domain operations. Persistence is local `sqlite3` or hosted
  PostgreSQL via **`hospes/store.py`**; the local database is a
  development artifact at `out/hospes.sqlite3`, overridable with the `HOSPES_DB` env var. Operator
  mode requires an explicit `--db`, `HOSPES_DATABASE_URL`, or `HOSPES_DB`; the development path is
  never implicit live authority. Private fields and artifacts use tenant-scoped AES-256-GCM
  envelope encryption from **`hospes/encryption.py`** and **`hospes/artifacts.py`**.
- **`hospes/contact_roster.py`** — private candidate-roster validation,
  field-by-field encryption, tenant/show-scoped public references, authorized
  transient reveal, and verified invitation prefill. It never sends.
- **`hospes/api.py`** — an **optional** HTTP surface (FastAPI) over the service layer,
  import-guarded so the local CLI does not require the API extra. Install and run it with:

  The supported operator entrypoint is `python3 -m hospes operator`; it binds only to `127.0.0.1`, keeps the
  bearer token server-side, and gives the browser an HttpOnly process-local session cookie.

  Actor, role, and tenant are bound once at process launch. The browser cannot replace them with
  request headers, and raw `/v1` access is disabled in operator mode unless the development-only
  `--enable-raw-v1` flag is supplied. The boundary fails closed when the token is unset.

  Tunnel and hosted profiles instead validate the Cloudflare Access application JWT at the
  origin—RS256 signature, issuer, audience, expiry, issued-at time, and rotating JWKS—then resolve
  its one-way subject digest through HOSPES's tenant/show identity mappings. Every browser mutation
  also requires an identity-bound double-submit CSRF token. See
  [`docs/authentication-and-jobs.md`](docs/authentication-and-jobs.md).

- **`hospes/jobs.py`** — a tenant/show-scoped durable queue with PostgreSQL row leases (and a
  serialized SQLite local rail), bounded retries/timeouts/cost, idempotency keys, hashed lease
  tokens, and immutable attempt receipts. Run registered local handlers with:

  ```bash
  python3 -m hospes jobs run --db /private/path/hospes.sqlite3 --worker operator_worker
  ```

  The hosted internal wake endpoint is credential-gated. Scheduled work is restricted to
  analytics/research reads; it cannot send or publish.

The service-level tests (`tests/test_service.py`) run unconditionally; the HTTP tests
(`tests/test_api.py`) skip cleanly when the `api` extra is absent, so `done.sh` stays green either
way.

---

## Layout

```
hospes/                 Python package — domain kernel, pipeline, triage, routing
hospes/service.py       Guest-operations service layer (human-gated; drafts, never sends)
hospes/store.py         SQLite/PostgreSQL connection protocol with custody-safe migrations
hospes/encryption.py    Tenant DEK wrapping and authenticated private-field encryption
hospes/artifacts.py     Encrypted local and S3-compatible artifact stores
hospes/authentication.py  Process-bound local and origin-validated Access identity/CSRF
hospes/jobs.py          Durable leased jobs and immutable attempt receipts
hospes/api.py           Optional FastAPI HTTP surface (pip install -e '.[api]')
hospes/operator.py      Localhost authenticated dashboard/API composition
hospes/partnerships.py  Safe template import and public partnership facade
hospes/partnership_projections.py  Live coverage, agenda, capability, and pilot readiness views
hospes/partnership_records.py      Revisioned items, links, reviews, and candidate selection
tests/                  pytest suite (test_almost_working.py, test_triage.py, fixtures/)
spec/                   JSON Schemas + permission-matrix.yaml + states/events
dna/                    Show DNA configs (show.schema.json, field.show.yaml, …)
adapters/               Thin adapter stubs per source organ
briefs/                 Pilot episode brief templates
data/                   Guest network seed data (unlicensed-therapy/, …)
upstream/               Preserved v0 seed packages (podcast_os_v0_starter_pack/, recomposition/)
dashboard/              Modular live approval UI plus labeled in-memory CSV fallback
config/partnerships/    Safe tracked seed templates; no contracts or private deal terms
docs/                   Design docs (asks ledger, provenance, network doctrine, …)
scripts/                Utility scripts (check_asks.py, …)
out/                    Runtime outputs — gitignored
done.sh                 The executable goal predicate (exit 0 = done)
Makefile                Convenience targets
```

---

## Naming note

**HOSPES** is the Latin word that simultaneously means *guest* and *host*. A single word for the
person who visits and the person who receives — exactly the duality this system serves.

The blueprint's working name `conversation-operations-system` is an alias used in provenance docs.
Use HOSPES everywhere in code and tooling.

---

## Safety line

**HOSPES drafts correspondence; it never sends.** Sending is a human-gated action by design,
enforced in `spec/permission-matrix.yaml`. Every outreach path through the system terminates at a
draft awaiting human review. The operator delivers; the system prepares.

See `spec/permission-matrix.yaml` for the full permission surface.

Additional standing rules (from the repo agent protocol):

- No secrets, credentials, mailbox contents, or private guest data in Git.
- Raw prompt transcripts and source exports remain in private corpus custody outside this repository.
- Relationship ownership and protected-contact decisions are explicit policy, never inferred permission.
