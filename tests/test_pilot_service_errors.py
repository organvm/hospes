"""Deterministic coverage for every Pilot service rejection boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hospes import partnerships, pilot_service, store
from hospes.pilot_models import PilotError
from hospes.pilot_planner import (
    AVAILABLE,
    FOLLOW_UP_DUE,
    PROMOTION_DUE,
    REPLIED,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "partnerships" / "example-partnership-private-pilot.yaml"
POLICY_PATH = ROOT / "config" / "pilot_policies" / "example-partnership-pilot-1.yaml"
NOW = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)
TENANT = "private_pilot"
PARTNERSHIP_ID = "partnership-fixture"
RUN_ID = "run-fixture"


def imported_partnership(tmp_path: Path):
    connection = store.connect(tmp_path / "pilot-errors.sqlite3")
    imported = partnerships.import_template(
        connection,
        TEMPLATE,
        tenant_id=TENANT,
        actor_id="producer_fixture",
        actor_role="producer",
        show_id="private_pilot",
    )
    return connection, imported.partnership_id


def future_policy(**overrides):
    policy = {
        "id": "policy-fixture",
        "deadline_at": (NOW + timedelta(days=10)).isoformat(),
        "candidate_count": 1,
        "candidate_network_id": "fixture_network",
        "candidate_city": "Los Angeles",
        "allowed_relationship_classes": ["C2"],
        "max_social_cost": 2,
        "relationship_exposure_budget": 2,
        "initial_response_hours": 24,
        "follow_up_response_hours": 24,
        "follow_up_limit": 1,
        "production_gate_durations": {"brief": 1},
        "human_authority_rules": {"decision_roles": ["relationship_owner", "producer"]},
    }
    policy.update(overrides)
    return policy


def assignment(identifier: str = "assignment-a", **overrides):
    value = {
        "id": identifier,
        "active_state": AVAILABLE,
        "relationship_class": "C2",
        "social_cost": 1,
        "route_id": f"route-{identifier}",
        "owner_id": f"owner-{identifier}",
        "follow_ups_sent": 0,
        "opportunity_id": f"opportunity-{identifier}",
    }
    value.update(overrides)
    return value


def fake_decision(kind: str, payload: dict, revision: int = 1):
    return SimpleNamespace(
        decision_kind=kind,
        payload=payload,
        expected_revision=revision,
    )


def prepare_record_decision(
    monkeypatch,
    *,
    lifecycle: str = "ACTIVE",
    assignments: list[dict] | None = None,
    policy: dict | None = None,
) -> None:
    run = {
        "id": RUN_ID,
        "policy_id": "policy-fixture",
        "revision": 1,
        "lifecycle_state": lifecycle,
    }
    monkeypatch.setattr(pilot_service, "_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(pilot_service, "_policy", lambda *_args, **_kwargs: policy or future_policy())
    monkeypatch.setattr(
        pilot_service,
        "_advance_expired_windows",
        lambda _conn, current_run, *_args, **_kwargs: current_run,
    )
    monkeypatch.setattr(
        pilot_service,
        "_assignments",
        lambda *_args, **_kwargs: assignments or [],
    )


def assert_record_error(monkeypatch, decision, message: str, **setup) -> None:
    prepare_record_decision(monkeypatch, **setup)
    with pytest.raises(PilotError, match=message):
        pilot_service.record_decision(
            None,
            PARTNERSHIP_ID,
            RUN_ID,
            decision,
            tenant_id=TENANT,
            actor_id="ari_owner",
            actor_role="relationship_owner",
            now=NOW,
        )


def test_import_policy_rejects_unreadable_file(tmp_path: Path) -> None:
    connection, partnership_id = imported_partnership(tmp_path)
    with pytest.raises(PilotError, match="unreadable"):
        pilot_service.import_policy(
            connection,
            partnership_id,
            tmp_path / "missing.yaml",
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    connection.close()


def test_import_policy_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    connection, partnership_id = imported_partnership(tmp_path)
    policy_path = tmp_path / "list.yaml"
    policy_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(PilotError, match="must be an object"):
        pilot_service.import_policy(
            connection,
            partnership_id,
            policy_path,
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    connection.close()


def test_import_policy_rejects_conflicting_digest(tmp_path: Path) -> None:
    connection, partnership_id = imported_partnership(tmp_path)
    pilot_service.import_policy(
        connection,
        partnership_id,
        POLICY_PATH,
        tenant_id=TENANT,
        actor_id="producer_fixture",
        actor_role="producer",
        now=NOW,
    )
    document = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    document["policy"]["deadline_at"] = "2026-08-04T00:00:00+00:00"
    conflicting = tmp_path / "conflicting.yaml"
    conflicting.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(PilotError, match="different digest"):
        pilot_service.import_policy(
            connection,
            partnership_id,
            conflicting,
            tenant_id=TENANT,
            actor_id="producer_fixture",
            actor_role="producer",
            now=NOW,
        )
    connection.close()


def test_policy_lookup_rejects_missing_policy(tmp_path: Path) -> None:
    connection = store.connect(tmp_path / "missing-policy.sqlite3")
    with pytest.raises(PilotError, match="pilot policy not found"):
        pilot_service._policy(connection, "missing", "missing", TENANT)
    connection.close()


def test_run_lookup_rejects_missing_run(tmp_path: Path) -> None:
    connection = store.connect(tmp_path / "missing-run.sqlite3")
    with pytest.raises(PilotError, match="pilot run not found"):
        pilot_service._run(connection, "missing", "missing", TENANT)
    connection.close()


@pytest.mark.parametrize(
    ("payload", "message"),
    [({}, "requires exactly policy_id"), ({"policy_id": 7}, "opaque internal id")],
)
def test_start_run_validates_payload_before_policy_lookup(monkeypatch, payload: dict, message: str) -> None:
    monkeypatch.setattr(pilot_service, "_partnership", lambda *_args: {})
    with pytest.raises(PilotError, match=message):
        pilot_service.start_run(
            None,
            PARTNERSHIP_ID,
            payload,
            tenant_id=TENANT,
            actor_id="ari_owner",
            actor_role="relationship_owner",
            now=NOW,
        )


def prepare_start(monkeypatch, *, policy=None, open_run=None, ready=True, slots=None):
    monkeypatch.setattr(pilot_service, "_partnership", lambda *_args: {"show_id": "private_pilot"})
    monkeypatch.setattr(pilot_service, "_policy", lambda *_args: policy or future_policy())
    monkeypatch.setattr(
        pilot_service.store,
        "fetch_one",
        lambda *_args, **_kwargs: open_run,
    )
    monkeypatch.setattr(
        pilot_service.partnerships,
        "command_center",
        lambda *_args, **_kwargs: {
            "pilot_readiness": {"checks": [{"key": f"gate-{index}", "met": ready} for index in range(4)]}
        },
    )
    monkeypatch.setattr(
        pilot_service.store,
        "fetch_all",
        lambda *_args, **_kwargs: slots or [],
    )


def start(monkeypatch, **setup) -> None:
    prepare_start(monkeypatch, **setup)
    pilot_service.start_run(
        None,
        PARTNERSHIP_ID,
        {"policy_id": "policy-fixture"},
        tenant_id=TENANT,
        actor_id="ari_owner",
        actor_role="relationship_owner",
        now=NOW,
    )


def test_start_run_rejects_elapsed_deadline(monkeypatch) -> None:
    with pytest.raises(PilotError, match="deadline has elapsed"):
        start(
            monkeypatch,
            policy=future_policy(deadline_at=(NOW - timedelta(seconds=1)).isoformat()),
        )


def test_start_run_rejects_an_existing_open_run(monkeypatch) -> None:
    with pytest.raises(PilotError, match="already has an open"):
        start(monkeypatch, open_run={"id": "existing"})


def test_start_run_rejects_incomplete_prerequisites(monkeypatch) -> None:
    with pytest.raises(PilotError, match="prerequisites are incomplete"):
        start(monkeypatch, ready=False)


def test_start_run_rejects_slot_count_mismatch(monkeypatch) -> None:
    with pytest.raises(PilotError, match="slate does not match"):
        start(monkeypatch)


def test_start_run_rejects_policy_violating_slot(monkeypatch) -> None:
    bad_slot = {
        "slot": 1,
        "network_id": "wrong_network",
        "preferred_city": "Los Angeles",
        "relationship_class": "C2",
        "social_cost_1_5": 1,
        "relationship_owner": "owner-a",
        "route_usable": True,
        "route_provenance": "fixture://route/a",
        "route_verified_at": NOW.isoformat(),
    }
    with pytest.raises(PilotError, match="violates the selected policy"):
        start(monkeypatch, slots=[bad_slot])


def test_activation_rejects_unknown_or_nonavailable_assignment() -> None:
    with pytest.raises(PilotError, match="non-available"):
        pilot_service._validate_activation(
            future_policy(),
            [assignment(active_state=REPLIED)],
            [{"assignment_id": "assignment-a", "not_before": NOW.isoformat()}],
            now=NOW,
        )


def test_activation_rejects_eligibility_or_exposure_violation() -> None:
    with pytest.raises(PilotError, match="eligibility or exposure"):
        pilot_service._validate_activation(
            future_policy(),
            [assignment(social_cost=3)],
            [{"assignment_id": "assignment-a", "not_before": NOW.isoformat()}],
            now=NOW,
        )


def test_activation_rejects_past_start() -> None:
    with pytest.raises(PilotError, match="cannot be in the past"):
        pilot_service._validate_activation(
            future_policy(),
            [assignment()],
            [
                {
                    "assignment_id": "assignment-a",
                    "not_before": (NOW - timedelta(hours=1)).isoformat(),
                }
            ],
            now=NOW,
        )


def test_activation_rejects_route_or_owner_shared_with_active_ask() -> None:
    available = assignment()
    active = assignment(
        "assignment-b",
        active_state=FOLLOW_UP_DUE,
        route_id=available["route_id"],
    )
    with pytest.raises(PilotError, match="multiple active asks"):
        pilot_service._validate_activation(
            future_policy(),
            [available, active],
            [{"assignment_id": available["id"], "not_before": NOW.isoformat()}],
            now=NOW,
        )


def test_activation_rejects_window_that_cannot_finish_before_production() -> None:
    with pytest.raises(PilotError, match="production dependencies"):
        pilot_service._validate_activation(
            future_policy(deadline_at=(NOW + timedelta(hours=1)).isoformat()),
            [assignment()],
            [{"assignment_id": "assignment-a", "not_before": NOW.isoformat()}],
            now=NOW,
        )


def test_activation_rejects_overlapping_choices_with_shared_owner() -> None:
    left = assignment("assignment-left", owner_id="shared-owner")
    right = assignment("assignment-right", owner_id="shared-owner")
    with pytest.raises(PilotError, match="overlapping activations"):
        pilot_service._validate_activation(
            future_policy(),
            [left, right],
            [
                {"assignment_id": left["id"], "not_before": NOW.isoformat()},
                {"assignment_id": right["id"], "not_before": NOW.isoformat()},
            ],
            now=NOW,
        )


def test_record_decision_rejects_terminal_run(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision("wait", {}),
        "no longer accepts",
        lifecycle="COMPLETED",
    )


def test_record_decision_freezes_outreach_after_terminal_evidence(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision("follow_up", {"assignment_id": "assignment-a"}),
        "freezes this action",
        assignments=[assignment(active_state=REPLIED)],
    )


def test_record_decision_rejects_activation_while_paused(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision("activate_set", {"assignments": []}),
        "paused run cannot activate",
        lifecycle="PAUSED",
    )


def test_record_decision_requires_follow_up_due_assignment(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision("follow_up", {"assignment_id": "missing"}),
        "requires an assignment in FOLLOW_UP_DUE",
    )


def test_record_decision_enforces_follow_up_ceiling(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision("follow_up", {"assignment_id": "assignment-a"}),
        "ceiling is exhausted",
        assignments=[assignment(active_state=FOLLOW_UP_DUE, follow_ups_sent=1)],
    )


def test_record_decision_requires_exhausted_promotion_source(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision(
            "promote",
            {
                "exhausted_assignment_id": "missing",
                "promoted_assignment_id": "assignment-a",
                "not_before": NOW.isoformat(),
            },
        ),
        "requires an exhausted PROMOTION_DUE",
        assignments=[assignment()],
    )


def test_record_decision_requires_available_promotion_target(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision(
            "promote",
            {
                "exhausted_assignment_id": "assignment-a",
                "promoted_assignment_id": "assignment-b",
                "not_before": NOW.isoformat(),
            },
        ),
        "target must be an unused AVAILABLE",
        assignments=[
            assignment(active_state=PROMOTION_DUE),
            assignment("assignment-b", active_state="PAUSED"),
        ],
    )


def test_record_decision_rejects_unsupported_kind(monkeypatch) -> None:
    assert_record_error(
        monkeypatch,
        fake_decision("unsupported", {}),
        "unsupported pilot decision",
    )


def evidence_assignment(*, lifecycle: str = "ACTIVE", state: str = AVAILABLE):
    return {
        **assignment(active_state=state),
        "pilot_run_id": RUN_ID,
        "partnership_id": PARTNERSHIP_ID,
        "policy_id": "policy-fixture",
        "revision": 1,
        "deadline_at": (NOW + timedelta(days=10)).isoformat(),
        "lifecycle_state": lifecycle,
        "run_tenant_id": TENANT,
        "created_by": "producer_fixture",
        "created_role": "producer",
        "run_created_at": NOW.isoformat(),
        "run_updated_at": NOW.isoformat(),
    }


def assert_evidence_error(monkeypatch, evidence, kind: str, message: str) -> None:
    monkeypatch.setattr(pilot_service.store, "fetch_one", lambda *_args, **_kwargs: evidence)
    monkeypatch.setattr(pilot_service, "_policy", lambda *_args, **_kwargs: future_policy())
    with pytest.raises(PilotError, match=message):
        pilot_service.apply_opportunity_evidence(
            None,
            "opportunity-fixture",
            evidence_kind=kind,
            evidence_reference="fixture://pilot/evidence",
            actor_id="producer_fixture",
            actor_role="producer",
            occurred_at=NOW,
        )


def test_evidence_rejects_nonproduction_event_while_paused(monkeypatch) -> None:
    assert_evidence_error(
        monkeypatch,
        evidence_assignment(lifecycle="PAUSED"),
        "outreach.sent",
        "incompatible with the current pilot lifecycle",
    )


def test_evidence_rejects_outreach_in_wrong_assignment_state(monkeypatch) -> None:
    assert_evidence_error(
        monkeypatch,
        evidence_assignment(),
        "outreach.sent",
        "outreach receipt is incompatible",
    )


def test_evidence_rejects_reply_in_wrong_assignment_state(monkeypatch) -> None:
    assert_evidence_error(
        monkeypatch,
        evidence_assignment(),
        "reply.classified",
        "reply evidence is incompatible",
    )
