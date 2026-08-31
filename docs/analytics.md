# Analytics: pluggable providers → dashboard metrics

HOSPES books guests; analytics tells the desk what happened after the episode
shipped. The organ is deliberately a **read organ**: it admits provider
observations, validates them, charts them, and exports them. It never sends,
publishes, or mutates a provider. Issue #29 is closed by
`tests/issue_predicates/test_issue_29.py`, which proves every contract on this
page against the live service, CLI, API, and dashboard surfaces.

## The record

Every stored observation validates against
[`spec/analytics.schema.json`](../spec/analytics.schema.json) before it is
persisted:

| Field | Meaning |
|-------|---------|
| `provider` | `spotify_creator_csv`, `youtube_analytics`, `megaphone_chartable_import`, or `custom_csv_http` |
| `episode_id` | the episode the observation belongs to |
| `metrics` | canonical names `downloads`, `streams`, `retention_pct`, `avg_consumption`, `top_clip_views`, `top_clip_ref`, plus any further provider column, carried through verbatim |
| `period_start` / `period_end` | the closed ISO-8601 window the observation covers |
| `fetched_at` | when HOSPES read it |
| `source_receipt_ref` | the provider receipt that authorized this read |

`source_receipt_ref` is required, not decorative: **no metric enters the store
without an attributable provider receipt.** A row is unique on
`(tenant_id, show_id, provider, episode_id, period_start, period_end)`, so
re-reading the same export is idempotent.

Provider column spellings are normalized onto the canonical names
(`retention_%` → `retention_pct`, `top_clips` → `top_clip_views`), but metric
*values* keep their provider type and spelling verbatim. Numeric coercion
happens only in the trend projection, so the stored record stays faithful to
the source while the dashboard still charts it.

## Three ways in, one shape out

1. **Operator import** — `hospes analytics import <csv> --provider … --receipt …`
   imports a provider export the operator already holds, under an explicit
   receipt reference.
2. **Bounded fetch** — `hospes fetch-analytics` verifies every configured
   analytics provider, **writes the provider receipts first**, and then imports
   each provider's declared export.
3. **Scheduled read** — `hospes analytics schedule` enqueues `analytics.fetch`
   as an `execution_kind=scheduled`, `operation=read` job. `hospes jobs run`
   leases and executes it.

```bash
python3 -m hospes fetch-analytics --tenant hospes --show flagship
python3 -m hospes analytics trends --show flagship --limit 12
python3 -m hospes analytics export --show flagship --out analytics.csv
python3 -m hospes analytics schedule --show flagship --actor operator
python3 -m hospes jobs run --worker operator_worker
```

## Read-only scheduling

`hospes/jobs.py` restricts scheduled work to the `analytics.` and `research.`
namespaces *and* to the `read` operation. `schedule_fetch` never accepts an
operation from its caller: it always enqueues `analytics.fetch` as
`scheduled`/`read`, and `jobs.enqueue` rejects any other execution kind or
operation in that namespace with `422`. A scheduled analytics job therefore
cannot acquire send or publish authority — the queue refuses to hold one.

The local worker's authority is the handler map it is given
(`local_job_handlers`), because `jobs.run_once` leases only job types that have
a registered handler. That map holds the analytics read and nothing else.

## Visible unconfigured and blocked state

A fetch records one receipt per configured provider before importing anything,
so each provider ends in an attributable state:

| Status | Meaning |
|--------|---------|
| `imported` | the provider was ready and its export was read |
| `unconfigured` | no `import_path` is declared for it in `config/analytics.yaml` |
| `unavailable` | the declared export is not present on disk |
| `blocked` | the provider is `live` and its credential wall is not provisioned |

A provider is never silently substituted with another provider's numbers, and a
blocked provider still leaves a receipt. `config/analytics.yaml` holds only
policy — a provider names a `credential_ref` by key; **no secret ever lives in
that file**, and no API response echoes a credential reference.

## Trends

`trends()` groups observations by episode, takes each episode's latest reported
period, and returns the last N episodes oldest-first so a chart reads left to
right. Across the providers that reported one episode, **counters are summed**
(`downloads`, `streams`, `top_clip_views`) and **ratios are averaged**
(`retention_pct`, `avg_consumption`). Non-numeric metric values are skipped by
the projection rather than coerced.

## API

Every route is tenant/show scoped, rejects a foreign show scope with `403`, and
answers with `Cache-Control: no-store, private`.

| Route | Purpose |
|-------|---------|
| `GET /v1/shows/{show_id}/analytics` | stored observations, newest period first |
| `GET /v1/shows/{show_id}/analytics/trends?limit=12` | chartable per-episode series |
| `GET /v1/shows/{show_id}/analytics/receipts` | the analytics provider receipts |
| `GET /v1/shows/{show_id}/analytics/export.csv` | reporting CSV |
| `POST /v1/shows/{show_id}/analytics/imports` | validated operator import |
| `POST /v1/shows/{show_id}/analytics/fetches` | bounded provider fetch |
| `POST /v1/shows/{show_id}/analytics/schedules` | enqueue the read-only pull |

## Dashboard

The cockpit's **Analytics** view charts downloads across the selected window,
lists Episode / Downloads / Retention / Top clip views / Period / Providers, and
shows the provider receipts behind the reads. `canViewAnalytics` gates the read
for host and owner roles; `canExportAnalytics` gates the CSV export and is false
for the completed specimen, which exposes no export surface.

## Export safety

Reporting CSV neutralizes spreadsheet-formula prefixes (`=`, `+`, `-`, `@`, tab,
carriage return) on every non-numeric cell by prefixing an apostrophe. Real
numbers — including negatives — are left untouched, so the export stays
arithmetically usable while a provider-supplied label cannot become a formula in
a spreadsheet.
