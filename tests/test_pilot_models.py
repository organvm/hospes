"""Validation and privacy tests for Pilot policy and decision inputs."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hospes.pilot_models import PilotDecisionInput, PilotError, PilotPolicyInput, opaque_id


POLICY = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "pilot_policies"
    / "example-partnership-pilot-1.yaml"
)


def test_pilot_one_policy_is_stable_and_contextual() -> None:
    source = yaml.safe_load(POLICY.read_text())
    policy = PilotPolicyInput.from_mapping(source)
    repeated = PilotPolicyInput.from_mapping(source)

    assert policy.policy_key == "example_partnership.pilot_1"
    assert policy.deadline_at.isoformat() == "2026-08-06T06:59:59+00:00"
    assert policy.timezone_name == "America/Los_Angeles"
    assert policy.allowed_relationship_classes == ("C2", "C3")
    assert policy.max_social_cost == policy.relationship_exposure_budget == 2
    assert policy.follow_up_limit == 1
    assert policy.human_authority_rules["no_autonomous_sending"] is True
    assert policy.digest == repeated.digest
    assert len(policy.digest) == 64


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("policy", "candidate_eligibility", "city"), "partner@example.org"),
        (("policy", "candidate_eligibility", "network_id"), "+1 310-555-0199"),
        (("policy", "human_authority_rules", "unexpected"), "copied body"),
    ],
)
def test_policy_rejects_private_or_unversioned_fields(
    path: tuple[str, ...], value: str
) -> None:
    source = yaml.safe_load(POLICY.read_text())
    target = source
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(PilotError):
        PilotPolicyInput.from_mapping(source)


def test_decision_input_is_revisioned_and_body_free() -> None:
    decision = PilotDecisionInput.from_mapping({
        "decision_kind": "activate_set",
        "expected_revision": 3,
        "assignments": [
            {
                "assignment_id": "assignment_one",
                "not_before": "2026-07-23T12:00:00-07:00",
            },
            {
                "assignment_id": "assignment_two",
                "not_before": "2026-07-24T12:00:00-07:00",
            },
        ],
    })

    assert decision.expected_revision == 3
    assert decision.payload["assignments"][0]["not_before"].endswith("+00:00")

    with pytest.raises(PilotError, match="unsupported fields"):
        PilotDecisionInput.from_mapping({
            "decision_kind": "wait",
            "expected_revision": 3,
            "message_body": "Hello, this must never be stored.",
        })


def test_internal_uuid_is_not_mistaken_for_contact_data() -> None:
    value = "1f11af53-3653-4786-8493-482bf9340df7"

    assert opaque_id(value, "assignment_id") == value
    with pytest.raises(PilotError):
        opaque_id("3105550199", "assignment_id")
