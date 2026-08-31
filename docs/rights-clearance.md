# Rights and clearance gate

A clearance is the record that one rights item on one episode is outstanding,
licensed, or refused. Music, clip, IP, footage, and guest-likeness items block
publishing silently in most production stacks; in HOSPES they block it loudly,
with a named reason and an attributable receipt behind every decision.

Owner: `hospes/clearances.py`. Schemas: `spec/clearance.schema.json` and
`spec/clearance_receipt.schema.json`. Domain contract:
`config/domain_kernel.yaml` → `clearance_contract`. Storage: migration 15,
`rights_clearance_custody`.

## Record shape

| Field | Meaning |
|---|---|
| `clearance_id` | Deterministic identity over tenant, show, episode, type, and both private-value checksums. Re-recording the same item returns the existing record. |
| `type` | `music`, `clip`, `IP`, `footage`, `guest_likeness`. |
| `status` | `pending`, `cleared`, `denied`. |
| `blocks_publication` | True for every status other than `cleared`. |
| `custody_mode` | `sealed` or `external_reference` — see below. |
| `rights_holder_ref`, `license_terms_ref` | Opaque references. Never the private value. |
| `rights_holder_checksum`, `license_terms_checksum` | SHA-256 over the private value, used for identity and tamper detection on reveal. |
| `evidence_ref` | Opaque custody reference for the most recent decision. |
| `recorded_by`, `decided_by` | Operator identity, taken only from the authenticated session. |

## Custody: sealed, or explicitly external

Rights holder and licence terms are private counterparty data, so they are never
stored in the clear.

* **`sealed`** — a tenant field vault is configured. Both values are sealed with
  AES-256-GCM under the tenant's data key, bound to
  `tenant/show/clearances/<clearance_id>/<field>` as additional authenticated
  data. The row keeps a `private-field://…` reference; plaintext exists only in
  an authorized `Cache-Control: no-store` response, and only for the roles in
  `AUTHORIZED_CLEARANCE_ROLES`.
* **`external_reference`** — no field vault is configured. The caller must
  supply an opaque custody reference (`rights://owner/bmi-licence`) that names an
  owner outside HOSPES. A private literal is refused with `503` rather than
  stored in the clear, and the record's `custody_mode` says which mode produced
  it. This is the "visible `unconfigured` state, never a silent substitution"
  rule from `AGENTS.md`.

Reveal recomputes the checksum. A mismatch is a `409`, not a silently wrong
value.

## Decisions and receipts

`POST /v1/shows/{show}/clearances/{clearance_id}/decision` takes a target
`status` and a required `evidence_ref`. There is no status change without
evidence.

Every decision appends one immutable row to `clearance_receipts`:

| Event | Meaning |
|---|---|
| `clearance.recorded` | The item was created; `from_status` is null and `to_status` is `pending`. |
| `clearance.cleared` | Licensed against `evidence_ref`. |
| `clearance.denied` | Refused against `evidence_ref`. |
| `clearance.reopened` | Returned to `pending` against `evidence_ref`. |

Receipts are append-only and carry `correlation_id` values of the form
`clearance://<clearance_id>/<ordinal>`, unique per tenant/show. Re-submitting
the same status with the same evidence is idempotent; the same status with
*different* evidence is a `409`, because that would overwrite custody.

Only `producer`, `editorial_owner`, and `relationship_owner` may record or
decide. `host` and `network_operator` may read.

## Publication denial

`clearances.publication_gate` is the predicate. It denies for **every** status
other than `cleared` — pending and denied alike — and names each blocking item:

```json
{
  "episode_id": "episode-12",
  "publishable": false,
  "pending": 2,
  "denied": 0,
  "blocking": [{"clearance_id": "…", "type": "music", "status": "pending", "due_date": "2026-09-01"}],
  "reason": "2 rights item(s) are not cleared: 2 pending, 0 denied"
}
```

`hospes/distribution.py` calls it three times, so there is no path around it:

1. `create_draft` — a blocked episode's distribution is created with status
   `blocked` rather than `draft`.
2. `authorize_publish` — refuses with `409` and the gate's reason.
3. `mark_published` — refuses with `409` even for an already-authorized job,
   because a clearance can be reopened after authorization.

## Badges

`GET /v1/shows/{show}/clearance-badges` projects one row per episode with
`pending`, `cleared`, `denied`, `total`, `blocking`, `publishable`,
`next_due_date`, and a rendered `badge` string:

* `⚠ 2 clearances pending`
* `⚠ 1 clearance denied`
* `⚠ 2 pending · 1 denied`
* `✓ 3 clearances cleared`
* `No clearances recorded`

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/shows/{show}/clearances` | List with `episode_id`, `status`, `clearance_type`, `due_before`, `blocking_only`, `limit`. |
| `POST` | `/v1/shows/{show}/clearances` | Record a new pending item. |
| `POST` | `/v1/shows/{show}/clearances/{id}/decision` | Move status against evidence. |
| `GET` | `/v1/shows/{show}/clearances/{id}/receipts` | The immutable decision timeline. |
| `GET` | `/v1/shows/{show}/clearance-badges` | Per-episode badge counters. |
| `GET` | `/v1/shows/{show}/clearance-report` | CSV report, `text/csv`. |

Every read that can carry a revealed private value responds
`Cache-Control: no-store, private`. Every route is bound to the operator's show
scope; a request aimed at another show is a `403`, never a filtered empty list.

## CSV report

The report carries opaque references only — never decrypted rights holder or
licence text — and neutralizes spreadsheet-formula prefixes (`=`, `+`, `-`, `@`)
by quoting the cell, the same rule the relationship-map export uses. Columns are
fixed and declared once as `clearances.CSV_COLUMNS`.

## Dashboard

Pilot Workbench carries the **Clearance** tab: the per-episode badge strip, a
status filter (all / pending / cleared / denied), the item list with an inline
status-plus-evidence decision form, an add form, and the CSV export rendered
into a read-only textarea. Visibility follows `canViewClearances` and
`canManageClearances` in `dashboard/assets/capabilities.mjs`; both are
documented in the guided-tour glossary.
