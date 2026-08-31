# HOSPES adapter contract

HOSPES (Latin: *guest* **and** *host*) is the podcast guest-operations product. Its blueprint
working name is **conversation-operations-system** — an alias mentioned once here and never used
again; the product is **HOSPES** everywhere.

You built the organs. HOSPES assembles the organism. The estate already contains most of the
horizontal machinery of a media company (relationship CRM, correspondence, capture, canonical
media, transcript intelligence, editorial production, clip assembly, content multiplication,
distribution, voice governance, approval/audit). What was missing is the podcast-specific domain
kernel that composes them. HOSPES is that kernel — and it stays **thin**.

## The contract

1. **Compose through adapters and events — never merge estate repos into HOSPES.** Each estate
   system keeps its own identity, repo, and provenance. HOSPES holds the composition (domain,
   state machine, permissions, events), not copies of the systems.
2. **One adapter file per system** — `adapters/<system>.adapter.yaml`. Each declares: `system`,
   `claimed_by` (which source(s) named it), `repo`, `verified` + `verified_how`, `disposition`,
   `capability`, `seam` (`consumes` / `emits` events + a one-line `interface`),
   `first_integration_step`, and `caveats`.
3. **Systems talk to HOSPES only through their `seam`** — a bounded event vocabulary in and out.
   No estate system reaches into HOSPES internals; HOSPES reaches into no estate internals beyond
   the declared interface.
4. **Dispositions** (from the audit's table; "archive/fold" means preserve + redirect, never
   delete):
   - `retain-service` — mature system keeps running; HOSPES calls a stable adapter.
   - `extract-package` — portable logic lifted into a HOSPES package with provenance back to source.
   - `adapt-spec` — schemas / rubrics / workflows become HOSPES domain configuration under `spec/`.
   - `preserve-rnd` — advanced R&D; referenced, never a launch dependency.
   - `fold-archive` — superseded/duplicate surface; unique vocabulary harvested, the rest redirected.
5. **HOSPES stays thin.** It owns the domain contracts, the appearance lifecycle, the permission
   model, the event bus, and the human-approval gates. It does not own capture, canonical media,
   the content engine, or distribution mechanics — those are adapters.
6. **Draft-only, never send.** Any adapter that touches outreach (mail, distribution) drafts and
   stages; **sending is a human-gated action by design.** No estate seam grants HOSPES autonomous
   send.
7. **$0 / keyless v0.** The v0 runtime is python3 stdlib + PyYAML only, no network, no paid APIs.
   Adapters that imply network or paid services (vox synth/Whisper, Postgres/Redis contact store,
   speech-score diagnostics) are declared but kept **out of the v0 loop** and marked in their
   `caveats`.

## The 18 systems

| Adapter | Repo | Disposition | Verified |
|---|---|---|---|
| application-pipeline | organvm/application-pipeline | extract-package | true |
| universal-mail--automation | organvm/universal-mail--automation | retain-service | true |
| mirror-mirror | organvm/mirror-mirror | extract-package | true |
| salon-archive | organvm/salon-archive | adapt-spec | true |
| media-ark | organvm/media-ark | retain-service | true |
| materia-collider | organvm/materia-collider | extract-package | true |
| content-engine--asset-amplifier | organvm/content-engine--asset-amplifier | retain-service | true |
| multi-camera--livestream--framework | organvm/multi-camera--livestream--framework | retain-service | true |
| social-automation | organvm/social-automation | retain-service | true |
| vox | organvm/vox | preserve-rnd | true |
| public-record-data-scrapper | organvm/public-record-data-scrapper | extract-package | true |
| community-hub | organvm/community-hub | adapt-spec | true |
| kerygma-pipeline | organvm/kerygma-pipeline | fold-archive | true |
| praxis-perpetua-object-lessons | organvm/praxis-perpetua (+ object-lessons) | adapt-spec | true |
| persona-voice-governance | organvm/vox--architectura-gubernatio (+ vox--publica, collective-persona-operations) | extract-package | true |
| speech-score-engine | organvm/speech-score-engine | preserve-rnd | true |
| aerarium | organvm/aerarium--res-publica | adapt-spec | **false** |
| a-i-council--coliseum | organvm/a-i-council--coliseum | fold-archive | true |

The full discrepancy accounting (the one `verified:false` row, naming mismatches, and
description-vs-code drift) is in [`../docs/estate-composition.md`](../docs/estate-composition.md) →
Reconciliation table.
