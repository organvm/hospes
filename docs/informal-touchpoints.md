# Informal touchpoints

HOSPES records an informal touchpoint when an authenticated operator needs to
preserve a factual text, email, Instagram, in-person, phone, or hallway
conversation. A touchpoint is informational evidence only. It does not advance
an opportunity, satisfy a Pilot gate, send correspondence, book time, or create
consent.

## Private custody

The browser submits the private note over the authenticated operator surface.
HOSPES validates and encrypts it with AES-256-GCM before committing the receipt.
The authenticated additional data binds tenant, show, `touchpoint_receipts`,
touchpoint id, `notes`, private-relationship category, and tenant key version.
Only the resulting `private-field://` reference is stored on the receipt; the
master key remains in the credential wall.

Host, producer, editorial-owner, and relationship-owner sessions may record and
view touchpoints. Network-operator sessions cannot reveal private relationship
notes. API responses containing decrypted notes use `Cache-Control: no-store,
private` and `Pragma: no-cache`. Public schemas and audit details carry opaque
references and bounded metadata, never note plaintext.

## API contract

`POST /v1/shows/{show_id}/touchpoints` accepts exactly:

- `guest_id` or `opportunity_id` (the workbench supplies both);
- optional `partnership_id` for the Executive Overview audit timeline;
- `channel`, `notes`, and an aware `occurred_at` timestamp.

The route ignores no identity fields: submitting `initiator` or
`initiator_role` is rejected. Both values are derived from the authenticated
process-bound or Cloudflare Access-mapped operator. Exact retries are
idempotent; a retry with different note evidence fails closed.

`GET /v1/shows/{show_id}/touchpoints` supports bounded filters for guest,
opportunity, partnership, channel, initiator, date range, and limit. The
opportunity-specific timeline is also available at
`GET /v1/opportunities/{opportunity_id}/touchpoints`. Every query applies
tenant/show scope before decrypting any note.

## Operator workflow

In Pilot Workbench, **Log chat** opens a modal with guest, channel, date, and
private notes. After recording, the same immutable receipt appears in the guest
timeline and as an attributable event in Executive Overview. Values are HTML
escaped before rendering. The workflow deliberately has no edit, delete, send,
or state-transition control.
