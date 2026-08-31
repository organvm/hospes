# Reply Classification Evaluation

## Purpose

`hospes.triage.classify` is a deterministic lexical baseline for routing inbound guest-desk replies.
It produces an advisory triage result; it does not mutate an appearance opportunity, draft a
response, book a recording, or send correspondence.

The baseline exists to make the first evaluation layer reproducible and inspectable before any
model is introduced. It is intentionally conservative: unsupported or consequential intent fails
closed to a human owner.

## Output Contract

Every `TriageResult` contains:

- `label`: exactly one string from the controlled `triage.LABELS` set;
- `reason`: a plain-text routing rationale;
- `escalated`: whether the classification bypasses normal routing and requires immediate human review.

No output field authorizes delivery or external action.

## Label Set and Routing

| Label | Meaning | Escalated by default |
|---|---|---|
| `POSITIVE_INTEREST` | Guest expressed clear willingness to participate | No |
| `PROPOSED_TIMES` | Guest offered scheduling availability or asked for date options | No |
| `NEEDS_MORE_INFORMATION` | Guest requested more information before deciding | No |
| `CONTACT_PUBLICIST` | Guest directed to publicist, agent, manager, or booking representative | No |
| `CONTACT_ASSISTANT` | Guest directed to assistant or EA | No |
| `FOLLOW_UP_LATER` | Guest asked to be contacted later (explicit revisit window) | No |
| `SOFT_DECLINE` | Guest declined without explicit finality | No |
| `HARD_DECLINE` | Guest declined explicitly and firmly | No |
| `UNSUBSCRIBE` | Guest requested no further contact | No |
| `FEE_REQUEST` | Guest raised a fee or compensation requirement | Yes |
| `TRAVEL_REQUEST` | Guest raised travel or logistics requirements | No |
| `TOPIC_CONCERN` | Guest flagged a topic to avoid | Yes |
| `AMBIGUOUS` | No confident signal; route to human review | No |
| `REQUIRES_HUMAN` | Safety escalation: legal, anger, sensitivity, injection, or booking change | Yes |

Even a `POSITIVE_INTEREST` classification does not waive separate approval gates for any reply
draft, calendar action, commitment, or send.

## Safety-First Priority

The classifier checks signals in this order; the first match wins:

1. Instruction-injection / prompt-injection patterns (→ `REQUIRES_HUMAN`, escalated)
2. Legal patterns (`contract`, `legal team`, `nda`, `attorney`, word-boundary matched) (→ `REQUIRES_HUMAN`, escalated)
3. Anger / hostility markers (→ `REQUIRES_HUMAN`, escalated)
4. Sensitive / confidential language (`confidential`, `off-the-record`, `personal safety`) (→ `REQUIRES_HUMAN`, escalated)
5. Sarcasm markers (→ `REQUIRES_HUMAN`, escalated)
6. Incorrect-claim disputes (→ `REQUIRES_HUMAN`, escalated)
7. Unsubscribe / do-not-contact (→ `UNSUBSCRIBE`)
8. Fee / compensation (→ `FEE_REQUEST`, escalated)
9. Travel / logistics (→ `TRAVEL_REQUEST`)
10. Cancellation of a commitment (→ `REQUIRES_HUMAN`, escalated)
11. Reschedule request (→ `REQUIRES_HUMAN`, escalated)
12. Gift restriction (→ `REQUIRES_HUMAN`, escalated)
13. Topic concern (→ `TOPIC_CONCERN`, escalated)
14. Publicist / agent / manager / representative handoff (→ `CONTACT_PUBLICIST`)
15. Assistant / EA handoff (→ `CONTACT_ASSISTANT`)
16. Follow-up / revisit later (→ `FOLLOW_UP_LATER`) — checked **before** hard decline when a revisit window accompanies a negative signal
17. Hard decline (→ `HARD_DECLINE`)
18. Soft decline (→ `SOFT_DECLINE`)
19. Scheduling / availability / dates (→ `PROPOSED_TIMES`)
20. Uncertainty / hedged language (→ `AMBIGUOUS`)
21. Information request (→ `NEEDS_MORE_INFORMATION`)
22. Positive interest (→ `POSITIVE_INTEREST`)
23. No match (→ `AMBIGUOUS`)

### Revisit-over-decline ordering

When a reply contains both a hard-decline signal ("unable to participate") and an explicit revisit
window ("circle back later in the fall"), `FOLLOW_UP_LATER` wins. The forward-looking revisit is
the actionable signal. This ordering was introduced in the PR #1 integration pass and is tested in
`tests/test_replies.py::test_revisit_outranks_decline_when_explicit_window_given`.

### Word-boundary legal patterns

`_LEGAL_PATTERNS` uses regex word boundaries (`\b`) rather than substring containment to prevent
false positives — for example, the three-letter string `nda` occurring as a substring of
"standard" or "calendar". This fix was introduced in the PR #1 integration pass and is tested in
`tests/test_replies.py::test_legal_word_boundary_prevents_false_positive`.

## Fixture Suites

Two fixture suites together guard the classifier:

