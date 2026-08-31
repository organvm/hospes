# HOSPES 1.0 completion register

The 1.0 program is additive: all compatible modes ship behind configuration. The issue mapping
below is authoritative for the current completion pass. A row may be locally implemented while
its provider, deployment, or human-pilot receipt remains externally gated.

## Issue-to-predicate map (#9 and #19–34)

The canonical machine-readable owner is
[`hospes/resources/spec/completion-registry.yaml`](../hospes/resources/spec/completion-registry.yaml).
The table below is generated and checked from that registry.

<!-- hospes-completion-registry:start -->
| Issue | Live title | Dependencies | Predicate | Receipt owner |
|---|---|---|---|---|
| #9 | Example Private Pilot: record Pilot 1 at earliest confirmed production window | substrate.storage_migration, substrate.encryption_artifacts, substrate.authentication_jobs, issue:19, issue:20, issue:21, issue:23, issue:24, issue:26, issue:27, issue:34 | `python -m pytest tests/issue_predicates/test_issue_09.py -q` | `github://organvm/hospes/issues/9` |
| #19 | [P1-1] Guest Suggestion: hospes suggest-guests → auto-suggest C2 alumni from Unlicensed Therapy archive | substrate.storage_migration, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_19.py -q` | `github://organvm/hospes/issues/19` |
| #20 | [P1-2] Contact Roster per Guest: publicist, manager, agent, direct in candidate schema | substrate.storage_migration, substrate.encryption_artifacts | `python -m pytest tests/issue_predicates/test_issue_20.py -q` | `github://organvm/hospes/issues/20` |
| #21 | [P1-3] Informal Touchpoint Logging: Log a text/DM/hallway chat → lightweight receipt | substrate.storage_migration, substrate.encryption_artifacts, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_21.py -q` | `github://organvm/hospes/issues/21` |
| #22 | [P1-4] Relationship Graph: hospes network-map --guest 'Theo Von' → second-degree paths | substrate.storage_migration | `python -m pytest tests/issue_predicates/test_issue_22.py -q` | `github://organvm/hospes/issues/22` |
| #23 | [P2-1] Multi-Show / Multi-Tenant Dashboard: show switcher in header | substrate.storage_migration, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_23.py -q` | `github://organvm/hospes/issues/23` |
| #24 | [P2-2] Guest CRM — Cross-Season Memory: Previously asked, declined, do-not-contact | substrate.storage_migration, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_24.py -q` | `github://organvm/hospes/issues/24` |
| #25 | [P2-3] Sponsor / Ad Inventory Tracker: sold/available slots, revenue dashboard | substrate.storage_migration, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_25.py -q` | `github://organvm/hospes/issues/25` |
| #26 | [P2-4] Rights / Clearance Gate: music, clip, IP checklist per episode | substrate.storage_migration, substrate.encryption_artifacts, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_26.py -q` | `github://organvm/hospes/issues/26` |
| #27 | [P2-5] Team Notifications & Assignments: Producer draft due, Host brief review, Editor clips needed | substrate.storage_migration, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_27.py -q` | `github://organvm/hospes/issues/27` |
| #28 | [P3-1] Publishing Pipeline: RSS + YouTube + TikTok/Reels clip queue | substrate.storage_migration, substrate.encryption_artifacts, substrate.authentication_jobs, issue:25, issue:26 | `python -m pytest tests/issue_predicates/test_issue_28.py -q` | `github://organvm/hospes/issues/28` |
| #29 | [P3-2] Analytics Hook: Pluggable providers (Spotify, YouTube, Chartable) → dashboard metrics | substrate.storage_migration, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_29.py -q` | `github://organvm/hospes/issues/29` |
| #30 | [P3-3] White-Label / Brand Config: config/brand.yaml → logo, colors, custom domain | substrate.authentication_jobs, issue:23 | `python -m pytest tests/issue_predicates/test_issue_30.py -q` | `github://organvm/hospes/issues/30` |
| #31 | [P3-4] Non-Technical Onboarding Wizard: hospes init → interactive setup (8 questions → working repo) | substrate.storage_migration | `python -m pytest tests/issue_predicates/test_issue_31.py -q` | `github://organvm/hospes/issues/31` |
| #32 | [P4-1] Network-Level Dashboard: portfolio view for network ops | substrate.storage_migration, substrate.authentication_jobs, issue:23, issue:25, issue:29 | `python -m pytest tests/issue_predicates/test_issue_32.py -q` | `github://organvm/hospes/issues/32` |
| #33 | [P4-2] Guest Portal (Self-Serve): magic link → intake, consent, date picking | substrate.storage_migration, substrate.encryption_artifacts, substrate.authentication_jobs, issue:23, issue:30 | `python -m pytest tests/issue_predicates/test_issue_33.py -q` | `github://organvm/hospes/issues/33` |
| #34 | [P4-3] AI Research Assistant (Bounded): click 'Research' → verified claims, counterarguments, receipts | substrate.storage_migration, substrate.encryption_artifacts, substrate.authentication_jobs | `python -m pytest tests/issue_predicates/test_issue_34.py -q` | `github://organvm/hospes/issues/34` |
<!-- hospes-completion-registry:end -->

Pilot #9 is tracked separately from these software predicates because its acceptance requires the
real private receipt chain in `docs/receipts/pilot-9-readiness-20260810.md`.

---

## Historical Phase 2 register

