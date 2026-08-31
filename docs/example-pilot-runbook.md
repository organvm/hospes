# Example Private Pilot

This is the internal working label until three private pilots prove the format.
It is not a public title or release authorization.

## Fixed ownership

- Producer is showrunner, executive producer, guest/editorial lead, and HOSPES operator.
- Host is co-host, relationship owner, infrastructure provider, and weekly 12-minute decision-maker.
- The existing studio producer owns scheduling, guest intake, releases, room, crew, capture,
  hospitality, and master-media handoff.
- HOSPES researches, drafts, tracks, and prepares. It never sends correspondence or performs an
  external booking, consent, recording, media-ingest, or publication action.

Guest names, routes, correspondence, studio addresses, releases, private notes, recording files,
and scorecards remain outside Git. HOSPES records only bounded operational metadata and opaque
owner references such as `calendar://...`, `release://...`, or `media://...`.

## Partnership review surface

Before Host's first review, Producer imports the safe tracked partnership template:

```bash
export HOSPES_DB="$HOME/Library/Application Support/HOSPES/private_pilot/hospes.sqlite3"
python3 -m hospes import-partnership config/partnerships/example-private-pilot.yaml \
  --db "$HOSPES_DB" --tenant private_pilot --actor example_operator --role producer
```

Start the operator with a session-only token and the same explicit database:

```bash
IFS= read -r -s -p "Operator token: " HOSPES_OPERATOR_TOKEN; printf '\n'
export HOSPES_OPERATOR_TOKEN
python3 -m hospes operator --db "$HOSPES_DB" \
  --actor ari_owner --role relationship_owner --tenant private_pilot
unset HOSPES_OPERATOR_TOKEN
```

The operator's **Partnership Cockpit** is the shared walkthrough: the executive 12-minute agenda,
the Pilot Workbench, and the Complete Register of what the engine can do, pilot and launch plans,
fixed roles, working agreements, deal questions, obligations, decisions, custody receipts, risks,
and anything explicitly unknown. Use the capture panel during a review so a forgotten item becomes
tenant-scoped operating truth and an audit event.

The command center is intentionally reusable. A different partnership uses the same schema and UI
with a different tenant and template. It does not store contract bodies, signatures, correspondence,
private deal terms, banking data, or financial details; agreement and deal cards must point to an
opaque canonical-owner reference.

## Pilot 1 predicate — August 5, 2026

Issue [#9](https://github.com/organvm/hospes/issues/9) is the owner receipt. It remains open until a
real guest pilot is recorded and every external receipt below is present. If the guest becomes
unavailable, complete the technical/format rehearsal by the deadline and record it explicitly as
`technical_rehearsal`; do not close Pilot 1.

### Candidate and decision packet

Producer imports three low-social-cost Los Angeles candidates from Host's prior professional network:
one primary and two backups. Use C2 or C3 only for this pilot; never use C4/C5. The private CSV is
ingested with `python3 -m hospes import-candidates` and is never copied into the repository.

Host's 12-minute review records approve/reject/protect decisions, validates the thesis and verified
route, and confirms one recurring recording window. Decision notes in HOSPES are bounded operating
notes, never private correspondence or relationship narrative.

### Invitation boundary

HOSPES generates the invitation from the tracked template and voice configuration. Producer or the
named producer reviews and sends it manually in the external correspondence owner. HOSPES accepts
only an `outreach.sent` receipt with an opaque external reference; there is no send route.

### Hosts-and-producer rehearsal

Before final guest setup, complete and play back:

- environmental master and isolated host singles;
- concealed isolated lavaliers and an out-of-frame safety microphone;
- backup recorder, room tone, and slate;
- optional artifact camera;
- a twenty-minute Claim → Stress Test → Artifact rehearsal;
- verified master-media handoff to the canonical owner.

### Recording readiness

`recording.ready` is accepted only after HOSPES holds:

- `booking.confirmed` with opaque studio and producer references plus recording time;
- `consent.signed` with explicit private-pilot and clip scope;
- a verified episode brief and segment plan;
- a declared preflight asset package;
- an opaque technical-preflight reference.

The existing producer owns booking, consent, preflight, room/crew, and hospitality in existing
tools. HOSPES observes receipts; it does not replace those tools.

### After capture

Record `recording.completed` with `session_kind=guest_pilot`. Within 24 hours, verify masters and
checksums and hand them to the canonical media owner, then record `media.ingested` with only master
and checksum references. Within 48 hours, complete the scorecard in `docs/evaluation.md` and attach
its opaque reference to issue #9. Nothing is published.

## Pilot-to-launch runway

- Complete Pilot 2 (Producer network, New York City) and Pilot 3 (weak tie, Austin) by September 2,
  2026.
- After three pilots, freeze the format, public title, voice, and production profile.
- Produce the sizzle, show brief, guest brief, host bios, booking page, and three consented clips.
- Book five to eight public-run guests and bank six strong recordings before releasing a trailer
  and three episodes, then publish weekly.
- Only after flagship proof, begin the Phase 2 integrations listed in `docs/ROADMAP.md`.
