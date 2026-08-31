"""Predicates for the deferred review-finding registry and its `done.sh` gate.

Two things are under test, and the second matters as much as the first:

  1. The registry loads and holds the campaign it claims to hold.
  2. The gate ACTUALLY REDS. A gate never observed failing is not known to
     work, and this one is wired into `done.sh` in a state where it is green on
     day one (the ratchet records the current backlog). Without the failure
     tests below, nothing would distinguish "the gate passes" from "the gate is
     inert" until the first regression slipped through it unremarked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from hospes import completion_registry, hardening_registry

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_hardening.py"


def _raw_registry() -> dict:
    return yaml.safe_load(
        files("hospes.resources").joinpath("spec/hardening-registry.yaml").read_text(encoding="utf-8")
    )


def _write(tmp_path: Path, name: str, raw: object) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _assert_invalid(tmp_path: Path, name: str, raw: object, match: str) -> None:
    with pytest.raises(hardening_registry.HardeningRegistryError, match=match):
        hardening_registry.load_registry(_write(tmp_path, name, raw))


def _find(raw: dict, identifier: str) -> dict:
    for item in raw["findings"]:
        if item["id"] == identifier:
            return item
    raise AssertionError(f"{identifier} not in registry")


# --------------------------------------------------------------------------
# The registry holds the campaign it claims to hold
# --------------------------------------------------------------------------


def test_registry_covers_every_deferred_issue() -> None:
    registry = hardening_registry.load_registry()
    assert {item.issue for item in registry.findings} == hardening_registry.EXPECTED_ISSUES
    # Eleven consolidated hardening issues, 109 findings deferred from the 1.0 campaign.
    assert len(registry.findings) == 109
    assert len(registry.for_issue(81)) == 20  # the portal, highest P1 density
    assert len(registry.for_issue(72)) == 5


def test_receipt_owner_is_derived_not_stored() -> None:
    registry = hardening_registry.load_registry()
    for finding in registry.findings:
        assert finding.receipt_owner == f"github://organvm/hospes/issues/{finding.issue}"


def test_every_finding_carries_an_executable_close_condition() -> None:
    registry = hardening_registry.load_registry()
    for finding in registry.findings:
        assert finding.close_condition.strip()
        assert finding.status == "open"  # nothing is closed yet; this is the day-one state


def test_severity_reanchoring_is_recorded_not_silent() -> None:
    """The P-label is never overwritten, so the re-ranking stays auditable."""
    registry = hardening_registry.load_registry()
    reranked = [item for item in registry.findings if item.reranked]
    # The backlog's labels disagree with a uniform rubric on a large minority of
    # rows. The exact figure is asserted so a silent drift in either direction
    # shows up as a test failure rather than as a quietly different campaign.
    assert len(reranked) == 45

    # The two findings the plan names as the clearest mis-anchorings: both
    # arrived P2 and both are exploitable.
    assert _severity(registry, "H-80.11") == "S1"  # path traversal
    assert _severity(registry, "H-81.13") == "S1"  # consent-form clickjacking
    assert registry.finding("H-80.11").priority == "P2"
    assert registry.finding("H-81.13").priority == "P2"

    # And the one that arrived P1 while being a declaration chore.
    assert registry.finding("H-77.3").priority == "P1"
    assert _severity(registry, "H-77.3") == "S3"


def _severity(registry, identifier: str) -> str:
    return registry.finding(identifier).severity


def test_portal_entry_point_is_marked_first_in_its_track() -> None:
    """H-81.6 blocks runtime verification of the other 19 portal findings."""
    registry = hardening_registry.load_registry()
    finding = registry.finding("H-81.6")
    assert finding.theme == "portal-entry-point"
    assert finding.severity == "S1"


def test_role_model_contradiction_is_decided_and_blocked_on_phase_one() -> None:
    """#73.9 wants `host` admitted, #81.2 wants `host` refused — C-4 resolves it."""
    registry = hardening_registry.load_registry()
    contradiction = next(item for item in registry.contradictions if item.id == "C-4")
    assert set(contradiction.between) == {"H-73.9", "H-81.2"}
    assert "capability" in contradiction.resolution
    for identifier in ("H-73.9", "H-81.2"):
        assert registry.finding(identifier).blocked_by_phase == 1


def test_false_closure_pair_must_land_together() -> None:
    """H-70.3 without H-70.5 restores a fail-closed gate that still passes evidence-less rows."""
    registry = hardening_registry.load_registry()
    contradiction = next(item for item in registry.contradictions if item.id == "C-5")
    assert contradiction.landing_constraint == "single-pr"
    assert registry.finding("H-70.3").landing_constraint == "single-pr-with-H-70.5"
    assert registry.finding("H-70.5").landing_constraint == "single-pr-with-H-70.3"


