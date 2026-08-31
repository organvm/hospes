"""The canonical guest-lifecycle state machine.

Loads ``spec/states.json`` (treated as canon) and enforces legal transitions.
An AppearanceOpportunity has exactly one state at any time. ``advance`` moves
an opportunity to a target state only if the transition is legal per the
spec's ``transitions`` map; an illegal move raises :class:`HospesStateError`.

The transition target may be given either as a target *state* name
(``"RESEARCHING"``) or as an *event* name from ``spec/events.json``
(``"appearance.researching"``); events are mapped to their resulting state.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Dict, List, Mapping, MutableMapping, Optional

from .paths import SPEC_DIR


class HospesStateError(ValueError):
    """Raised when an illegal state transition is attempted."""


# Map of canonical event name -> resulting state. Derived from the semantics
# in spec/events.json + spec/states.json (events drive transitions). Only the
# events that unambiguously land an opportunity in a specific state are mapped;
# anything else must be expressed as a target state name directly.
_EVENT_TO_STATE: Dict[str, str] = {
    "appearance.candidate_created": "DISCOVERED",
    "candidate.created": "DISCOVERED",
    "appearance.researching": "RESEARCHING",
    "appearance.qualified": "QUALIFIED",
    "candidate.thesis_ready": "QUALIFIED",
    "appearance.editorial_review": "EDITORIAL_REVIEW",
    "candidate.awaiting_host_approval": "EDITORIAL_REVIEW",
    "appearance.approved": "APPROVED",
    "candidate.approved": "APPROVED",
    "appearance.rejected": "DECLINED",
    "candidate.rejected": "DECLINED",
    "appearance.duplicate_detected": "DUPLICATE",
    "contact_route.identified": "CONTACT_ROUTE_IDENTIFIED",
    "outreach.draft_ready": "OUTREACH_DRAFTED",
    "outreach.approved": "OUTREACH_APPROVED",
    "outreach.human_approved": "OUTREACH_APPROVED",
    "outreach.sent": "OUTREACH_SENT",
    "followup.due": "FOLLOW_UP_DUE",
    "guest.followup_due": "FOLLOW_UP_DUE",
    "booking.interested": "INTERESTED",
    "booking.negotiating": "NEGOTIATING",
    "booking.proposed": "SCHEDULING",
    "booking.confirmed": "BOOKED",
    "booking.cancelled": "CANCELLED",
    "guest_intake.completed": "INTAKE_PENDING",
    "guest.intake_complete": "INTAKE_PENDING",
    "preinterview.completed": "PREINTERVIEW_PENDING",
    "recording.ready": "RECORDING_READY",
    "recording.completed": "RECORDED",
    "recording.complete": "RECORDED",
    "transcript.ready": "TRANSCRIPT_READY",
    "episode.published": "PUBLISHED",
    "relationship.nurture_due": "RELATIONSHIP_NURTURE",
}


@lru_cache(maxsize=1)
def load_states(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and cache the canonical states spec."""
    spec_path = SPEC_DIR / "states.json" if path is None else path
    with open(spec_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def all_states() -> List[str]:
    """Every legal state name (main + branch)."""
    spec = load_states()
    names = [s["state"] for s in spec["main_states"]]
    names += [s["state"] for s in spec["branch_states"]]
    return names


def transitions() -> Mapping[str, List[str]]:
    """The legal transition map: state -> list of legal next states."""
    return load_states()["transitions"]


def is_valid_state(state: str) -> bool:
    return state in all_states()


def resolve_target(event_or_state: str) -> str:
    """Resolve an event name or a bare state name to a target state.

    A bare, valid state name resolves to itself. A known event name resolves to
    its terminal state. Anything else raises :class:`HospesStateError`.
    """
    if is_valid_state(event_or_state):
        return event_or_state
    if event_or_state in _EVENT_TO_STATE:
        return _EVENT_TO_STATE[event_or_state]
    raise HospesStateError(
        f"Unknown event or state: {event_or_state!r}. "
        f"Not a canonical state and not a mappable event."
    )


def legal_next_states(state: str) -> List[str]:
    if not is_valid_state(state):
        raise HospesStateError(f"Unknown state: {state!r}")
    return list(transitions().get(state, []))


def can_advance(current_state: str, event_or_state: str) -> bool:
    """True iff advancing from ``current_state`` via ``event_or_state`` is legal."""
    try:
        target = resolve_target(event_or_state)
    except HospesStateError:
        return False
    return target in transitions().get(current_state, [])


def advance(opportunity: MutableMapping[str, Any], event_or_state: str) -> MutableMapping[str, Any]:
    """Advance an opportunity to a new state, enforcing legal transitions.

    ``opportunity`` is a mapping carrying at least a ``state`` key. The target
    is resolved from ``event_or_state`` (an event name or a state name). On a
    legal transition the opportunity's ``state`` is updated in place and the
    same mapping is returned. An illegal transition raises
    :class:`HospesStateError` and leaves the opportunity untouched.
    """
    current = opportunity.get("state")
    if current is None:
        raise HospesStateError("Opportunity has no 'state' field.")
    if not is_valid_state(current):
        raise HospesStateError(f"Opportunity is in an unknown state: {current!r}")

    target = resolve_target(event_or_state)
    allowed = transitions().get(current, [])
    if target not in allowed:
        raise HospesStateError(
            f"Illegal transition {current!r} -> {target!r} "
            f"(via {event_or_state!r}). Legal next states: {sorted(allowed)}"
        )
    opportunity["state"] = target
    return opportunity
