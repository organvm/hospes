# Public software acceptance

This ledger is derived from the content checks already shipped in this public
repository. It covers software artifacts and declared product requirements.
It does not reconstruct the historical private ask ledger or assert delivery of
private participant, correspondence, recording, custody, or consent outcomes.
The historical operations ledger remains in its private canonical owner.

The full public software predicate is `bash done.sh`. Presence checks below are
only one gate; tests, packaging, privacy, permissions and declared-predicate
coverage remain separate gates. Open hardening debt remains counted.

| Check | Public requirement | Artifact |
|---|---|---|
| AA1-care-profile-consent-type | care_profile_storage | `spec/consent.schema.json` |
| AA1-delight-section | Guest delight and care profile | `docs/guest-operations-os.md` |
| AA2-permission-matrix-blocked | request_guest_promotion | `spec/permission-matrix.yaml` |
| AA2-consent-promotion-const | promotion_required | `spec/consent.schema.json` |
| AA3-claim-not-approved-error | ClaimNotApprovedError | `hospes/drafts.py` |
| AA3-validate-claims | validate_claims | `hospes/drafts.py` |
| AA4-event-draft-edited | draft.edited | `spec/events.json` |
| AA4-event-classification-overridden | classification.overridden | `spec/events.json` |
| AA4-audit-record-draft-edited | record_draft_edited | `hospes/audit.py` |
| AA4-audit-record-classification-overridden | record_classification_overridden | `hospes/audit.py` |
| AA4-audit-human-correction-counts | human_correction_counts | `hospes/audit.py` |
| AA4-evaluation-doc-metrics-section | Human-correction metrics | `docs/evaluation.md` |
| ROADMAP-guest-portal | Guest portal UI | `docs/ROADMAP.md` |
| ROADMAP-calendar-connector | Google Calendar free/busy connector | `docs/ROADMAP.md` |
| ROADMAP-asset-amplifier | Asset Amplifier | `docs/ROADMAP.md` |
| ROADMAP-voice-constitution | Voice Constitution | `docs/ROADMAP.md` |
| ROADMAP-outreach-templates | outreach templates | `docs/ROADMAP.md` |
| ROADMAP-city-intelligence | City Intelligence | `docs/ROADMAP.md` |
| ROADMAP-sponsor-governance | Sponsor creative governance | `docs/ROADMAP.md` |
| ROADMAP-nurture-cadence | Relationship nurture cadence | `docs/ROADMAP.md` |
| ROADMAP-distribution-tooling | Owned-distribution launch tooling | `docs/ROADMAP.md` |
| PRIOR-ART-register | Podcast Livestream Narrative Framework | `docs/prior-art.md` |
| PRIOR-ART-field-show-precursor | road-trip travel podcast | `docs/prior-art.md` |
| PRIOR-ART-estate-boundary | podcast engine | `docs/prior-art.md` |
| ROADMAP-amp-lab-tenant | AMP LAB MEDIA | `docs/ROADMAP.md` |