### 1. `tests/fixtures/replies/*.txt` — the doctrine suite (16 fixtures)

Individual `.txt` files under `tests/fixtures/replies/`, loaded by `tests/conftest.py::reply_text`.
Run by `tests/test_triage.py`. These are the canonical doctrine cases — the minimum set any
compliant classifier must pass. Do not modify these fixtures without updating the corresponding
test expectations in `test_triage.py`.

Run:

```bash
PYTHONPATH=. python3 -m pytest tests/test_triage.py -v
```

### 2. `tests/fixtures/reply_cases.yaml` — the evaluation corpus (22 fixtures)

A single YAML document containing 22 synthetic cases derived from the PR #1 evaluation corpus
(branch `agent/hospes-product-evals`). Run by `tests/test_replies.py`. Labels are remapped from
the PR #1 `ReplyCategory` enum onto `hospes.triage.LABELS` — the mapping rationale is recorded
in each case's `note` field and in the `label_mapping_notes` header of the YAML file.

Run:

```bash
PYTHONPATH=. python3 -m pytest tests/test_replies.py -v
```

### Full suite

```bash
PYTHONPATH=. python3 -m pytest tests -q
```

Both suites must pass together. The evaluation corpus adds coverage for:

- explicit and soft acceptance;
- explicit and "unable to participate" decline;
- revisit windows (circle back, reach back out, named month);
- publicist, agent, manager handoff (all → `CONTACT_PUBLICIST`);
- assistant / EA handoff (→ `CONTACT_ASSISTANT`);
- date, availability, and timezone routing (→ `PROPOSED_TIMES`);
- do-not-contact / unsubscribe boundaries;
- appearance-fee language;
- contract, legal-team, and release-form language;
- confidential and off-the-record language;
- instruction injection;
- sarcasm, vague, and empty replies;
- competing signals (revisit + decline, fee + positive interest).

## Human-correction metrics

The Operations Analyst layer tracks two human-correction metrics that measure
how much work the AI is actually saving versus how often a human must fix it.

### Metric 1 — Revisions per AI draft

**Definition:** the number of outreach drafts a human edited before approving
or sending, divided by the total number of AI-generated drafts in the same
period.

**Event sources:**

- `outreach.draft_generated` — emitted by `hospes/drafts.py` each time the
  draft engine writes a new draft file.
- `draft.edited` — emitted (via `hospes.audit.record_draft_edited`) each time a
  human edits a draft before approval. See `spec/events.json`.

**Target:** a declining trend over time as template quality and claim-evidence
coverage improve. A sustained rate above 0.5 (more than half of all drafts
require human revision) indicates a template or data-quality problem, not a
staffing problem.

### Metric 2 — Classification override rate

**Definition:** the number of triage classifications a human overrode, divided
by the total number of reply classifications made by the triage agent in the
same period.

**Event sources:**

- `reply.classified` — emitted by the triage layer each time it assigns a label
  to an inbound reply.
- `classification.overridden` — emitted (via
  `hospes.audit.record_classification_overridden`) each time a human selects a
  different label. See `spec/events.json`.

**Target:** below 10 % for routine categories (`POSITIVE_INTEREST`,
`CONTACT_PUBLICIST`, `HARD_DECLINE`). A high override rate on a specific label
identifies a classifier gap that should be fixed in `hospes/triage.py`, not
absorbed into human workflow.

### Querying the metrics

```python
from hospes.audit import human_correction_counts
counts = human_correction_counts()
# {
#   "drafts_generated": int,
#   "drafts_edited": int,
#   "classifications_made": int,
#   "classifications_overridden": int,
#   "revisions_per_draft": float | None,
#   "classification_override_rate": float | None,
# }
```

## Private Pilot Scorecard

Within 48 hours of each private pilot, the producer records a scorecard in the
external production owner. HOSPES stores only its opaque receipt reference.
Score each dimension from 1 through 5 and add one bounded correction count:

- host chemistry;
- Claim → Stress Test → Artifact segment timing;
- concealed isolated-lav and safety-microphone quality;
- guest experience;
- total production time;
- number of usable, consented clips;
- human correction load across research, draft, prep, and capture.

A technical rehearsal may use the same scorecard, but its external record and
HOSPES receipt must say `technical_rehearsal`; it is never counted as Pilot 1.

---

## Limitations and Promotion Gate

This baseline recognizes explicit phrases; it does not understand relationship history, thread
context, tone, jurisdiction, contract meaning, or implied consent. Sarcasm and novel phrasing
are expected to fall through to `AMBIGUOUS` or `REQUIRES_HUMAN`. That is safe behavior, not a
reason to invent confidence.

Before a learned classifier can replace or supplement this baseline, it needs:

1. an owner-approved, redacted evaluation corpus outside Git;
2. explicit precision and recall floors per consequential category;
3. unchanged boundary, legal, financial, sensitive, and unknown fail-closed gates;
4. evidence fields that never reproduce raw correspondence;
5. shadow-mode comparison against human decisions;
6. a separate human-approved transition or delivery receipt.