# --------------------------------------------------------------------------
# There is no field to lie in
# --------------------------------------------------------------------------


def test_registry_rejects_any_field_that_could_carry_a_distance(tmp_path: Path) -> None:
    """The finding schema is closed, so a stored count cannot be introduced at all.

    This is the executable form of "a row may not carry a distance in the
    registry — there is no field to lie in". Prose could not enforce it; a
    closed key set can.
    """
    for smuggled in ("open_count", "percent_complete", "distance", "progress"):
        raw = deepcopy(_raw_registry())
        raw["findings"][0][smuggled] = 12
        _assert_invalid(tmp_path, f"closed-{smuggled}", raw, "unknown keys")


def test_absence_must_be_named_in_both_directions(tmp_path: Path) -> None:
    raw = deepcopy(_raw_registry())
    target = _find(raw, "H-70.1")
    target["files"] = []
    _assert_invalid(tmp_path, "empty-files", raw, "not marked unattributed")

    raw = deepcopy(_raw_registry())
    target = _find(raw, "H-70.1")
    target["files"] = []
    target["attribution"] = "unattributed"
    target.pop("attribution_note", None)
    _assert_invalid(tmp_path, "no-note", raw, "attribution_note")

    # An unattributed row that names files is the same lie in reverse.
    raw = deepcopy(_raw_registry())
    _find(raw, "H-70.1")["attribution"] = "unattributed"
    _assert_invalid(tmp_path, "attributed-but-marked", raw, "marked unattributed but names files")


def test_an_unattributed_row_loads_when_its_absence_is_explained(tmp_path: Path) -> None:
    """The vocabulary member must work, or the rule is untested where it matters."""
    raw = deepcopy(_raw_registry())
    target = _find(raw, "H-70.1")
    target["files"] = []
    target["attribution"] = "unattributed"
    target["attribution_note"] = "No file identified; the prose names no symbol resolvable in the tree."
    registry = hardening_registry.load_registry(_write(tmp_path, "explained", raw))
    assert registry.finding("H-70.1").files == ()


def test_reranking_without_a_reason_is_rejected(tmp_path: Path) -> None:
    raw = deepcopy(_raw_registry())
    target = _find(raw, "H-70.1")
    target.pop("severity_note", None)
    _assert_invalid(tmp_path, "silent-rerank", raw, "severity_note")


def test_codeql_evidence_requires_its_alert_count(tmp_path: Path) -> None:
    raw = deepcopy(_raw_registry())
    _find(raw, "H-82.3").pop("alert_count", None)
    _assert_invalid(tmp_path, "no-alerts", raw, "positive alert_count")

    raw = deepcopy(_raw_registry())
    _find(raw, "H-70.1")["alert_count"] = 3
    _assert_invalid(tmp_path, "stray-alerts", raw, "without codeql evidence")


def test_registry_rejects_structural_drift(tmp_path: Path) -> None:
    with pytest.raises(hardening_registry.HardeningRegistryError, match="cannot read"):
        hardening_registry.load_registry(tmp_path / "missing.yaml")
    _assert_invalid(tmp_path, "root", [], "YAML object")

    raw = deepcopy(_raw_registry())
    raw["version"] = 0
    _assert_invalid(tmp_path, "version", raw, "positive integer")

    raw = deepcopy(_raw_registry())
    _find(raw, "H-70.1")["theme"] = "not-a-theme"
    _assert_invalid(tmp_path, "theme", raw, "unknown theme")

    raw = deepcopy(_raw_registry())
    _find(raw, "H-70.1")["issue"] = 999
    _assert_invalid(tmp_path, "issue", raw, "issue must be one of")

    raw = deepcopy(_raw_registry())
    raw["findings"] = [item for item in raw["findings"] if item["issue"] != 72]
    _assert_invalid(tmp_path, "dropped-issue", raw, "issue set mismatch")

    raw = deepcopy(_raw_registry())
    raw["findings"].append(deepcopy(_find(raw, "H-70.1")))
    _assert_invalid(tmp_path, "dupe", raw, "duplicate finding")

    raw = deepcopy(_raw_registry())
    _find(raw, "H-70.1")["contradiction"] = "C-404"
    _assert_invalid(tmp_path, "bad-contradiction", raw, "unknown contradiction")

    raw = deepcopy(_raw_registry())
    raw["contradictions"][0]["between"] = ["H-999.1"]
    _assert_invalid(tmp_path, "bad-between", raw, "unknown findings")


def test_finding_id_must_encode_its_own_issue(tmp_path: Path) -> None:
    raw = deepcopy(_raw_registry())
    _find(raw, "H-70.1")["id"] = "H-71.99"
    _assert_invalid(tmp_path, "id-drift", raw, "does not encode its own issue")


