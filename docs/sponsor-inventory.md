# Sponsor and ad inventory

The Revenue surface answers two questions a booking desk otherwise keeps in a spreadsheet:
**what is still for sale on this episode**, and **what did this episode earn**. It is
tenant/show scoped, human-gated, and it never invoices, signs, or sends anything.

Issue: `github://organvm/hospes/issues/25` · contract: `config/domain_kernel.yaml` →
`sponsor_inventory_contract` · predicate: `python -m pytest tests/issue_predicates/test_issue_25.py -q`

## Why inventory is its own table

Migration 008 shipped `sponsors` and `sponsorships`. That pair can say *this sponsor holds
the mid-roll on episode 12*; it cannot say *the post-roll is unsold*, because an unsold slot
had no row anywhere. "Sold versus available" was therefore unrepresentable, and so was the
committed-slot publication gate. Migration 016 adds the missing rows:

| Table | What it owns |
|---|---|
| `sponsor_slots` | The declared ad inventory for one episode: slot type, list rate, currency, and whether the slot is **committed**. |
| `sponsorships` | One declared slot assigned to one sponsor as `reserved` or `sold`, with the recorded rate and `sold_at`. |
| `sponsor_claims` | Claims governance: claim text, public source URL, verification date, and explicit external-use approval. |
| `sponsorship_receipts` | Append-only attributable evidence for every inventory, assignment, release, and claim decision. |

`ux_sponsorship_slot` makes one episode slot sellable to exactly one sponsor, so double-selling
is a database error rather than a reconciliation surprise.

## Slot lifecycle

```
declare inventory ──▶ available ──▶ reserved ──▶ sold
                          ▲             │           │
                          └──── release ┴───────────┘
```

* **available** — declared, nobody holds it.
* **reserved** — a sponsor holds it, but the deal has not closed. Earns nothing.
* **sold** — recognized revenue, and the only state that fills a committed slot.

Declaring inventory is an idempotent PUT of the complete slot set for one episode. A slot the
payload omits is withdrawn — but only while nothing is assigned to it. Withdrawing inventory a
sponsor already holds would silently erase a sale, so that case returns `409` and the operator
resolves it explicitly by releasing the sponsorship first.

## Money

Rates are integers in **minor currency units** with an ISO 4217 code (`USD` by default), so no
total is ever a float. Recognized revenue counts `sold` assignments only. The accounting export
emits exactly five columns — `Episode, Sponsor, Slot, Rate, Date` — with the rate rendered in
major units and every cell neutralized against spreadsheet-formula prefixes (`=`, `+`, `-`, `@`).

## Claims governance

Every factual claim an ad read makes about a sponsor is recorded with its public source URL and
the date it was verified against that source, following `spec/claim_evidence.schema.json`. A new
claim is **unapproved**. Only an `editorial_owner` or `relationship_owner` may approve it for
external use or withdraw that approval; a producer who may sell the slot may not approve the
claim about it. Approval and withdrawal both write a receipt naming the approver and their role.

## The publication gate

`platform.publish_blockers` is the single predicate `hospes.distribution` already consults before
a draft, an authorization, or a publish. It returns uncleared rights items first (callers read
that position), then the sponsor blockers:

* `sponsor_slot_unfilled` — a **committed** slot on this episode is not sold.
* `sponsor_claim_unapproved` — a sponsor holding a sold slot has at least one claim that is not
  approved for external use.

Both rules are configuration, not code:

```yaml
# config/runtime.yaml — the safe default for every show
runtime:
  sponsor_inventory:
    currency: USD
    slot_types: [pre, mid, post]
    block_publication_on_unfilled_committed_slots: true
    require_approved_claims_before_publication: true
```

```yaml
# config/shows/client-x.yaml — a show that sells no committed inventory
sponsor_inventory:
  block_publication_on_unfilled_committed_slots: false
  require_approved_claims_before_publication: true
```

Show configuration wins over the runtime default (`configuration.sponsor_inventory_policy`), and
an unregistered `show_id` falls back to the runtime default rather than failing closed — tenants
create shows in the store before anyone writes a tracked configuration file, and the revenue
surface must still run under the safe default.

## Authorization

| Capability | Roles |
|---|---|
| Read inventory, revenue, receipts | `host`, `network_operator`, `producer`, `editorial_owner`, `relationship_owner` |
| Register sponsors, declare inventory, sell and release slots, record claims | `producer`, `editorial_owner`, `relationship_owner` |
| Approve or withdraw a claim's external use | `editorial_owner`, `relationship_owner` |

Actor id and role always come from the authenticated operator session, never from a payload.
Every route is bound to one tenant/show: a request whose identity is scoped to another show
returns `403` before any lookup can leak state.

## Privacy

Sponsor contact details and signed deal terms stay with their external owner. The record holds
only opaque custody references (`vault://sponsor/contact`), and a raw contact-like value is
rejected at the write boundary by the same `privacy.contact_kind` check the rest of the estate
uses. Every revenue response carries `Cache-Control: no-store, private`.

## HTTP surface

| Method | Path |
|---|---|
| `GET` | `/v1/shows/{show_id}/sponsors` |
| `POST` | `/v1/shows/{show_id}/sponsors` |
| `POST` | `/v1/shows/{show_id}/sponsors/{sponsor_id}/claims` |
| `POST` | `/v1/shows/{show_id}/sponsors/{sponsor_id}/claims/{claim_id}/approval` |
| `GET` | `/v1/shows/{show_id}/ad-slots` |
| `PUT` | `/v1/shows/{show_id}/ad-slots` |
| `POST` | `/v1/shows/{show_id}/ad-slots/allocations` |
| `POST` | `/v1/shows/{show_id}/ad-slots/releases` |
| `GET` | `/v1/shows/{show_id}/revenue` (`?format=csv` for the accounting export) |
| `GET` | `/v1/shows/{show_id}/sponsorship-receipts` |
| `GET` | `/v1/shows/{show_id}/episodes/{episode_id}/publication-gate` |

There is deliberately no invoice, payment, or send route.

## Operator UI

The dashboard's fourth view, **Revenue**, renders the required table — Episode | Sponsor |
Slots Sold/Total | Revenue — over a per-episode summary row and one row per sponsor holding a
slot on it. Beside it sit the slot inventory with release controls, the publication gate, the
sponsors-and-claims register with approval controls, an **Add sponsor** modal, and the
**Export CSV for accounting** button. Write controls are hidden for roles that cannot use them,
and every rendered sponsor name, claim, and source URL is HTML-escaped.

## Worked example

```
Add sponsor        → "Athletic Greens", vault://sponsor/ag/contact, vault://sponsor/ag/terms
Declare inventory  → episode-12: pre 1500.00 committed, mid 1500.00 committed, post 1500.00
Assign             → pre sold, mid sold, post reserved
Revenue            → Ep 12 · Athletic Greens 2/3 · 3000.00 USD
Export CSV         → Episode,Sponsor,Slot,Rate,Date
                     episode-12,Athletic Greens,mid,1500.00,2026-08-14
                     episode-12,Athletic Greens,pre,1500.00,2026-08-14
```
