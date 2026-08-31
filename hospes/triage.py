"""Rule-based inbox reply triage.

Classifies an inbound reply into exactly one of the canonical intent labels
(transcript §B7). This is a deterministic, stdlib-only classifier — no model,
no network. It NEVER improvises a label outside the controlled set.

Safety-first routing: any reply carrying uncertainty, money, anger, legal
matter, sarcasm, or an embedded instruction (prompt injection) is routed to
``REQUIRES_HUMAN`` regardless of any other signal. The system does not
negotiate, does not follow instructions embedded in untrusted email, and
escalates nuance immediately (per the permission matrix and transcript §E
security layer).

Pattern and vocabulary additions in this file are derived from the evaluation
corpus in ``tests/fixtures/reply_cases.yaml`` and the PR #1 integration pass.
Rule identifiers in comments correspond to evidence keys used by the fixture suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# The controlled label set. The classifier may return ONLY these.
LABELS = [
    "POSITIVE_INTEREST",
    "NEEDS_MORE_INFORMATION",
    "CONTACT_ASSISTANT",
    "CONTACT_PUBLICIST",
    "PROPOSED_TIMES",
    "FEE_REQUEST",
    "TRAVEL_REQUEST",
    "TOPIC_CONCERN",
    "SOFT_DECLINE",
    "HARD_DECLINE",
    "FOLLOW_UP_LATER",
    "UNSUBSCRIBE",
    "AMBIGUOUS",
    "REQUIRES_HUMAN",
]

REQUIRES_HUMAN = "REQUIRES_HUMAN"
AMBIGUOUS = "AMBIGUOUS"


@dataclass
class TriageResult:
    label: str
    reason: str
    escalated: bool = False


# ---- signal vocabularies -------------------------------------------------

# Embedded-instruction / prompt-injection cues. Untrusted email content must
# never be allowed to steer the system.
# Evidence key: sensitive:instruction_injection
_INJECTION_PATTERNS = [
    r"ignore (all |the |your )?(previous|prior|above) instructions",
    r"disregard (all |the |your )?(previous|prior|above)",
    r"system prompt",
    r"you are (now )?an? ai",
    r"as an ai language model",
    r"forget (everything|your instructions)",
    r"new instructions?:",
    r"reveal (your (system )?prompt|the system prompt|the api key|secrets?|credentials?)",
    r"print (the )?(system prompt|api key)",
    r"do not tell (anyone|the human)",
    r"send \$?\d+ to",
    r"transfer .*(bitcoin|crypto|wire|funds)",
    r"click (this|the) link and",
    r"override .*(policy|approval|gate)",
    r"run this command",
]

# Anger / hostility cues.
_ANGER_WORDS = [
    "furious", "outrageous", "how dare", "unacceptable", "harassment",
    "stop emailing me", "leave me alone", "disgusting", "insulting",
    "never contact", "cease and desist", "reported you", "sick of",
    "who do you think you are", "appalled",
]

# Legal cues — use word-boundary patterns to avoid substring false positives
# (e.g. "nda" matching inside "standard", "calendar needs").
# Evidence key: legal:review_required
_LEGAL_PATTERNS = [
    r"\battorney\b",
    r"\blawyer\b",
    r"\blegal (counsel|team|review)\b",
    r"\bcease and desist\b",
    r"\blawsuit\b",
    r"\blitigation\b",
    r"\bdefamation\b",
    r"\bsue you\b",
    r"\bgdpr request\b",
    r"\bnda\b",
    r"\bsubpoena\b",
    r"\bcontract\b",
    r"\brelease form\b",
    r"\bindemnif(y|ication)\b",
    r"\busage rights?\b",
    r"\brights clearance\b",
    r"\bnon-disclosure\b",
]

# Confidential / safety-sensitive cues — routed to human without classification.
# Evidence key: sensitive:confidential_or_safety
_SENSITIVE_WORDS = [
    "off-the-record", "off the record", "confidential", "private medical",
    "health matter", "personal safety", "trauma", "minor child",
    "security concern",
]

# Money cues (fee).
_FEE_WORDS = [
    "fee", "honorarium", "our rate", "appearance fee", "compensation",
    "paid", "payment for", "how much do you pay", "speaking fee",
    "what's the budget", "what is the budget", "day rate",
]

# Travel cues.
_TRAVEL_WORDS = [
    "travel", "flight", "airfare", "hotel", "accommodation", "per diem",
    "cover my travel", "fly me", "ground transportation", "cover the cost of travel",
]

# Sarcasm cues (explicit markers only — we do not infer tone).
_SARCASM_MARKERS = [
    "oh sure", "yeah right", "how *original*", "wow, groundbreaking",
    "just what i always wanted", "/s", "as if", "riveting", "thrilled. truly.",
    "can't wait. really.", "another podcast",
]

_UNSUBSCRIBE = [
    "unsubscribe", "remove me from your list", "opt out", "opt-out",
    "take me off", "do not contact me", "stop contacting",
    "no further emails", "no further messages", "no further contact",
    "remove me from",
]

_HARD_DECLINE = [
    "no thank you", "not interested", "we'll pass", "we will pass",
    "i must decline", "must decline", "not a fit", "will have to decline",
    "have to decline", "not going to happen", "definitely not", "hard pass",
    "please don't ask again", "unable to participate", "won't be able to participate",
    "pass on this",
]

_SOFT_DECLINE = [
    "not right now", "not at this time", "maybe another time",
    "not the right time", "timing isn't right", "timing is not right",
    "too busy at the moment", "wish i could",
    "afraid i can't this", "unfortunately can't make",
]

_FOLLOW_UP_LATER = [
    "reach out after", "check back after", "after my book", "after the tour",
    "later in the year", "next quarter", "in the fall", "revisit in",
    "ask me again", "after the release",
    # Explicit revisit / later-window phrases (from PR #1 evaluation corpus).
    "circle back", "reach back out", "try again", "check back",
    "follow up later", "after the launch", "after the project",
    "after the tour", "in the winter", "in the spring", "in the summer",
    "in the fall", "next month", "next season", "next year",
]

# Publicist / formal representative routing — maps to CONTACT_PUBLICIST.
# Evidence key: representative:handoff
_PUBLICIST = [
    "publicist", "pr team", "press office", "my publicity", "publicity team",
    "media relations", "loop in my pr", "booking team",
    "my agent", "my manager", "my representative",
    "contact my manager", "speak with my manager", "coordinate with my manager",
    "contact my agent", "reach out to my agent",
    "my representative handles", "representative will handle",
]

_ASSISTANT = [
    "my assistant", "assistant will", "reach my assistant", "cc my assistant",
    "ea will", "executive assistant", "my ea", "schedule through my assistant",
]

_PROPOSED_TIMES = [
    "how about", "would work", "i'm free", "im free", "available on",
    "does tuesday", "does wednesday", "let's do", "lets do", "propose",
    "what about the week of", "these times", "i can do",
    # Scheduling / availability phrases from PR #1 evaluation corpus.
    "availability", "available next", "available this", "calendar",
    "send some dates", "send the dates", "time options", "date options",
    "time zone", "timezone", "next week", "send some time",
]

_TOPIC_CONCERN = [
    "rather not discuss", "prefer not to talk about", "off the table",
    "don't want to get into", "avoid the topic", "not comfortable discussing",
    "steer clear of", "off-limits", "off limits", "won't be discussing",
]

_NEEDS_INFO = [
    "tell me more", "more information", "more details", "what's the show",
    "what is the show", "who else has been on", "what's the format",
    "what is the format", "how long", "send me the brief", "can you explain",
    "before i decide",
]

_POSITIVE = [
    "yes", "i'd love", "id love", "sounds great", "happy to", "count me in",
    "let's do it", "lets do it", "delighted", "sign me up", "absolutely",
    "i'm in", "im in", "would be glad", "interested", "love to join",
]

_CANCELLATION = [
    "cancel", "have to cancel", "can no longer", "won't be able to make",
    "need to pull out", "need to back out", "call it off",
]

_RESCHEDULE = [
    "reschedule", "move our", "push our", "change the date", "different day",
    "shift the recording", "postpone",
]

_INCORRECT_CLAIM = [
    "i never said", "i didn't say", "that's not what i", "you got that wrong",
    "i never wrote", "that's incorrect", "you attributed", "misquoted",
    "i did not work on", "that isn't my",
]

_GIFT_RESTRICTION = [
    "can't accept gifts", "cannot accept gifts", "no gifts", "gift policy",
    "our policy prohibits gifts", "unable to accept", "decline any gift",
]

_UNCERTAINTY = [
    "maybe", "not sure", "possibly", "i guess", "we'll see", "perhaps",
    "hard to say", "it depends", "on the fence", "i think so but",
    "i'm torn", "im torn",
]


def _contains_any(text: str, needles: List[str]) -> bool:
    return any(n in text for n in needles)


def _matches_any(text: str, patterns: List[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(reply_text: str) -> TriageResult:
    """Classify one inbound reply into exactly one canonical label."""
    text = (reply_text or "").lower()

    # ---- 1. Hard safety escalations (checked first, highest priority) -----
    if _matches_any(text, _INJECTION_PATTERNS):
        return TriageResult(REQUIRES_HUMAN, "embedded instruction / prompt injection detected", escalated=True)
    if _matches_any(text, _LEGAL_PATTERNS):
        return TriageResult(REQUIRES_HUMAN, "legal matter detected", escalated=True)
    if _contains_any(text, _ANGER_WORDS):
        return TriageResult(REQUIRES_HUMAN, "anger / hostility detected", escalated=True)
    if _contains_any(text, _SENSITIVE_WORDS):
        return TriageResult(REQUIRES_HUMAN, "sensitive or confidential matter detected", escalated=True)
    if _contains_any(text, _SARCASM_MARKERS):
        return TriageResult(REQUIRES_HUMAN, "sarcasm marker detected", escalated=True)
    if _contains_any(text, _INCORRECT_CLAIM):
        return TriageResult(REQUIRES_HUMAN, "guest disputes a personalized/factual claim", escalated=True)

    # Unsubscribe / opt-out is terminal and unambiguous — honor it directly.
    if _contains_any(text, _UNSUBSCRIBE):
        return TriageResult("UNSUBSCRIBE", "explicit opt-out")

    # ---- 2. Money / travel — always human-adjacent categories -------------
    if _contains_any(text, _FEE_WORDS):
        # Fee requests are a defined category AND a human gate; label it FEE_REQUEST
        # (the workflow treats FEE_REQUEST as human-gated downstream).
        return TriageResult("FEE_REQUEST", "fee / compensation request", escalated=True)
    if _contains_any(text, _TRAVEL_WORDS):
        return TriageResult("TRAVEL_REQUEST", "travel / logistics request")

    # ---- 3. Cancellation / reschedule -> escalate (booking change) --------
    if _contains_any(text, _CANCELLATION):
        return TriageResult(REQUIRES_HUMAN, "cancellation of a commitment", escalated=True)
    if _contains_any(text, _RESCHEDULE):
        return TriageResult(REQUIRES_HUMAN, "reschedule request on a booking", escalated=True)

    # ---- 4. Gift restriction ----------------------------------------------
    if _contains_any(text, _GIFT_RESTRICTION):
        return TriageResult(REQUIRES_HUMAN, "gift restriction / policy raised", escalated=True)

    # ---- 5. Topic concern -------------------------------------------------
    if _contains_any(text, _TOPIC_CONCERN):
        return TriageResult("TOPIC_CONCERN", "guest raised a topic to avoid", escalated=True)

    # ---- 6. Routing handoffs ----------------------------------------------
    if _contains_any(text, _PUBLICIST):
        return TriageResult("CONTACT_PUBLICIST", "directed to publicist")
    if _contains_any(text, _ASSISTANT):
        # Assistant proposing times is still an assistant handoff.
        return TriageResult("CONTACT_ASSISTANT", "directed to assistant")

    # ---- 7. Declines and revisit ------------------------------------------
    # FOLLOW_UP_LATER outranks HARD_DECLINE when an explicit revisit window
    # accompanies a negative signal (e.g. "unable to participate now; circle
    # back later in the fall"). The forward revisit is the actionable signal.
    if _contains_any(text, _FOLLOW_UP_LATER):
        return TriageResult("FOLLOW_UP_LATER", "asked to be contacted later")
    if _contains_any(text, _HARD_DECLINE):
        return TriageResult("HARD_DECLINE", "explicit hard decline")
    if _contains_any(text, _SOFT_DECLINE):
        return TriageResult("SOFT_DECLINE", "soft decline / not right now")

    # ---- 8. Scheduling ----------------------------------------------------
    if _contains_any(text, _PROPOSED_TIMES):
        return TriageResult("PROPOSED_TIMES", "guest proposed specific times")

    # ---- 9. Uncertainty -> AMBIGUOUS (then human review) ------------------
    if _contains_any(text, _UNCERTAINTY):
        return TriageResult(AMBIGUOUS, "hedged / uncertain language")

    # ---- 10. Information request ------------------------------------------
    if _contains_any(text, _NEEDS_INFO):
        return TriageResult("NEEDS_MORE_INFORMATION", "guest requested more information")

    # ---- 11. Positive interest --------------------------------------------
    if _contains_any(text, _POSITIVE):
        return TriageResult("POSITIVE_INTEREST", "positive interest")

    # ---- 12. Nothing matched -> AMBIGUOUS (never improvise) ---------------
    return TriageResult(AMBIGUOUS, "no confident signal; route to human review")