This register is the repo recording its own remaining work. Each row is a
deliberate deferral from the atomic audit of 2026-07-14, not an oversight.
The phase-1 atoms (booking pipeline, outreach engine, triage, approvals, brief
generation, voice constitution, studio routing, consent, evaluation) are
executable today. The items below are architecturally designed but not yet
wired to external systems or show-specific configuration.

---

## Deferred partials

| # | Item | What exists today | What remains | Design source section |
|---|------|------------------|--------------|----------------------|
| 1 | Guest portal UI | `spec/consent.schema.json` + intake consent flow designed; `dashboard/index.html` provides operator surface; guest-facing consent types are enumerated | A hosted, branded guest-facing web UI for intake forms, care profile collection, consent capture, and pre-interview scheduling — all currently done via manual form tools | `docs/guest-operations-os.md` §3 Scheduling Concierge; `spec/consent.schema.json` |
| 2 | Google Calendar free/busy connector + durable scheduler worker | Architecture fully specified in `docs/guest-operations-os.md` (Scheduling Concierge reads real calendar data, never mocked); `hospes/routing.py` carries city/studio routing logic | Live OAuth connector to host calendar(s); durable worker that reads actual free/busy slots and generates confirmed scheduling options; integration test against a real calendar | `docs/guest-operations-os.md` §3; `docs/studio-routing.md` |
| 3 | Asset Amplifier / content-engine production wiring | `adapters/content-engine--asset-amplifier.adapter.yaml` defines the adapter contract; `hospes/assets.py` exists; `spec/content_unit.schema.json` + `spec/asset.schema.json` are live | Runtime pipeline connection from approved Episode records through the content engine to Asset Amplifier for automated derivative production (clips, chapter cards, quote graphics) | `adapters/content-engine--asset-amplifier.adapter.yaml`; `docs/estate-composition.md` |
| 4 | Show-specific Voice Constitution variants | `config/voice.yaml` is the tracked version-1 voice contract and `hospes/voice.py` loads and enforces it; the governance adapters define the future composition boundary | A tenant/show-keyed registry for distinct voice contracts, preferred openers, tone parameters, and persona variants; the current externalized contract is shared | `config/voice.yaml`; `adapters/persona-voice-governance.adapter.yaml`; `hospes/voice.py` |
| 5 | Per-show outreach templates registry | `config/outreach_templates.yaml` externalizes the producer-cold, prior-collaborator, and Host-note templates; `hospes/drafts.py` validates and loads the tracked version-1 contract | Tenant/show-keyed template selection and revision history so operators can customize safe templates without editing Python while retaining Voice Constitution validation | `config/outreach_templates.yaml`; `hospes/drafts.py`; `docs/guest-operations-os.md` §2 Booking Desk |
| 6 | Tour/press/book-tour data source for City Intelligence Agent | `hospes/routing.py` implements city-overlap routing logic; the Scheduling Concierge is designed to intercept touring guests | Upstream integration with a real touring/press/book-tour data source (e.g. Songkick, publisher tour calendars, RSS feeds) that feeds city-overlap detection automatically; currently a manual input | `docs/studio-routing.md`; `hospes/routing.py` |
| 7 | Sponsor creative governance workflow | `spec/sponsor.schema.json` is live; `docs/ad-lab.md` specifies 6 commercial break formats and the house-ads-first policy | Script versioning system, claims-evidence records for sponsor claims (per `spec/claim_evidence.schema.json`), and compliance-approval workflow before any ad script reaches production | `docs/ad-lab.md`; `spec/sponsor.schema.json`; `spec/claim_evidence.schema.json` |
| 8 | Relationship nurture cadence policy engine per C0–C5 | `spec/relationship.schema.json` carries relationship state; `events.json` includes `relationship.nurture_due` and `relationship.followup_due`; relationship classes C0–C5 are fully specified in `spec/permission-matrix.yaml` | A policy engine that reads relationship class, last-contact timestamp, and episode history to compute the correct nurture cadence and fire `relationship.nurture_due` events at the right intervals — today this is a manual judgment | `spec/relationship.schema.json`; `spec/permission-matrix.yaml` relationship_classes; `docs/network-doctrine.md` |
| 9 | Owned-distribution launch tooling | Launch sequence fully specified in `docs/launch-sequence.md` (3 private pilots → bank 6 → trailer+3 drop); distribution strategy in `docs/network-doctrine.md`; `spec/content_unit.schema.json` + distribution events in `spec/events.json` | Tooling that connects approved ContentUnit records to distribution channels (RSS feed, YouTube, newsletter, show website) and tracks delivery evidence; today distribution is fully manual | `docs/launch-sequence.md`; `docs/network-doctrine.md`; `spec/events.json` `distribution.*` events |

## Candidate tenants (from the prior-art excavation, 2026-07-14)

| # | Item | What exists today | What remains | Design source |
|---|------|------------------|--------------|---------------|
| 10 | AMP LAB MEDIA as a third Show DNA tenant | Multi-show tenancy architecture (`dna/show.schema.json`, flagship + field instances); prior-art lineage registered in [prior-art.md](prior-art.md) | A `dna/amp-lab.show.yaml` instance for the academic video-essay/monthly-podcast channel, if and when the operator activates it — the 2025-02 ET4L/AMP LAB MEDIA commitment predates this repo and is the natural first outside-the-flagship tenant | `docs/prior-art.md` (ET4L Vision 2025-02-03); `docs/productization.md` |
