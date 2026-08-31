# Podcast / Conversation OS — GitHub Recomposition Blueprint

**Status:** Architecture audit; no repository mutations performed  
**Working product name:** Conversation Operations System  
**Primary proof environments:** Ari + Anthony flagship show; Anthony's mobile field show  
**Commercial direction:** Multi-tenant invisible production desk for podcasters and studio networks

## Executive conclusion

This is not a greenfield build.

The existing GitHub estate already contains most of the *horizontal machinery* required for a serious podcast operating system:

- relationship CRM and weak-tie pathfinding
- guest/opportunity pipeline mechanics
- inbox triage, follow-up aging, draft approvals, and audit trails
- booking and reminder interaction patterns
- multi-camera capture profiles and production runbooks
- canonical media ingestion, deduplication, metadata, and archival integrity
- transcript atomization, speaker/timestamp export, semantic analysis, and clip assembly
- editorial standards, episode production templates, and object-driven segment logic
- content fragmentation and platform-specific asset generation
- distribution dispatch, retries, receipts, and analytics
- voice/persona registries and deterministic style validation
- human-in-the-loop agent orchestration
- evidence-linked publishing and correction gates

The missing product is not another agent. It is a **podcast-specific domain kernel and composition boundary** that turns these systems into one coherent product.

## Governing architecture decision

Do **not** physically merge every repository into one giant codebase.

Use three modes of reuse:

1. **Retain as service** — mature systems keep their identity and expose a stable adapter.
2. **Extract as package** — portable logic moves into a shared package with provenance back to its source.
3. **Adapt as specification** — schemas, rubrics, workflows, and creative methods become podcast-domain configuration.
4. **Archive as precursor** — superseded or duplicate surfaces are preserved and redirected; nothing unique is destroyed.

## Canonical domain kernel

The central entity is **AppearanceOpportunity**, not `Guest` and not `Lead`.

A person can decline one opportunity, accept another, return years later, appear on multiple shows, or be represented through different routes. The system must preserve that distinction.

### Core entities

- `Tenant`
- `Network`
- `Show`
- `Host`
- `Person`
- `Organization`
- `ContactRoute`
- `Relationship`
- `RelationshipEdge`
- `AppearanceOpportunity`
- `Episode`
- `Studio`
- `AvailabilityWindow`
- `Booking`
- `Touchpoint`
- `Commitment`
- `ConsentRecord`
- `GuestCareProfile`
- `ResearchClaim`
- `EvidenceObject`
- `Segment`
- `Artifact`
- `MediaAsset`
- `TranscriptSegment`
- `ContentUnit`
- `DistributionJob`
- `SponsorCampaign`
- `ApprovalRequest`
- `AuditEvent`

### Appearance state machine

```text
DISCOVERED
→ RESEARCHING
→ QUALIFIED
→ EDITORIAL_REVIEW
→ APPROVED
→ CONTACT_ROUTE_IDENTIFIED
→ OUTREACH_DRAFTED
→ OUTREACH_APPROVED
→ OUTREACH_SENT
→ INTERESTED
→ SCHEDULING
→ BOOKED
→ INTAKE_PENDING
→ RELEASE_PENDING
→ PREP_IN_PROGRESS
→ RECORDING_READY
→ RECORDED
→ TRANSCRIPT_READY
→ CONTENT_IN_REVIEW
→ PUBLICATION_PENDING
→ PUBLISHED
→ RELATIONSHIP_NURTURE
```

Branch states:

```text
FOLLOW_UP_DUE
REVISIT_LATER
DECLINED
DO_NOT_CONTACT
PROTECTED_RELATIONSHIP
LEGAL_REVIEW
NEEDS_HUMAN
CANCELLED
```

## Target product architecture

