# Team notifications and assignments

HOSPES coordinates a production team the same way it coordinates a guest: through
records, not through someone remembering. A lifecycle transition that creates work
for another role writes two things — a **notification** addressed to that role, and
a **queue assignment** that role can close. Both are tenant/show scoped, both are
idempotent, and neither can send anything.

The machine-readable owner is `notification_contract` in
[`config/domain_kernel.yaml`](../config/domain_kernel.yaml). The implementation is
`hospes/notifications.py`; `hospes/platform.py` keeps its three historical entry
points as delegations so there is exactly one implementation.

## What a transition produces

| Trigger | Recipient role | Notification type | Queue task | Severity |
|---|---|---|---|---|
| `appearance.approved` | producer | `draft_ready` | `draft_review` | normal |
| `brief.ready` | host | `brief_ready` | `brief_review` | normal |
| `booking.confirmed` | producer | `booking_confirmed` | `schedule_prep` | normal |
| `recording.completed` (guest pilot) | editor | `clips_needed` | `clip_production` | critical |

`booking.confirmed` carries the confirmed recording time through as the task's due
date. A technical rehearsal never emits `clips_needed`, because it is never a
completed guest pilot.

`editor` is a team role, not a lifecycle authority. It has always been declared in
`config/shows/*.yaml`; it now exists as `service.HumanRole.EDITOR` so an editor can
hold a session, read its own bell, and close its own clip work. It gains no approve,
draft, receipt, or Pilot permission.

## Idempotence and scope

Emission joins the caller's transaction, so a transition that is later rejected
leaves no bell item behind. Each notification carries a `dedupe_key` of
`trigger:entity_ref:recipient_role` under a partial unique index, and each
assignment is unique per `(tenant, show, assignment_type, entity_ref, assignee_role)`.
Re-approving a candidate or re-recording a receipt therefore returns the existing
rows rather than creating a second one.

A show that is not an **active row in the show registry** emits neither record.
Team work is a registered-show capability; the `assignments` foreign key says so, and
`notifications.emit_transition` returns `None` rather than half-writing.

## Private-custody boundary

A notification holds a bounded title, an opaque `entity_ref`, and an opaque
`body_ref` — the entity that already owns the detail. Titles are validated against
the same privacy admission checks the rest of HOSPES uses, so contact-,
correspondence-, and agreement-like text is rejected. A guest name that *looks*
private degrades to a neutral subject instead of rejecting the transition: a display
string must never be able to block a lifecycle move.

Read control is per role. Only the addressed role may mark one of its notifications
read — another role gets `403`, an unknown id gets `404`, and a second read is a
no-op that keeps the original `read_at`. `read-all` clears the acting role alone.
Only the assigned role — or a network operator — may complete an assignment (`403`
otherwise), and completion records the closing actor. Reading or queueing another
show under a show-bound identity is `403`, as everywhere else in the estate.

## API contract

Every route is tenant/show scoped and derives the acting role from the authenticated
session; browser-supplied role headers are ignored as everywhere else.

```
GET  /v1/shows/{show_id}/notifications?unread_only=&notification_type=&severity=&limit=
GET  /v1/shows/{show_id}/notifications/summary
POST /v1/shows/{show_id}/notifications/read-all
POST /v1/shows/{show_id}/notifications/{notification_id}/read
POST /v1/shows/{show_id}/notifications/{notification_id}/previews
GET  /v1/shows/{show_id}/notification-previews?notification_id=&limit=
POST /v1/shows/{show_id}/notification-previews/{preview_id}/authorize
POST /v1/shows/{show_id}/notification-previews/{preview_id}/receipts
GET  /v1/shows/{show_id}/my-queue?include_done=&limit=
GET  /v1/shows/{show_id}/queue-tasks?assignee_role=&status=&include_done=&limit=
POST /v1/shows/{show_id}/queue-tasks
POST /v1/shows/{show_id}/queue-tasks/{task_id}/complete
```

The queue resource is `queue-tasks`, not `assignments`, and the delivery resource is
`notification-previews`, not `notification-deliveries`. `tests/test_api.py` asserts
that no route path contains `send`, `deliver`, `book`, `sign`, `publish`, or
`distribute` — a blunt guard worth keeping blunt, so the routes are named around it.

## Draft-only email and webhook delivery

`POST .../notifications/{id}/previews` renders the exact payload a human would
deliver — an escaped HTML block for `email`, a canonical JSON object for `webhook` —
banners it `NOT SENT`, stores its SHA-256 checksum, and returns it under
`Cache-Control: no-store, private`. Preview is limited to **critical** notifications,
the target is an opaque route reference (never a raw address), and an exact retry
under the same `idempotency_key` returns the same preview; a retry that changes the
notification, channel, or rendered bytes fails closed with `409`.

Authorization fails closed twice over. It is refused while the runtime feature
default `notifications_are_draft_only` is true (it is true in the shipped
`config/runtime.yaml`), and refused unless the show's outbound mode is
`manual_receipt` or `provider_connected`. When both permit it, the same
`providers.require_human_authorization` receipt distribution uses is bound to that
preview under the action `notification.delivery`.

Only then may a human — who performed the send externally — record the delivery
receipt. HOSPES verifies the authorization receipt still binds this exact preview,
writes an immutable `provider_receipts` row carrying the preview checksum, and
refuses any later attempt to change the external reference.

## Operator workflow

The cockpit header carries an **Alerts** bell whose badge counts unread
notifications for the signed-in role and turns critical when a critical one is
unread. Opening it reveals the notification centre: filter by type, show unread
only, mark one read, or mark all read. The **My Queue** tab lists that role's tasks
ordered by due date with null due dates last, marks overdue open work, and offers a
single **Mark done** control. Both surfaces escape every value before rendering, and
neither has an edit, delete, send, or state-transition control.
