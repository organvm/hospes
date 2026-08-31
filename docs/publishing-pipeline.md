# Publishing pipeline

A distribution job is one publication package for one episode on one platform.
HOSPES prepares the package, gates it, and keeps the evidence; a human publishes
elsewhere and records the receipt. There is no route that publishes, delivers,
or uploads anything, and the dashboard footer says so.

Owner: `hospes/distribution.py`. Schemas: `spec/distribution.schema.json` and
`spec/delivery_receipt.schema.json`. Domain contract:
`config/domain_kernel.yaml` → `distribution_contract`. Storage: migration 21,
`distribution_publishing_pipeline`, on top of the `distributions` and
`delivery_receipts` tables from migration 9.

## Three package families

The platform selects the family; a field outside that family is refused at the
write boundary rather than stored as an opaque blob.

| Platform | Family | Required | Also accepted |
|---|---|---|---|
| `rss` | `rss` | `title` | `description`, `audio_url`, `duration`, `chapters`, `guid` |
| `youtube` | `video` | `title` | `description`, `tags`, `chapters`, `playlist`, `thumbnail_ref` |
| `tiktok`, `reels`, `shorts`, `clip_queue` | `clip` | `start_seconds`, `end_seconds`, `target_platform`, `caption_ref` | `hashtags` |

Only what makes an artifact identifiable is required. `package_completeness`
reports the rest — `missing: ["description", "audio_url"]` — because an operator
drafts a package before the audio is cut, and refusing that draft pushes the
half-finished state back into a spreadsheet. What actually blocks a publication
is the rights gate, the sponsor gate, and the human authorization receipt.

Bounded everywhere: titles ≤ 160 characters, descriptions ≤ 5 000, ≤ 100
chapters with distinct ascending starts, ≤ 30 tags inside YouTube's 500-character
budget, clips ≤ 600 seconds. `audio_url` and `thumbnail_ref` are a public
`https` URL **or** an opaque custody reference; a contact-like value is refused.
`caption_ref` and every failure reference stay opaque custody references.

## Previews

`distribution.preview(platform, metadata)` renders the exact artifact a platform
receives and reads and writes nothing:

* `rss` → a parseable `<item>` with `itunes:duration`, an optional
  `psc:chapters` block, and every value HTML-escaped.
* `video` → the metadata document an operator pastes into the studio, with
  chapters as `HH:MM:SS` timestamps.
* `clip` → the queue card: target, window, duration, caption reference, hashtags.

## Lifecycle

```
draft ─┬─ authorize ─→ authorized ─┬─ schedule ─→ scheduled ─┐
blocked┘                           │                          ├─→ published
                                   ├──────────────────────────┤
                                   └─ record failure → failed ─┘ (retryable)
```

* **draft / blocked** — a job is created `blocked` when `platform.publish_blockers`
  already denies the episode (an uncleared rights item, an unfilled committed
  sponsor slot, an unapproved sponsor claim). Re-drafting under the same
  idempotency key returns the existing job, never a second one.
* **authorized** — `providers.require_human_authorization` records a named human
  authorizing this exact subject. Both gates must be green at that moment, and
  the show's `outbound_mode` must be `manual_receipt` or `provider_connected`;
  `draft_only` is a `403`.
* **scheduled** — scheduling deliberately requires the authorization receipt
  first. A scheduled outbound action with no human behind it is exactly the
  automation `AGENTS.md` forbids: a future timestamp is the same gate, later.
* **published** — the manual receipt. Both gates re-run (a clearance can be
  reopened after authorization), the external identifier is stored, and it is
  immutable: the same reference is idempotent, a different one is a `409`.
* **failed** — a delivery attempt that did not succeed keeps its own evidence
  row and stays retryable under the same authorization receipt.

## Immutable delivery evidence

Every attempt, succeeded or failed, appends one `delivery_receipts` row at
attempt *n+1*. The attempt ordinal is derived from the evidence table itself,
not from a counter, so a retry can never overwrite the record of the first try.

