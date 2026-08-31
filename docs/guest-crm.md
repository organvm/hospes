# Guest CRM — cross-season memory and do not contact

A show does not meet a guest once. It meets them in season one, again two years
later, and a third time when a producer who was not there the first time reads a
name off a spreadsheet. Without memory the third ask is indistinguishable from
the first — which is exactly how a booking desk re-invites someone who already
said no, and looks unprofessional doing it.

HOSPES keeps that memory as data, enforces it at every outbound edge, and shows
it on the card where the decision actually gets made.

## The identity

Cross-season memory keys on one **opaque guest identity**, scoped to a tenant
and a show: `(tenant_id, show_id, guest_id)`. It is not a name. An import row may
declare `guest_id` explicitly; when it does not, the identity is the opaque
`source_key` the external candidate system already owns, which is the same value
informal touchpoints key on. Every opportunity carries `guest_id`, so two
seasons that name the same identity share one memory even though each season is
a separate `AppearanceOpportunity`.

Migration 15 adds `guest_id`, `season`, and `episode` to
`appearance_opportunities` and backfills `guest_id` from `source_key`, so no
existing candidate loses its identity.

## The memory

`guest_history` holds one row per recorded interaction:

| column | meaning |
|--------|---------|
| `guest_id` | the opaque cross-season identity |
| `season` / `episode` | short labels, e.g. `S2` and `E3` |
| `disposition` | one of the bounded outcomes below |
| `date` | the calendar date of the interaction (never in the future) |
| `notes_ref` | encrypted `private-field://` reference, or an opaque owner reference |
| `opportunity_id` | the season's opportunity, when HOSPES ran that season |
| `recorded_by` / `recorded_by_role` | derived from the authenticated operator, never from the payload |

Dispositions: `APPROVED`, `DECLINED`, `SOFT_DECLINE`, `HARD_DECLINE`,
`NO_RESPONSE`, `REVISIT_LATER`, `PROTECTED`, `DO_NOT_CONTACT`, `RECORDED`,
`PUBLISHED`, `CANCELLED`.

Entries arrive two ways:

* **The engine writes them.** A terminal decision — approve, reject, protect —
  on an opportunity that declares its `season` and `episode` appends exactly one
  entry attributed to the deciding operator. That is the loop: season one's
  decision is season three's badge.
* **An operator records them.** `POST /v1/shows/{show_id}/guest-history` records
  a season HOSPES never observed, which is how a show that existed before the
  product gets its history in.

Every entry is **idempotent**: its id is derived from tenant, show, guest,
season, episode, disposition, and date, so the same interaction recorded twice
is one row. The same identity with *different* evidence is a `409`, never a
silent overwrite. A history entry is **informational only** — it advances no
opportunity state and moves no Pilot gate.

Note plaintext is optional. When supplied it is sealed with the tenant field
vault under full tenant/show/record AAD and revealed only to an authorized
operator in the same scope, exactly like an informal touchpoint. Season,
episode, disposition, and date are operational metadata and carry no custody
requirement, which is why the badge renders without a key.

## The badge

The approval card shows the last real season interaction:

```
Previously: S2E3 — SOFT_DECLINE (2024-03-15)
```

Directive-derived entries stay in the timeline but never become the badge — the
card should report the last time this show and this guest actually dealt with
each other, not the moment an operator flipped a switch.

## The directive

`guest_directives` holds the live answer to one question: may this show contact
this guest at all? One row per `(tenant_id, show_id, guest_id)`, with
`do_not_contact` as a boolean an operator can both set and lift.

**Only a relationship owner may write it.** Recording history is open to every
operator role; deciding that a human may never be approached again is not. The
toggle lives on the approval card and is rendered only for that role; the API
returns `403` for any other.

When no directive row exists the reader falls back to the append-only
`guest_history.do_not_contact` marker rather than reporting "contact permitted",
so a block recorded before the directive existed is never silently lifted.
Writing a directive appends its own history entry, so the timeline records who
decided and when.

`do_not_contact` is not the same as `PROTECTED`. A protected relationship is
owner-managed — HOSPES simply refuses to draft for it. A do-not-contact guest
asked never to be approached, and the refusal reaches back to intake.

## Enforcement — both ends of the loop

| edge | behavior |
|------|----------|
| candidate import | a row for a blocked guest is rejected and **no row in the batch is written** |
| draft preview | `403` |
| correspondence draft | `403` |
| `outreach.sent` receipt | `403` |
| guest suggestions | the guest is withheld |
| relationship nurture cadence | never due |

Import rejection has one deliberate exception: a row that itself declares
`do_not_contact: true` is agreeing with the directive, not re-asking, so it stays
idempotent. Declaring `do_not_contact` in an import is itself a relationship-owner
act — any other role's row is rejected.

## Surfaces

```
GET  /v1/opportunities/{opportunity_id}/guest-history
PUT  /v1/opportunities/{opportunity_id}/do-not-contact
GET  /v1/shows/{show_id}/guest-history
POST /v1/shows/{show_id}/guest-history
```

Every response carries `Cache-Control: no-store`. The approval queue and the
opportunity detail both project `guest_history`, `guest_history_badge`,
`guest_directive`, and `do_not_contact` so the card needs no extra round trip.

In the dashboard: the **approval card** carries the memory badge and the
relationship owner's do-not-contact toggle; the **Complete Register** carries the
Guest history panel — every interaction per guest, newest first, with the
recording operator and role — plus the form that records a prior season.

## Predicate

```bash
python -m pytest tests/issue_predicates/test_issue_24.py -q
```