```text
conversation-ops/
├── apps/
│   ├── operator/             # Ari/producer approvals and exception queue
│   ├── guest-portal/         # scheduling, intake, releases, care preferences
│   ├── show-console/         # episode thesis, research, segments, day-of packet
│   ├── network-admin/        # multi-tenant setup, studios, shows, policies
│   └── field-capture/        # Anthony's portable show profile and ingest
├── services/
│   ├── api/
│   ├── workflow-worker/
│   ├── mail-ingestor/
│   ├── calendar-router/
│   ├── media-ingestor/
│   ├── transcript-worker/
│   ├── content-worker/
│   └── distribution-worker/
├── packages/
│   ├── domain/
│   ├── database/
│   ├── events/
│   ├── policy/
│   ├── approvals/
│   ├── network-graph/
│   ├── voice/
│   ├── editorial/
│   ├── capture-profiles/
│   ├── integrations/
│   └── observability/
└── show-profiles/
    ├── flagship/
    └── field-show/
```

## Source-to-target composition

### Retain as services

#### `content-engine--asset-amplifier`
Owns derivative generation after a master recording is approved:
- clip extraction
- fragment scoring
- captions and platform variants
- resizing
- approval states
- scheduled release
- yield analytics

**Boundary:** It does not own canonical raw media or guest relationships.

#### `universal-mail--automation`
Owns inbox ingestion and correspondence workflow:
- thread ingestion
- classification
- escalation
- follow-up aging
- draft approvals
- delivery evidence
- privacy and redaction controls

**Boundary:** Podcast-specific intent classes and relationship permissions live in Conversation OS.

#### `media-ark`
Owns canonical source media:
- ingest
- hashes
- deduplication
- sidecars
- metadata
- archive verification
- repair

**Boundary:** Content Engine may read masters and write derivatives, but it must not become the archive of record.

#### `social-automation`
Owns channel dispatch mechanics:
- platform adapters
- retries
- circuit breakers
- delivery receipts
- distribution analytics

**Boundary:** Announcement templates and Content Engine create payloads; Social Automation delivers them.

### Extract as packages

#### From `application-pipeline`
Extract:
- opportunity state-machine patterns
- scoring framework
- funnel and velocity analytics
- relationship CRM semantics
- next-action and overdue-action logic
- weak-tie graph and pathfinding
- network-proximity scoring

Replace job-specific terms with:
- `application` → `appearance_opportunity`
- `organization target` → `guest / representative / institution`
- `interview` → `recording`
- `outcome` → `booked / declined / revisit / published`

#### From `public-record-data-scrapper`
Extract:
- production-grade multi-tenant contact service
- contact activity ledger
- many-to-many contact associations
- timezone, preferred contact method, tags, source provenance
- API, PostgreSQL, Redis, workers, OpenAPI, auth patterns

Do **not** reuse the public-record scraping purpose or lead language.

#### From `vox--architectura-gubernatio` and `vox--publica`
Extract:
- persona/voice registry
- anti-pattern detectors
- output linting
- human approval queue
- corpus comparison

Create separate voice profiles for:
- Ari personal note
- producer / guest desk
- flagship show
- Anthony field show
- sponsor creative
- public launch copy

#### From `agentic-titan`
Extract:
- approval gates
- audit event format
- bounded tool permissions
- budget controls
- model routing
- workflow DAG patterns

Do not import a free-roaming 22-agent swarm. Start with deterministic workflows and five bounded capabilities.

#### From `mirror-mirror`
Extract:
- booking UX
- slot-ranking preference schema
- confirmation flow
- reminder data model
- retry concepts
- operator dashboard patterns
- group booking patterns
- calendar export UX

Rewrite:
- mocked availability → Google Calendar free/busy + studio inventory
- browser interval scheduler → durable server-side workflow
- Spark KV → PostgreSQL
- hard-coded providers → hosts, guests, producers, studios

#### From `linguistic-atomization-framework`
Extract:
- transcript hierarchy
- semantic and temporal analysis
- entity extraction
- sentiment/emotional trajectory
- chapter and quote candidates
- rhetorical and contradiction analysis

