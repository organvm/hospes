"""Temporal safety — verify that pilot policy tests never depend on wall-clock time."""

from __future__ import annotations

from pathlib import Path

import yaml

from hospes.pilot_models import PilotPolicyInput, aware_datetime


ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "config" / "pilot_policies"


def test_all_policies_parse_without_temporal_assertion() -> None:
    """Every tracked policy file must parse without error regardless of clock."""
    for path in sorted(POLICY_DIR.glob("*.yaml")):
        source = yaml.safe_load(path.read_text())
        policy = PilotPolicyInput.from_mapping(source)
        assert policy.policy_key, f"{path.name} missing policy_key"
        assert policy.digest, f"{path.name} missing digest"


def test_policy_deadline_is_deterministic() -> None:
    """The config deadline must be parseable and timezone-aware."""
    for path in sorted(POLICY_DIR.glob("*.yaml")):
        source = yaml.safe_load(path.read_text())
        raw = source["policy"]["deadline_at"]
        parsed = aware_datetime(str(raw), "deadline_at")
        assert parsed.tzinfo is not None, f"{path.name} deadline has no timezone"


def test_prepared_run_never_uses_wall_clock() -> None:
    """Import guard: test_pilot_service.prepared_run must not call datetime.now."""
    import inspect
    from tests.test_pilot_service import prepared_run
    source = inspect.getsource(prepared_run)
    assert "datetime.now" not in source, (
        "prepared_run must use SIMULATED_NOW, not datetime.now"
    )
