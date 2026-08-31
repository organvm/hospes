# Network dashboard: the portfolio view for network operations

A show operator opens one cockpit. A **network operator** runs several shows at
once, and until this view existed the only way to answer "how is the network
doing" was to open each show's dashboard in turn and add the numbers by hand.
`hospes/network_dashboard.py` is the one surface that reads *across* the shows
of a tenant. Issue #32 is closed by
`tests/issue_predicates/test_issue_32.py`, which proves every contract on this
page against the live service, API, dashboard, and receipt surfaces.

## One role, one tenant

Two boundaries carry the whole surface, and they are enforced in the module
rather than at the edge:

- **One role.** Every entry point takes the authenticated operator's role and
  admits `network_operator` alone. A `host`, `producer`, `editorial_owner`, or
  `relationship_owner` is refused **even for a show they otherwise operate** —
  "how is the portfolio doing" is a different question from "how is my show
  doing", and only the network seat is scoped to ask it. The seat is declared
  in [`spec/permission-matrix.yaml`](../spec/permission-matrix.yaml) under
  `team_roles`, where it is the only role whose `scope` is `network`.
- **One tenant.** Scope comes from the authenticated identity's tenant and is
  never widened by a payload or a path segment. Every statement in the module
  carries `tenant_id = ?`, and the drilldown resolves its show through *that
  tenant's* active show registry, so a show id belonging to another tenant is a
  `404` rather than a leak.

The role gate lives in the module *and* on the HTTP routes: `api.py` refuses
any non-network identity before the request reaches the service, and the
service refuses it again for a direct caller. The dashboard hides the Network
tab for every other role, which is a courtesy — not the enforcement.

The portfolio writes no show state. It books nothing, decides no candidate,
approves no claim, and sends nothing. It is a projection over rows the per-show
views already own.

## What the portfolio answers

`network_dashboard.portfolio(conn, tenant_id=…, actor_role="network_operator")`
returns one row per **active** show plus tenant totals:

| Field | Meaning |
|-------|---------|
| `pipeline.candidates` | every appearance opportunity on the show |
| `pipeline.approved` / `booked` / `published` | candidates that have **reached or passed** that milestone |
| `pipeline.off_ladder` | candidates in a branch state (`DECLINED`, `PAUSED`, `DO_NOT_CONTACT`, …) |
| `booking_velocity.bookings_per_week` | bookings inside the trailing 28-day window, per week |
| `revenue` | currency, sold slots, recognized `revenue_minor`, committed-but-unfilled slots, publication-blocked episodes |
| `summary` | the per-show counters the show's own cockpit already publishes |

The **booking velocity** figure is deliberately a trailing rate rather than a
lifetime count: bookings inside the last `VELOCITY_WINDOW_DAYS` (28) divided by four
weeks. Four weeks is the shortest window that survives one skipped recording
week without reading as zero, and a booking older than the window drops out —
so the number answers "is this show booking *now*", not "has it ever booked".

Two further details are deliberate:

- **Milestones are cumulative, and their ordinals come from the canon.** An
  episode sitting in `RECORDED` *was* booked even though it is no longer in
  `BOOKED`, so a milestone counts every candidate at or past its ordinal. The
  ordinals are read from [`spec/states.json`](../spec/states.json) at call
  time rather than restated here, so adding a state to the canon cannot leave
  this projection counting a stale ladder. A branch state has no ordinal and is
  reported as `off_ladder` instead of being folded into the funnel.
- **Revenue is asked, not recomputed.** The money line comes from
  `sponsors.revenue_report` — the same call the Revenue view makes — so the
  portfolio and the per-show view cannot disagree about a number, and the
  committed-slot and claim-approval publication gates are the ones already
  reported per show. `totals.currency` is the currency every show agreed on, or
  `null` when they differ; the per-show rows stay authoritative in that case.

## Guest overlap

The `overlap` block is the guest overlap matrix an operator actually needs:
**which guest has a candidate record on more than one show.** Two shows chasing the same person is
a booking collision worth seeing before the invitation goes out.

Identity resolves through the same `COALESCE(guest_id, source_key, guest_name)`
ladder the suggestion and directive readers use, so a row predating the
guest-CRM migration still matches its later self. Each entry carries the guest
name, the show ids, and the labelled shows — "Chad Kroeger in Flagship + Field
Show" is one entry with `show_count: 2`.

## Drilldown

`network_dashboard.show_detail(conn, tenant_id=…, show_id=…, actor_role=…)`
returns one show's portfolio row plus the guests it shares with its siblings.
It answers "what am I about to open" from the tenant's active registry; a show
that is not registered active **in this tenant** is a `404` whatever the caller
passed.

In the cockpit, opening a row navigates to `?show=<show_id>`, which is the
exact mechanism the show switcher already uses — so a portfolio row opens the
same dashboard the operator would reach by hand, rather than a second, parallel
rendering of it.

## The health report, and its receipt

`export_health_report(conn, tenant_id=…, actor_role=…, actor_id=…, format=…)`
renders the portfolio as `html`, `json`, or `pdf`:

- **HTML** is a self-contained document with an inline stylesheet and **no
  external asset reference**. Every interpolated value — show labels and guest
  names included — passes through `html.escape`, so operator-supplied text
  cannot inject markup into a report that will be mailed around.
- **JSON** is the same projection, sorted and indented, for a consumer that
  wants the numbers rather than the page.
- **PDF** renders the HTML through WeasyPrint, which is the declared optional
  extra: `pip install -e '.[pdf]'`. Without it the export reports the missing
  extra (`503`) rather than returning a partial or broken file.

The report is the one artifact that **leaves** the cockpit, so every render
appends an attributable `network_report_receipts` row *before* the body is
returned:

| Column | Meaning |
|--------|---------|
| `event_type` | always `network.health_report_exported` |
| `report_format` | `html`, `json`, or `pdf` |
| `document_checksum` | SHA-256 of the exact bytes handed over |
| `show_count` / `overlap_count` / `revenue_minor` | what the report said at that moment |
| `actor_id` / `actor_role` | from the authenticated operator, never a payload |

The checksum is the point: "which report left the cockpit" stays answerable
without the file surviving anywhere. `list_report_receipts` returns the
newest-first trail, and the Network view renders it under the tables.

## HTTP surface

All four routes are tenant-scoped, network-operator-only, and answered with
`Cache-Control: no-store, private`.

| Method | Path | Returns |
|--------|------|---------|
| `GET` | `/v1/network/portfolio` | the portfolio projection |
| `GET` | `/v1/network/shows/{show_id}` | one show's drilldown |
| `GET` | `/v1/network/receipts` | the health-report receipt trail |
| `GET` | `/v1/network/report.{html\|json\|pdf}` | the health report as an attachment |

No path takes a tenant, and there is no network route that writes, books,
publishes, or sends.

## Storage

Migration `020_network_report_receipts` adds the one new table. It is additive:
the portfolio itself introduces no schema of its own, because it reads the
tables the per-show organs already keep.

## Dashboard

The **Network** view (`#network-view`) is present only for a network operator.
It renders the five portfolio counters, the per-show table with an
`Open dashboard` drilldown per row, the guest-overlap matrix, and the export
receipt trail. `Export health report` picks PDF, HTML, or JSON and downloads
the document, then refreshes the receipt list so the operator sees the receipt
their own export just wrote.