#### From `materia-collider`
Extract:
- time-coded clip database
- searchable clip annotations
- coverage-gap reporting
- rough-cut assembly
- EDL/CSV/Markdown export
- usage and rights history

### Adapt as podcast-domain specifications

#### `salon-archive`
Adapt:
- session metadata
- participants and consent
- transcript review
- topic taxonomy
- outputs/artifacts
- participant and topic indices
- redaction and off-record rules

This becomes the Conversation Archive and Episode Knowledge Graph.

#### `praxis-perpetua` + `object-lessons`
Adapt:
- pre-production checklists
- episode structure
- clip logs
- research briefs
- SEO and thumbnail gates
- Shorts extraction plans
- performance review
- lessons learned
- object-driven episode and segment mechanics

#### `editorial-standards`
Adapt:
- document types
- required frontmatter
- controlled vocabulary
- review rubrics
- metadata gates

Create canonical documents:
- guest dossier
- appearance brief
- episode thesis
- segment card
- day-of packet
- sponsor brief
- publication package
- postmortem

#### `announcement-templates`
Adapt:
- single source → multiple channel outputs
- variable interpolation
- channel-specific formatting
- factual consistency
- publication checklists

#### `the-actual-news`
Adapt:
- atomic claim/evidence linkage
- provenance records
- deterministic publish gate
- immutable corrections
- public/internal boundary
- share-kit and offer-packet surfaces

Use this for:
- “Receipts” segments
- guest research claims
- ad claims
- sponsor approvals
- corrections to show notes and metadata

#### `collective-persona-operations`
Adapt:
- persona as machine-readable interface
- context-sensitive register
- composition rules
- legal persona transitions
- voice consistency validation

#### `a-i--skills`
Adapt:
- relational, give-first outreach
- research-before-contact
- connector strategy
- concise email structures
- distribution and repurposing methods

Do not inherit generic sequences blindly. The show’s policy should override them.

### Preserve as creative/R&D layers

#### `speech-score-engine`
Use later for:
- host/guest turn-duration diagnostics
- interruption and overlap analysis
- pacing drag
- rhythm monotony
- ad-sketch timing
- polyvocal segment design

It is not a core dependency for launch.

#### `sign-signal--voice-synth`
Preserve as the conceptual precursor to Speech Score Engine. Redirect new implementation work to the latter.

#### `meta-source--ledger-output`
Optional visual layer for:
- episode glyphs
- kinetic interstitials
- diagrams
- generative identity
- artifact rendering

### Archive or fold as independent active surfaces

These should not remain competing product centers unless deeper implementation justifies it:
- `kerygma-pipeline` → fold into distribution orchestration
- duplicate or precursor content-engine surfaces → preserve history, redirect to Asset Amplifier
- `universal-node-network` → retain specification; defer runtime adoption until needed
- `sign-signal--voice-synth` → preserve source corpus, redirect to Speech Score Engine

“No longer an active product surface” does not mean deletion.

## The five bounded AI capabilities

1. **Guest Intelligence**
   - source candidates
   - build dossiers
   - map representatives and relationship routes
   - score editorial fit and social cost

2. **Booking Desk**
   - draft outreach
   - classify replies
   - manage follow-up policy
   - route exceptions to humans

3. **Scheduling and Guest Experience**
   - studio/host/guest availability
   - intake and releases
   - accessibility and care preferences
   - reminders and logistics

4. **Episode Producer**
   - research claims and evidence
   - thesis and segment design
   - host brief
   - day-of packet
   - transcript and artifact extraction

5. **Relationship and Distribution Steward**
   - thank-you and commitments
   - publication notifications
   - content package
   - dispatch and analytics
   - long-term relationship state

## Event vocabulary