# --------------------------------------------------------------------------
# The GitHub projection is idempotent and preserves human prose
# --------------------------------------------------------------------------


def test_managed_block_is_idempotent_and_preserves_prose() -> None:
    registry = hardening_registry.load_registry()
    block = hardening_registry.managed_issue_block(registry, 81)
    body = "Original reviewer prose that must survive.\n\n- [ ] a finding\n"

    once = hardening_registry.replace_managed_block(body, block)
    twice = hardening_registry.replace_managed_block(once, block)
    assert once == twice
    assert "Original reviewer prose that must survive." in twice
    assert "- [ ] a finding" in twice
    assert twice.count(hardening_registry.START_MARKER) == 1
    assert "H-81.13" in twice


def test_managed_block_states_counts_are_derived_elsewhere() -> None:
    registry = hardening_registry.load_registry()
    block = hardening_registry.managed_issue_block(registry, 70)
    assert "check_hardening.py" in block
    assert registry.plan in block


def test_the_two_registries_do_not_share_a_marker() -> None:
    """A hardening contract must not be labelled as a completion one."""
    assert hardening_registry.START_MARKER != completion_registry.START_MARKER
    assert "hardening" in hardening_registry.START_MARKER
    assert "completion" in completion_registry.START_MARKER


def test_both_managed_blocks_coexist_without_clobbering_each_other() -> None:
    """The real test of the marker split: two blocks in one body, both survive.

    Sharing the literal marker would have made the second sync overwrite the
    first, silently, on any issue that ever carried both. The issue sets are
    disjoint today — this pins the property so a future overlap cannot
    reintroduce the collision.
    """
    hardening = hardening_registry.load_registry()
    completion = completion_registry.load_registry()

    body = "Human prose that must survive both syncs.\n"
    body = completion_registry.replace_managed_block(body, completion_registry.managed_issue_block(completion, 19))
    body = hardening_registry.replace_managed_block(body, hardening_registry.managed_issue_block(hardening, 70))

    assert "Human prose that must survive both syncs." in body
    assert body.count(completion_registry.START_MARKER) == 1
    assert body.count(hardening_registry.START_MARKER) == 1
    assert "HOSPES 1.0 managed completion contract" in body
    assert "HOSPES managed hardening contract" in body

    # Re-running either sync is a no-op, and neither disturbs the other.
    once = hardening_registry.replace_managed_block(body, hardening_registry.managed_issue_block(hardening, 70))
    assert once == body
    twice = completion_registry.replace_managed_block(once, completion_registry.managed_issue_block(completion, 19))
    assert twice == body


# --------------------------------------------------------------------------
# The gate reds — observed, not assumed
# --------------------------------------------------------------------------


def _run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--check", *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    )


def _ratchet(tmp_path: Path, **counts: int) -> Path:
    path = tmp_path / "ratchet.json"
    path.write_text(json.dumps(counts), encoding="utf-8")
    return path


def test_gate_is_green_on_the_recorded_backlog() -> None:
    result = _run_gate()
    assert result.returncode == 0, result.stdout
    assert "review debt gate OK" in result.stdout


def test_gate_reds_on_a_regression(tmp_path: Path) -> None:
    """More open findings than the ratchet allows must fail the build."""
    result = _run_gate("--ratchet", str(_ratchet(tmp_path, open_total=50, open_s1=5)))
    assert result.returncode == 1
    assert "REGRESSION" in result.stdout
    assert "review debt gate FAILED" in result.stdout


def test_gate_reds_on_a_stale_ratchet(tmp_path: Path) -> None:
    """Closing findings without lowering the ratchet is also a failure.

    This direction is what makes the number honest: bookkeeping cannot lag
    behind the work, because the gate re-derives the count every run.
    """
    result = _run_gate("--ratchet", str(_ratchet(tmp_path, open_total=500, open_s1=400)))
    assert result.returncode == 1
    assert "STALE RATCHET" in result.stdout


def test_gate_reds_when_the_ratchet_is_missing(tmp_path: Path) -> None:
    result = _run_gate("--ratchet", str(tmp_path / "absent.json"))
    assert result.returncode == 1
    assert "ratchet file missing" in result.stdout


def test_gate_reds_when_a_finding_is_orphaned_by_a_rename(tmp_path: Path) -> None:
    """A path that no longer exists detaches the backlog from the code silently."""
    result = _run_gate("--root", str(tmp_path))
    assert result.returncode == 1
    assert "names a path that does not exist" in result.stdout


def test_gate_reds_when_the_registry_will_not_load(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("findings: []\n", encoding="utf-8")
    result = _run_gate("--registry", str(broken))
    assert result.returncode == 1
    assert "registry did not load" in result.stdout
