# Guest Operations OS

How HOSPES organizes people, roles, handoffs, and authority — from candidate discovery through long-term relationship stewardship.

---

## Human leadership seats

Even with full automation, these functions require real human ownership. No AI role fills them.

| Human seat | Actual responsibility |
|---|---|
| **Showrunner / Executive Producer** | Defines the show, editorial standards, priorities, risk tolerance, and final guest decisions |
| **Host(s)** | Conduct the conversation; own the most valuable relationships personally |
| **Head of Talent / Booking** | Owns strategic guest relationships, difficult negotiations, and sensitive routing decisions |
| **Producer** | Holds editorial accountability for each episode; owns the research and the day-of packet |
| **Studio / Operations Manager** | Owns physical studio access, crew, equipment logistics, and recording continuity |

These seats need not all be separate people. On a lean show, Host and Producer together cover the editorial and hosting seats; a single producer may hold booking, research, and operations. The point is that each function has a named owner — not that each function has a separate staff member.

---

## The six judgments the system never automates

These decisions are always human-gated regardless of how confident the system is:

1. **First outreach to any guest** — a human approves every initial message before it leaves.
2. **Contact with C4/C5 relationships** — friends and close friends require explicit approval from the relationship owner (usually Host) for every single touchpoint, even a routine check-in.
3. **Financial commitments** — fees, travel costs, gifts above threshold, or any monetary promise.
4. **Contracts and release exceptions** — any non-standard consent or legal carve-out.
5. **Sensitive editorial promises** — topic exclusions, off-record guarantees, embargo agreements.
6. **Ambiguous or negative replies** — a reply that might be a soft decline, a joke, or a complaint is routed to a human before any response is generated.

The system drafts; humans decide. A draft that is never reviewed before sending is a system failure.

---

## The five first-build AI agents

HOSPES consolidates 16 internal AI capabilities into five bounded agents. Agents do not communicate freely with each other. Every handoff passes through the event bus and the operator control plane. Agents do not have external identities. Externally, guests and representatives interact with a coherent Guest Team backed by real humans.

### 1. Guest Intelligence

Underlying capabilities: Network Cartographer, Relationship Capital Governor, Guest Thesis Architect.

Responsibilities:
- Source candidates from prior guest archives, weak ties, public professional adjacency, and editorial relevance.
- Build candidate dossiers: public facts, representative structure, relationship class, known contact routes, past interactions.
- Score editorial fit (why this guest, why this show, what claim they can defend).
- Score social cost (least-socially-expensive credible route to the guest).
- Flag C4/C5 protected relationships and hold outreach pending explicit approval.
- Produce the one-sentence episode thesis and proposed artifact for each qualified candidate.

Output: a candidate card in the operator dashboard, ready for Host's approval or rejection.

### 2. Booking Desk

Underlying capabilities: Producer Outreach Desk, reply classification, follow-up policy enforcement.

