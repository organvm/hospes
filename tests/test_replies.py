"""Evaluation corpus tests for the HOSPES triage baseline classifier.

This suite runs every case in ``tests/fixtures/reply_cases.yaml`` through
``hospes.triage.classify``.  It is an *additional* corpus beyond the 16-fixture
suite in ``test_triage.py``; both suites must pass together.

Labels in the fixture are mapped onto ``hospes.triage.LABELS`` — the canonical
controlled set.  Each case's ``note`` field records the mapping rationale where
the PR #1 taxonomy (accept/decline/revisit/representative/scheduling/boundary/
fee/legal/sensitive/unknown) differs from ours.

No real guest, company, representative, contact detail, or private relationship
appears anywhere in this file or the fixture YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hospes import triage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reply_cases.yaml"
_DOC = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = _DOC["cases"]

# Every expected_label in the fixture must be in our controlled set.
_ALL_EXPECTED_LABELS = {c["expected_label"] for c in CASES}


def test_fixture_schema_version() -> None:
    assert _DOC["schema_version"] == 1


def test_fixture_has_at_least_22_cases() -> None:
    """The PR #1 corpus contributed 22 cases; this floor preserves them."""
    assert len(CASES) >= 22


def test_fixture_case_ids_are_unique() -> None:
    ids = [c["id"] for c in CASES]
    assert len(set(ids)) == len(ids)


def test_all_cases_are_synthetic() -> None:
    assert all(c.get("synthetic") is True for c in CASES)


def test_all_expected_labels_are_in_controlled_set() -> None:
    """Every label in the fixture must be a member of triage.LABELS."""
    unknown = _ALL_EXPECTED_LABELS - set(triage.LABELS)
    assert not unknown, f"Fixture uses labels outside triage.LABELS: {unknown}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_reply_corpus_case(case: dict) -> None:
    """Each corpus case must classify to the remapped expected_label."""
    result = triage.classify(str(case["text"]))
    assert result.label == case["expected_label"], (
        f"[{case['id']}] got label={result.label!r} (reason={result.reason!r}); "
        f"expected={case['expected_label']!r}. "
        f"note: {case.get('note', '')}"
    )
    assert result.escalated is case["escalated"], (
        f"[{case['id']}] got escalated={result.escalated}; "
        f"expected escalated={case['escalated']}"
    )
    assert result.label in triage.LABELS


def test_boundary_cases_escalate_or_route_to_human() -> None:
    """All boundary/legal/sensitive/fee/injection cases must be escalated or REQUIRES_HUMAN."""
    boundary_ids = {
        "boundary_do_not_contact",
        "boundary_unsubscribe",
        "legal_contract",
        "sensitive_confidential",
        "sensitive_instruction_injection",
        "fee_appearance",
        "fee_competing_accept",
        "unknown_sarcasm",
    }
    for case in CASES:
        if case["id"] not in boundary_ids:
            continue
        result = triage.classify(str(case["text"]))
        assert result.label in {"REQUIRES_HUMAN", "UNSUBSCRIBE", "FEE_REQUEST"} or result.escalated, (
            f"[{case['id']}] safety case should escalate or route to human; "
            f"got label={result.label!r} escalated={result.escalated}"
        )


def test_revisit_outranks_decline_when_explicit_window_given() -> None:
    """FOLLOW_UP_LATER wins over HARD_DECLINE when both signals are present."""
    result = triage.classify(
        "I am unable to participate now; please circle back later in the fall."
    )
    assert result.label == "FOLLOW_UP_LATER", (
        f"Revisit window should outrank decline; got {result.label!r}"
    )


def test_scheduling_terms_route_to_proposed_times() -> None:
    """'availability', 'timezone', 'time options' all route to PROPOSED_TIMES."""
    scheduling_cases = [
        "I am available next week; please send some time options.",
        "Could you send dates and include the timezone for each window?",
    ]
    for text in scheduling_cases:
        result = triage.classify(text)
        assert result.label == "PROPOSED_TIMES", (
            f"Scheduling phrase should route to PROPOSED_TIMES; "
            f"text={text!r} got {result.label!r}"
        )


def test_manager_routes_to_contact_publicist() -> None:
    """'my manager' (representative handoff) routes to CONTACT_PUBLICIST."""
    result = triage.classify(
        "Please coordinate with my manager to schedule available dates next month."
    )
    assert result.label == "CONTACT_PUBLICIST", (
        f"Manager handoff should route to CONTACT_PUBLICIST; got {result.label!r}"
    )


def test_legal_word_boundary_prevents_false_positive() -> None:
    """'standard' and 'calendar' must not trigger legal detection ('nda' substring fix)."""
    texts_with_no_legal = [
        "Our standard appearance fee would apply to this recording.",
        "Oh sure, because my calendar clearly needs another mystery commitment.",
    ]
    for text in texts_with_no_legal:
        result = triage.classify(text)
        assert result.label != "REQUIRES_HUMAN" or result.reason != "legal matter detected", (
            f"False-positive legal hit on {text!r}; got reason={result.reason!r}"
        )


def test_sensitive_confidential_routes_to_requires_human() -> None:
    """'confidential' and 'off-the-record' must route to REQUIRES_HUMAN."""
    result = triage.classify(
        "That subject is confidential and should remain off-the-record."
    )
    assert result.label == "REQUIRES_HUMAN"
    assert result.escalated