```text
person.created
relationship.edge_created
relationship.protected
appearance.candidate_created
appearance.qualified
appearance.approved
contact_route.verified
outreach.draft_ready
outreach.approved
outreach.sent
reply.received
reply.classified
followup.due
booking.proposed
booking.confirmed
booking.rescheduled
consent.signed
guest_intake.completed
episode.thesis_locked
research.claim_verified
recording.ready
recording.completed
media.ingested
transcript.ready
segment.identified
commitment.created
commitment.completed
content_unit.generated
content_unit.approved
distribution.dispatched
distribution.delivered
episode.published
correction.appended
relationship.followup_due
```

## Multi-tenant product model

Because the same system must support two internal shows and later outside podcasters, multi-tenancy is not a later feature.

```text
Network / Customer
├── Show
│   ├── Show DNA
│   ├── host roles
│   ├── voice profiles
│   ├── relationship policy
│   ├── segment library
│   ├── studio access
│   ├── distribution policy
│   └── sponsor policy
├── People and contact routes
├── protected relationship graph
├── assets and transcripts
└── audit and approvals
```

A guest relationship may be network-level, but permission to use it must be explicit per show.

## Two initial show profiles

### Flagship
- controlled studio environment
- LA / NYC / Austin routing
- hidden microphones
- stable visual grammar
- fixed and rotating segments
- claim → stress test → artifact
- Ari’s effort concentrated in approval and recording

### Anthony field show
- portable GoPro/capture kit
- guest environment as third participant
- place → object → intervention → artifact
- same guest desk, relationship graph, archive, and distribution system
- different Show DNA and visual protocol

## Build order

### 0. Lock the domain
- approve entity schema
- approve events
- approve permissions
- approve relationship classes
- approve state machine

### 1. Build the control plane
- operator dashboard
- candidate cards
- protected-relationship controls
- approval queue
- exception queue
- audit log

### 2. Activate guest operations
- contact service migration
- relationship graph import
- Universal Mail adapter
- Gmail thread mapping
- real Calendar/studio free-busy
- durable reminders
- guest portal

### 3. Activate episode operations
- episode dossier
- research claim/evidence ledger
- segment cards
- day-of packet
- Salon Archive transcript schema
- Media Ark ingest
- Materia clip database

### 4. Activate the content yield loop
- master asset → Asset Amplifier
- clip review
- voice/editorial gates
- announcement compilation
- social dispatch
- delivery receipts and analytics

### 5. Productize for the network
- tenant isolation
- configurable Show DNA
- role-based access
- billing
- customer onboarding
- template marketplace
- permissioned cross-show guest routing

### 6. Add advanced creative intelligence
- speech rhythm diagnostics
- overlap and interruption analysis
- generative visual artifacts
- adaptive segment scoring
- ad-format experimentation

## Immediate repository work items

1. Create one new canonical product repository only after the domain and integration manifest are accepted.
2. Preserve all source repositories; add provenance references for extracted modules.
3. Write ADRs for:
   - service vs. package boundaries
   - canonical media ownership
   - contact/relationship ownership
   - workflow engine
   - multi-tenancy
   - evidence and correction policy
4. Build adapters before rewrites.
5. Replace mock scheduling with real calendar infrastructure.
6. Create seed data for Ari, Anthony, three studios, two shows, five relationship classes, and three pilot opportunities.
7. Use the flagship as the first production tenant.
8. Install a second internal tenant for the field show.
9. Only then install a third tenant for an outside podcaster.

## Product statement

> An invisible guest, production, and distribution desk for podcasters: it turns a host’s relationships, studios, recordings, and editorial instincts into a governed operating system while keeping consequential decisions human.

## What is genuinely new

The new value does not come from claiming that all prior repositories were secretly one podcast app.

It comes from recognizing that they form a reusable institutional stack and adding the missing composition:

- one podcast-domain ontology
- one permissioned relationship graph
- one deterministic appearance lifecycle
- one multi-show control plane
- one evidence-backed editorial layer
- one governed path from candidate to relationship to recording to archive to distribution

That composition is the product.