| Column | Meaning |
|---|---|
| `attempt` | Ordinal, unique per tenant, show, distribution, and provider. |
| `result` | `delivered` or `failed`. |
| `delivery_mode` | `manual_receipt` or `provider_connected`. |
| `provider` | The adapter that produced the evidence. |
| `external_reference` | The platform identifier for a delivery, the failure evidence for a failure. |
| `payload_checksum` | SHA-256 over the package that was authorized. |
| `authorization_receipt_id` | Foreign-keyed to the authorization for *this* subject and the `publish` action. |

A second, attributable trail lives in `distribution_receipts` — the sibling of
`clearance_receipts` and `sponsorship_receipts` — with one row per transition
(`distribution.drafted`, `.scheduled`, `.authorized`, `.published`, `.failed`,
`.clip_queued`), its `from_status`, its `to_status`, and the operator behind it.
A call that supplies no operator identity is recorded as the `system` actor
rather than borrowing somebody's name; no HTTP route takes that path.

## Adapters, and what "unconfigured" means

`delivery_adapter(platform)` resolves the distribution capability through the
provider registry with a per-platform preference and
`manual_external_receipt` as the last resort. It reports the adapter that was
actually chosen, its mode, and the reason any blocked candidate was skipped —
never a silent substitution. Today `youtube_metadata` is configured `live` with
a `credential_ref` and no authenticated smoke, so it verifies `blocked` and the
YouTube platform falls back to the manual adapter, visibly.

`delivery_mode` is therefore `manual_receipt` for every platform right now. That
is the honest state, not a placeholder.

## Webhook-ready, and visibly not yet armed

`webhook_contract(show)` declares what an auto-publish webhook would carry — the
events `distribution.scheduled`, `distribution.published`,
`distribution.failed`, the payload schema `spec/distribution.schema.json`, and
`requires_authorization_receipt: true` — and returns `status: "unconfigured"`
with a reason until a show opts into `provider_connected` outbound mode *and* a
live adapter verifies. Readiness is a visible state, never a silent default.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/shows/{show}/distributions` | List with `episode_id`, `platform_name`, `status`, `limit`. |
| `POST` | `/v1/shows/{show}/distributions` | Draft a validated package. |
| `POST` | `/v1/shows/{show}/distributions/preview` | Render a package; reads and writes nothing. |
| `POST` | `/v1/shows/{show}/distributions/{id}/authorize` | Bind the human authorization receipt. |
| `POST` | `/v1/shows/{show}/distributions/{id}/schedule` | Schedule an authorized publication. |
| `POST` | `/v1/shows/{show}/distributions/{id}/publication-receipt` | Record a publication and its external identifier. |
| `POST` | `/v1/shows/{show}/distributions/{id}/failures` | Record one failed delivery attempt. |
| `GET` | `/v1/shows/{show}/distributions/{id}/receipts` | Both evidence trails for one job. |
| `POST` | `/v1/shows/{show}/clips` | Queue one short-form clip. |
| `GET` | `/v1/shows/{show}/distribution-board` | The Publish view projection. |
| `POST` | `/v1/shows/{show}/distribution-adapters/verification` | Write one provider receipt per adapter state. |

Every route is bound to the operator's show scope — a request aimed at another
show is a `403`, never a filtered empty list — and every response carries
`Cache-Control: no-store, private`. No path carries an action verb HOSPES does
not perform: the board is a *board*, the receipt is a *receipt*, and
`tests/test_api.py` asserts that over the whole OpenAPI path set.

Roles: `host` and `network_operator` may read. A `producer`, `editorial_owner`,
or `relationship_owner` may draft, queue clips, schedule, record a failure, and
verify adapters. Only an `editorial_owner` or `relationship_owner` may authorize
a publication or record one — the most irreversible action in the product gets
the narrowest gate, the same shape as sponsor-claim approval.

## Dashboard

The cockpit's fifth view, **Publish**: per-episode sections with one card per
platform, the publication gate, the adapter states, and a package preview
rendered into a read-only textarea. Cards carry only the actions legal for their
current state and role — preview, authorize, schedule, mark published, record a
failed attempt — and a failed card keeps its attempt count rather than
pretending the first try never happened. Three capture forms generate the RSS
item, the video metadata, and the clip queue entry. Visibility follows
`canViewPublishing`, `canManagePublishing`, and `canAuthorizePublication` in
`dashboard/assets/capabilities.mjs`; all three are documented in the guided-tour
glossary.
