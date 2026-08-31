"""HOSPES — the guest-operations engine.

HOSPES (Latin: *guest* and *host* alike) is the deterministic core of a
podcast guest-operations system. It owns the guest lifecycle state machine,
the pipeline of candidates, the human-gated approval flow, studio routing,
outreach *drafting* (never sending), research/segment briefs, the recording
asset checklist, the promises ledger, and rule-based reply triage.

Design invariants (enforced in code, not merely documented):

* **Local/private by default.** SQLite and manual providers work without a
  hosted service; network adapters remain disabled or unconfigured until their
  credential references resolve.
* **Human authority at the boundary.** Scheduled work may prepare drafts but
  cannot send or publish. Connected execution requires an attributable,
  action- and subject-bound authorization receipt.
* **Dignity + private custody.** Public projections expose operational facts
  and opaque references only. Sensitive fields and artifacts are encrypted
  under tenant-scoped keys before persistence.

Working alias: the recomposition blueprint calls this the
"conversation-operations-system"; HOSPES is the product name used everywhere.
"""

__all__ = [
    "states",
    "pipeline",
    "approvals",
    "routing",
    "drafts",
    "voice",
    "briefs",
    "assets",
    "commitments",
    "triage",
    "dna",
    "paths",
    "store",
    "encryption",
    "artifacts",
    "authentication",
    "jobs",
    "suggest",
    "contact_roster",
    "sponsors",
    "service",
]

__version__ = "1.0.0"