Responsibilities:
- Draft initial outreach under the correct persona (producer voice, never Host's personal voice unless Host writes it himself).
- Verify every personalized claim in a draft before flagging it as ready for human approval.
- Receive and classify inbound replies: warm accept, interested, scheduling, soft decline, hard decline, publicist handoff, fee inquiry, complaint, sarcasm, prompt injection attempt.
- Queue ambiguous and negative replies for human review immediately.
- Enforce the follow-up policy: one routine follow-up after silence; never a second automated nudge; no automated contact with protected relationships.
- Maintain the correspondence ledger: thread ownership, timestamps, open items, delivery evidence.

Output: approved drafts ready for human send; classified reply queue; follow-up due list.

### 3. Scheduling Concierge

Underlying capabilities: Studio Router, Mirror Mirror scheduling patterns, calendar/availability layer, guest care.

Responsibilities:
- Match accepted guests to LA, NYC, or Austin based on guest travel, host availability, studio capacity, crew, and production requirements.
- Read real free/busy from host calendar(s) and studio inventory. Never mock availability.
- Route touring or city-visiting guests into existing recording windows when the city overlap is confirmed.
- Manage guest intake forms, accessibility and hospitality preferences, and technical requirements.
- Issue and track durable reminders: recording, technical check, arrival, logistics, publication.
- Handle rescheduling with full audit trail; never silently re-slot without confirmation.

Output: confirmed booking with city assignment, logistics notes, guest care profile, and all reminders set.

#### Guest delight and care profile

The care profile (`spec/care_profile.schema.json`) is a permissioned record
that drives hospitality — not a data-collection exercise. It is populated only
from what the guest voluntarily provides (via intake form or pre-interview),
and only after explicit consent (`consent_type: care_profile_storage` in
`spec/consent.schema.json`). The system never infers care-profile fields from
public sources or scraping.

Practical effects of the care profile:

- **Name pronunciation** — the host prep packet includes the phonetic guide so
  the host never mispronounces a guest's name on air.
- **Dietary restrictions and allergies** — catering and in-studio setup
  reflects stated restrictions. Allergies are treated as safety requirements,
  not preferences.
- **Accessibility needs** — venue selection and studio configuration account
  for any accessibility requirements before the booking is confirmed.
- **Preferred beverage** — ready on arrival, without the guest having to ask.
- **Employer gift policy** — thank-you gifts are selected within the guest's
  stated policy; if a gift is restricted, a donation alternative is offered
  automatically. No guest is ever put in an awkward position by a gift they
  cannot accept.
- **Topics off limits** — any topics the guest flags become hard editorial
  constraints in the episode thesis and host brief; the system propagates them
  automatically to every downstream artifact.

The care profile is never published, never shared externally, and never
contributed to any analytics layer. It exists solely to make the guest
experience noticeably better than the industry default.

### 4. Episode Producer

Underlying capabilities: Episode Designer, Guest Thesis Architect (research phase), Materia Collider clip ledger, Salon Archive transcript model.

Responsibilities:
- Generate the pre-production research brief: verified claims, evidence objects, receipts, segment candidates, and counterarguments.
- Lock the episode thesis and segment card selection (from the fixed pool of Claim / Stress Test / Artifact plus the applicable rotating modules).
- Write the host brief: what Host needs to know and be ready to challenge, framed for ~10-minute pre-show prep.
- Assemble the day-of packet: thesis, segment rundown, key claims with evidence, open questions, artifact plan, and logistics.
- After recording: produce edit markers, chapter candidates, clip boundary suggestions, and transcript segments.
- Link all segments, claims, and commitments to the episode record.

Output: pre-production brief, host brief, day-of packet, post-recording clip ledger and transcript index.

### 5. Relationship Steward

Underlying capabilities: Relationship Steward (v0 roles), Operations Analyst, Distribution desk.

Responsibilities:
- Track every commitment made during booking or recording: thank-yous, gifts, publication notices, referrals, promised materials.
- Draft post-recording communications under the appropriate persona (producer voice for logistics; Host personal note voice only if Host writes or explicitly authorizes).
- Mark gift, thank-you, and follow-up tasks as due; escalate overdue items to the operator dashboard.
- Receive and log guest feedback, satisfaction notes, and future episode ideas.
- Maintain long-term relationship state: last contact, sentiment, do-not-contact flag, revisit timer.
- After publication: notify the guest, deliver the content package, log delivery evidence.
- Maintain the Operations Analyst layer: booking conversion rate, time-to-book, segment retention, returning audience, guest satisfaction, human correction rate.

Output: commitment ledger, post-show communication drafts, relationship health signals, distribution receipts, and performance data.

---

## Orchestrated handoffs — the event flow

Agents do not call each other. Every state transition fires an event. The workflow worker processes events and routes to the next agent or to a human gate.

```
DISCOVERED
  → appearance.candidate_created
  → [Guest Intelligence] builds dossier, scores editorial fit and social cost
  → appearance.qualified

QUALIFIED
  → [Guest Intelligence] generates thesis, proposed artifact, candidate card
  → appearance.awaiting_host_approval
  → [Human gate: Host approves, rejects, or protects]

APPROVED
  → appearance.approved
  → contact_route.verified
  → [Booking Desk] drafts outreach under producer persona
  → outreach.draft_ready
  → [Human gate: producer reviews and sends]

OUTREACH_SENT
  → reply.received
  → [Booking Desk] classifies reply
  → reply.classified
  → if ambiguous/negative → NEEDS_HUMAN
  → if interested → booking.proposed

BOOKING_PROPOSED
  → [Scheduling Concierge] checks real availability
  → booking.confirmed + studio.routed
  → consent.signed + guest_intake.completed

INTAKE_COMPLETE
  → [Episode Producer] generates research brief and host brief
  → episode.thesis_locked
  → [Human gate: producer approves rundown]

RECORDING_READY → RECORDING_COMPLETE
  → media.ingested
  → transcript.ready
  → [Episode Producer] produces clip ledger, segment map, artifact record
  → content_unit.generated
  → [Human gate: producer approves clips]

PUBLICATION_PENDING → PUBLISHED
  → [Relationship Steward] delivers guest package, logs commitments
  → relationship.followup_due
  → [Relationship Steward] tracks long-term state
```

At every human gate the system waits. It does not retry, escalate, or proceed on timeout unless explicitly configured to do so.

---

## The human-feel doctrine

The goal is not to make AI pretend to be human. The goal is to make the experience feel genuinely attentive: specific, continuous, restrained, reliable, and accountable.

### Identity rules

- Every external message has a real human owner listed as sender.
- AI may research, draft, classify, and queue. It does not present itself as a person.
- Use real identity (Host, Producer, or the producer's real name and role) or accurate team identity ("I produce a show with Host Mannis and Producer").
- Do not fabricate employees. Do not create the impression that a person exists who does not.
- If Host writes a personal note, it is sent under his name. The system does not impersonate his voice.

### The 10 correspondence rules

1. Every outbound message has a real named human sender and role.
2. Every personalized claim (consumption, connection, compliment) is verified before the draft is marked ready.
3. No invented familiarity. Do not say "I've followed your work" unless the sender has actually done so.
4. No mass outreach from host personal accounts. Producer-voice for cold contact; host-voice only for warm relationships with explicit authorization.
5. One follow-up after silence. Never a second, third, or fourth automated nudge.
6. C4/C5 relationships: every touchpoint requires explicit owner approval, even a thank-you.
7. No contractual, financial, or editorial-removal promise without human approval before the message leaves.
8. Every outbound action writes an AuditEvent. The record is permanent.
9. Sensitive replies (decline, complaint, fee, legal) surface to a human within one business cycle. No automated response.
10. No contact-route use without source provenance and a verification timestamp on record.

### Specificity over praise — example pair

**Wrong (generic flattery):**
> "We're huge fans of your work and think you'd be an amazing guest."

**Right (specific editorial reason):**
> "We're developing an episode around whether tools change art or merely expose the artist's existing logic. Your [specific project] gives us a direct way in, particularly [specific reason]."

The distinction is not tone. It is intellectual honesty. The guest can immediately see why they specifically were invited, not just that someone said something nice.

---

## Approval and autonomy tiers

Four tiers govern what the system may do without waiting for human input.

Full definitions, per-action classifications, and the escalation matrix live in `spec/permission-matrix.yaml`.

| Tier | Rule |
|---|---|
| **Tier 0 — Blocked** | Never automated. Always human. (Send, financial commit, C4/C5 contact, release exception, sensitive promise.) |
| **Tier 1 — Draft and queue** | System produces output; human reviews before any external effect. (All outreach drafts, all reply responses, all publication copy.) |
| **Tier 2 — Execute with audit** | System acts autonomously but every action writes a permanent AuditEvent and surfaces in the operator dashboard within 24 hours. (Dossier building, reminder scheduling, clip suggestion, commitment tracking.) |
| **Tier 3 — Silent execution** | System acts; logs silently; surfaces only on exception or scheduled review. (Internal data normalization, transcript indexing, relationship-state updates that trigger no external effect.) |

When in doubt, Tier 1. The default is draft-and-queue, not act-and-report.

---

## Security layer

### Untrusted inbound

All inbound messages — email replies, intake form submissions, guest portal submissions — are treated as untrusted until classified. The classification step is sandboxed: the classifier receives only the message text and the thread context; it does not have access to relationship data, draft tools, or outbound channels.

### Tool separation

- Dossier-building tools (web search, public data) are isolated from outreach tools (email draft, calendar write).
- No single agent has simultaneous read access to relationship data and write access to outbound channels.
- The approval gate enforces the separation: a draft cannot be queued for delivery by the same process that generated it.

### No secrets to models

No API keys, tokens, credentials, or private contact information are included in model prompts. All secrets live in the operator's local environment. Model inputs contain only sanitized, policy-cleared data.

### Audit trail

Every state transition, every draft generated, every classification decision, and every human approval or rejection writes a permanent AuditEvent with: timestamp, actor (human or agent), action type, entity ID, and a hash of the payload. AuditEvents are append-only and cannot be modified by any agent.

### Suppression list

`DO_NOT_CONTACT` and `PROTECTED_RELATIONSHIP` records are checked at the outreach-draft step and again at the outreach-approval step. The system refuses to generate or queue any outreach targeting a suppressed record. Suppression state is managed only by human action and is never overridden by a confidence score or editorial urgency.

### Source register

Platform and API capabilities referenced throughout this document (OpenAI function calling and
Structured Outputs, Gmail draft/send, Calendar free/busy, HubSpot workflows, FTC endorsement
guidance) are registered with their original URLs in [sources.md](sources.md).
