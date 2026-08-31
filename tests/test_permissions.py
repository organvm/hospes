"""Predicates for the permission matrix loader and the parity gate.

The matrix declared six roles and their capabilities since 1.0 and no runtime
code read it, so nothing could notice that enforcement had drifted away from it
at eight of eighteen sites. These tests pin the loader, the site map, and — as
importantly — that the gate actually reds, since it ships green by construction.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from hospes import permissions
from hospes.service import HumanRole

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_permissions.py"


def _raw_matrix() -> dict:
    return yaml.safe_load((ROOT / "spec" / "permission-matrix.yaml").read_text(encoding="utf-8"))


def _write(tmp_path: Path, name: str, raw: dict) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--check", *args], capture_output=True, text=True, cwd=str(ROOT), check=False
    )


# --------------------------------------------------------------------------
# The matrix loads, and it is bound to the enum
# --------------------------------------------------------------------------


def test_matrix_declares_exactly_the_human_roles() -> None:
    matrix = permissions.load_matrix()
    assert matrix.roles == {member.value for member in HumanRole}
    assert len(matrix.roles) == 6
    assert "editor" in matrix.roles  # post-production; reads only, never a lifecycle write


def test_matrix_rejects_a_role_the_enum_does_not_know(tmp_path: Path) -> None:
    raw = deepcopy(_raw_matrix())
    raw["team_roles"]["intern"] = {"scope": "show", "reads": ["pipeline"]}
    with pytest.raises(permissions.PermissionMatrixError, match="absent from HumanRole"):
        permissions.load_matrix(_write(tmp_path, "extra-role", raw))


def test_matrix_rejects_an_enum_member_it_omits(tmp_path: Path) -> None:
    """Drift in the other direction: a shipped role the contract forgot."""
    raw = deepcopy(_raw_matrix())
    del raw["team_roles"]["editor"]
    with pytest.raises(permissions.PermissionMatrixError, match="absent from the matrix"):
        permissions.load_matrix(_write(tmp_path, "missing-role", raw))


def test_matrix_requires_a_scope_per_role(tmp_path: Path) -> None:
    raw = deepcopy(_raw_matrix())
    del raw["team_roles"]["host"]["scope"]
    with pytest.raises(permissions.PermissionMatrixError, match="requires a scope"):
        permissions.load_matrix(_write(tmp_path, "no-scope", raw))


def test_capabilities_are_axis_qualified() -> None:
    """`reads:clearances` and `decides:clearances` are different capabilities.

    Without the axis prefix they would collide, and the collision would silently
    grant every reader the right to decide.
    """
    matrix = permissions.load_matrix()
    assert matrix.declares("reads:clearances")
    assert matrix.declares("decides:clearances")
    assert matrix.holders("reads:clearances") != matrix.holders("decides:clearances")


# --------------------------------------------------------------------------
# The site map reads live values
# --------------------------------------------------------------------------


def test_every_site_resolves_to_a_live_frozenset() -> None:
    for site in permissions.ENFORCEMENT_SITES:
        assert isinstance(site.roles(), frozenset)
        assert site.roles(), f"{site.key} is empty"


def test_a_renamed_enforcement_attribute_raises() -> None:
    """The map must not rot silently when a module renames its role set."""
    stale = permissions.EnforcementSite("clearances", "ROLES_THAT_NO_LONGER_EXIST", None, "x")
    with pytest.raises(permissions.PermissionMatrixError, match="no longer exists"):
        stale.roles()


def test_sites_are_read_by_import_not_by_parsing_source() -> None:
    """A source-parsing census can pass while the running code does otherwise."""
    site = next(s for s in permissions.ENFORCEMENT_SITES if s.key == "sponsors:READ_ROLES")
    from hospes import sponsors

    assert site.roles() == sponsors.READ_ROLES


# --------------------------------------------------------------------------
# The divergence census
# --------------------------------------------------------------------------


def test_no_site_enforces_a_role_the_matrix_does_not_know() -> None:
    """A NARROWER set is legitimate; an unknown role is drift."""
    assert permissions.unknown_roles() == {}


def test_the_census_finds_the_known_divergences() -> None:
    found = {item.site: set(item.code_grants_extra) for item in permissions.divergences()}
    assert len(found) == 9
    # Every one runs the same direction: the code grants more than the contract.
    for item in permissions.divergences():
        assert item.code_grants_extra
        assert not item.code_refuses_granted


def test_the_two_pii_divergences_are_recorded() -> None:
    """The sharpest pair: both gate DECRYPTION of third-party contact data."""
    found = {item.site: set(item.code_grants_extra) for item in permissions.divergences()}
    assert found["contact_roster:AUTHORIZED_CONTACT_ROLES"] == {"host"}
    assert found["touchpoints:AUTHORIZED_TOUCHPOINT_ROLES"] == {"host"}


def test_notifications_team_roles_diverges_once_its_proposed_name_is_wired() -> None:
    """Wiring a proposed capability into ENFORCEMENT_SITES can surface a real gap.

    `notifications:TEAM_ROLES` was `capability=None` until this change; its
    proposed name `reads:notifications` turns out to already be declared, held
    by `editor` alone. The site enforces every team role. This divergence
    existed the whole time — it was invisible only because nothing compared
    the site against a capability the matrix had already named.
    """
    found = {item.site: set(item.code_grants_extra) for item in permissions.divergences()}
    assert found["notifications:TEAM_ROLES"] == {
        "editorial_owner",
        "host",
        "network_operator",
        "producer",
        "relationship_owner",
    }


def test_guest_history_diverges_opposite_to_the_h_73_9_fix() -> None:
    """H-73.9 wants hosts admitted; the matrix ALREADY grants host guest_history.

    The divergence here is the other way — the code additionally admits
    editorial_owner and producer. Pinning this keeps the two from being conflated
    when H-73.9 is implemented.
    """
    matrix = permissions.load_matrix()
    assert "host" in matrix.holders("reads:guest_history")
    found = {item.site: set(item.code_grants_extra) for item in permissions.divergences(matrix)}
    assert found["guest_crm:AUTHORIZED_HISTORY_ROLES"] == {"editorial_owner", "producer"}


def test_the_census_sees_every_role_set_not_just_the_named_ones() -> None:
    """An import-based census sees 18 of 39. The AST census sees all of them.

    This is the gate's own completeness. Reporting the size of the map as though
    it were the surface is a coverage claim wrong by more than half, stated with
    a green tick.
    """
    literals = permissions.role_literals()
    assert len(literals) == 39
    uncovered = permissions.uncovered_role_literals()
    assert len(uncovered) == 21
    assert len(literals) - len(uncovered) == len(permissions.ENFORCEMENT_SITES)


def test_the_eighth_role_helper_is_visible() -> None:
    """`partnership_records._require_owner_role` is invisible to both prior methods.

    It has a different name from the other seven helpers, so a grep for
    `_require_role` misses it, and it gates on an inline literal rather than a
    module constant, so an import-based census misses it too.
    """
    keys = {item.key for item in permissions.uncovered_role_literals()}
    assert "partnership_records:_require_owner_role" in keys


def test_a_table_of_role_sets_is_counted_per_entry() -> None:
    """`service._RECEIPT_ROLES` maps seven receipt types to seven role sets."""
    receipt = [item for item in permissions.role_literals() if item.key == "service:_RECEIPT_ROLES"]
    assert len(receipt) == 7


def test_nested_modules_are_scanned_with_dotted_module_names(tmp_path: Path) -> None:
    """A role gate in a subpackage must be visible to the permission census."""
    nested = tmp_path / "providers"
    nested.mkdir()
    (nested / "live.py").write_text(
        "_ALLOWED = frozenset({'producer', 'relationship_owner'})\n",
        encoding="utf-8",
    )

    literals = permissions.role_literals(package_dir=tmp_path)

    assert [(item.module, item.scope, item.members) for item in literals] == [
        ("providers.live", "_ALLOWED", ("producer", "relationship_owner")),
    ]


def test_signature_carries_members_so_a_widened_set_is_a_new_signature() -> None:
    """A site already known to be uncovered must not be able to quietly grant more."""
    item = next(i for i in permissions.role_literals() if i.key == "pilot_models:_ALLOWED_ROLES")
    assert item.signature.endswith("|editorial_owner,producer,relationship_owner")
    widened = permissions.RoleLiteral(item.module, item.scope, item.kind, (*item.members, "host"), item.unknown)
    assert widened.signature != item.signature


def test_enum_written_role_sets_are_normalised_to_role_values() -> None:
    """`{HumanRole.PRODUCER}` and `{"producer"}` are the same decision."""
    enum_sites = [item for item in permissions.role_literals() if item.kind == "enum"]
    assert enum_sites
    for item in enum_sites:
        for member in item.members:
            assert member in {role.value for role in HumanRole}


def test_unnamed_sites_carry_a_proposed_capability_name_not_none() -> None:
    """A still-unnamed site stores its proposed name as `capability`, not None.

    Storing `None` would need a second, easily-forgotten code edit the moment a
    maintainer adds the proposed name to the matrix. Storing the name itself
    means `matrix.declares(site.capability)` starts returning true — and the
    site starts being checked by `divergences()` — the instant the matrix
    gains the vocabulary, with no code change required at all.
    """
    matrix = permissions.load_matrix()
    for site in permissions.unnamed_sites(matrix):
        assert site.capability is not None
        assert not matrix.declares(site.capability)


def test_unnamed_site_signature_changes_when_its_enforced_roles_widen() -> None:
    """A key-only baseline cannot see a still-unnamed site grant a new role."""
    site = next(s for s in permissions.ENFORCEMENT_SITES if s.key == "distribution:PUBLISH_ROLES")
    before = permissions.unnamed_signature(site)
    widened = permissions.EnforcementSite(site.module, site.attribute, site.capability, site.note)
    # roles() re-imports live; simulate a widened set the way the gate would see it.
    import hospes.distribution as distribution_module

    original = distribution_module.PUBLISH_ROLES
    try:
        distribution_module.PUBLISH_ROLES = original | {"host"}
        after = permissions.unnamed_signature(widened)
    finally:
        distribution_module.PUBLISH_ROLES = original
    assert after != before


# --------------------------------------------------------------------------
# The gate reds — observed, not assumed
# --------------------------------------------------------------------------


def test_gate_is_green_on_the_recorded_baseline() -> None:
    result = _run_gate()
    assert result.returncode == 0, result.stdout
    assert "permission parity OK" in result.stdout
    # Green means "exactly the known disagreements", so they stay visible.
    assert "code is more permissive than the contract at:" in result.stdout


def test_gate_reds_on_a_new_divergence(tmp_path: Path) -> None:
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    del baseline["divergences"]["contact_roster:AUTHORIZED_CONTACT_ROLES"]
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert "NEW DIVERGENCE: contact_roster:AUTHORIZED_CONTACT_ROLES" in result.stdout


def test_gate_reds_on_a_widened_divergence(tmp_path: Path) -> None:
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    baseline["divergences"]["contact_roster:AUTHORIZED_CONTACT_ROLES"]["extra"] = ["host", "editor"]
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert "CHANGED DIVERGENCE" in result.stdout


def test_gate_reds_on_a_changed_capability_mapping(tmp_path: Path) -> None:
    """A site re-pointed at a DIFFERENT capability with the same holders must still red.

    The matrix has 21 capability names sharing only 10 distinct role-sets, so
    the extra/refused role diffs alone cannot distinguish this from no change
    at all — only comparing the capability itself catches it.
    """
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    baseline["divergences"]["clearances:CLEARANCE_DECISION_ROLES"]["capability"] = "decides:sponsor_claim_approval"
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert "CHANGED CAPABILITY: clearances:CLEARANCE_DECISION_ROLES" in result.stdout


def test_gate_reds_on_a_stale_baseline_entry(tmp_path: Path) -> None:
    """Reconciling a divergence and removing its line must be one change."""
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    baseline["divergences"]["clearances:AUTHORIZED_CLEARANCE_ROLES"] = {"extra": ["editor"], "refused": []}
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert "STALE BASELINE: clearances:AUTHORIZED_CLEARANCE_ROLES" in result.stdout


def test_gate_reds_on_a_changed_refusal(tmp_path: Path) -> None:
    """The `extra` side was always ratcheted; `refused` previously was not at all.

    A site could REVOKE a matrix-granted role while its recorded `extra` stayed
    untouched and the gate stayed green, because nothing compared the refused
    side to anything. Simulate that by recording a refusal the code does not
    actually make; the gate must notice the baseline no longer matches reality.
    """
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    baseline["divergences"]["contact_roster:AUTHORIZED_CONTACT_ROLES"]["refused"] = ["producer"]
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert "CHANGED REFUSAL: contact_roster:AUTHORIZED_CONTACT_ROLES" in result.stdout


def test_gate_reds_on_a_widened_unnamed_capability(tmp_path: Path) -> None:
    """A still-unnamed site is baselined by signature, not by key alone.

    Changing the recorded role membership for a key that stays present
    simulates the code widening what an unnamed site enforces — a scenario a
    key-only baseline could never catch because the key itself never changes.
    """
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    baseline["unnamed_capabilities"] = [
        "distribution:PUBLISH_ROLES|editorial_owner,host,relationship_owner"
        if entry.startswith("distribution:PUBLISH_ROLES")
        else entry
        for entry in baseline["unnamed_capabilities"]
    ]
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert (
        "NEW OR WIDENED UNNAMED CAPABILITY: distribution:PUBLISH_ROLES|editorial_owner,relationship_owner"
        in result.stdout
    )


def test_gate_reds_on_a_role_set_that_escapes_the_census(tmp_path: Path) -> None:
    """Dropping a coverage entry simulates a new inline role set appearing."""
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    baseline["uncovered_role_sets"].remove(
        "partnership_records:_require_owner_role|editorial_owner,producer,relationship_owner"
    )
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert "UNCOVERED ROLE SET: partnership_records:_require_owner_role" in result.stdout


def test_gate_reds_on_a_stale_coverage_entry(tmp_path: Path) -> None:
    """Covering a site and removing its line must be one change."""
    baseline = json.loads((ROOT / "permission-divergence.json").read_text(encoding="utf-8"))
    baseline["uncovered_role_sets"].append("ghost:module|producer")
    path = tmp_path / "b.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = _run_gate("--baseline", str(path))
    assert result.returncode == 1
    assert "STALE COVERAGE GAP: ghost:module" in result.stdout


def test_gate_reports_the_true_surface_not_the_size_of_its_map() -> None:
    result = _run_gate()
    assert result.returncode == 0
    assert "39 in hospes/*.py — 18 mapped to a capability, 21 not yet" in result.stdout


def test_gate_reds_when_the_baseline_is_missing(tmp_path: Path) -> None:
    result = _run_gate("--baseline", str(tmp_path / "absent.json"))
    assert result.returncode == 1
    assert "baseline file missing" in result.stdout


def test_gate_reds_on_a_mirror_mismatch(tmp_path: Path) -> None:
    """spec/ and the packaged copy must stay byte-identical."""
    spec = importlib.util.spec_from_file_location("hospes_check_permissions_under_test", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert module.check_mirror() == []
        drifted = tmp_path / "drifted.yaml"
        drifted.write_text("team_roles: {}\n", encoding="utf-8")
        module.PACKAGED = drifted
        problems = module.check_mirror()
        assert problems and "differ" in problems[0]
    finally:
        sys.modules.pop(spec.name, None)


# --------------------------------------------------------------------------
# A role literal with an unrecognized member is reported, not discarded
# --------------------------------------------------------------------------


def test_role_literal_with_one_typo_is_reported_not_dropped(tmp_path: Path) -> None:
    """A near-miss role literal — mostly known roles, one that is not — must survive.

    `_role_literal_members` used to require every element be a known role to
    accept the literal at all; a single typo made the whole site vanish from
    the census before either the coverage check or a vocabulary check could
    see it. It must now be returned, flagged as carrying an unknown member.
    """
    (tmp_path / "fake_site.py").write_text(
        "_ALLOWED = frozenset({'host', 'producer', 'editroial_owner'})\n", encoding="utf-8"
    )
    literals = permissions.role_literals(package_dir=tmp_path)
    assert len(literals) == 1
    assert literals[0].members == ("editroial_owner", "host", "producer")
    assert literals[0].unknown == ("editroial_owner",)


def test_unrelated_string_literals_are_still_not_swept_in(tmp_path: Path) -> None:
    """Accepting near-misses must not reopen the door to unrelated string sets.

    A same-shaped literal with ZERO overlap with real role names — an ordinary
    status vocabulary, say — is not role-shaped no matter how it looks
    structurally, and must not appear in the census at all.
    """
    (tmp_path / "fake_site.py").write_text(
        "_STATUSES = frozenset({'pending', 'active', 'archived'})\n", encoding="utf-8"
    )
    assert permissions.role_literals(package_dir=tmp_path) == []


def test_no_unknown_role_literal_exists_in_the_package_today() -> None:
    """This always reds live — there is no baseline, because a role the matrix
    has never heard of is never a resting state."""
    assert permissions.unknown_role_literals() == []


def test_gate_reds_on_an_unknown_role_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire an unknown-role literal into the live census and confirm the gate catches it."""
    spec = importlib.util.spec_from_file_location("hospes_check_permissions_under_test_unknown", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        monkeypatch.setattr(
            module,
            "unknown_role_literals",
            lambda: [permissions.RoleLiteral("fake", "_ALLOWED", "str", ("host", "produccer"), ("produccer",))],
        )
        monkeypatch.setattr(sys, "argv", ["check_permissions.py", "--check"])
        result = module.main()
        assert result != 0
    finally:
        sys.modules.pop(spec.name, None)


# --------------------------------------------------------------------------
# Matrix-loading validation the loader used to accept silently
# --------------------------------------------------------------------------


def test_matrix_rejects_a_scope_outside_the_known_vocabulary(tmp_path: Path) -> None:
    raw = _raw_matrix()
    raw["team_roles"]["host"]["scope"] = "global"
    path = _write(tmp_path, "bad-scope", raw)
    with pytest.raises(permissions.PermissionMatrixError, match="scope"):
        permissions.load_matrix(path)


def test_matrix_rejects_a_falsey_non_list_capability_axis(tmp_path: Path) -> None:
    """`reads: {}` (or `0`, or `""`) must raise, not silently become an empty list.

    `body.get(axis) or []` treated any falsey value as absent. A malformed
    matrix edit that turns a role's declarations into the wrong type — a dict,
    a number, an empty string — used to erase them instead of failing to load.
    """
    raw = _raw_matrix()
    raw["team_roles"]["host"]["reads"] = {}
    path = _write(tmp_path, "bad-axis", raw)
    with pytest.raises(permissions.PermissionMatrixError, match="reads"):
        permissions.load_matrix(path)


def test_matrix_treats_an_explicit_null_axis_as_empty(tmp_path: Path) -> None:
    """`reads:` with nothing after it is a legitimate empty declaration, not an error.

    Only a wrongly-TYPED value (a dict, a number, a string) should raise; an
    explicit YAML null for an axis nobody filled in is ordinary and must load
    exactly as if the axis were an empty list.
    """
    raw = _raw_matrix()
    raw["team_roles"]["producer"]["reads"] = None
    path = _write(tmp_path, "null-axis", raw)
    matrix = permissions.load_matrix(path)
    readers = {role for cap, roles in matrix.capabilities.items() if cap.startswith("reads:") for role in roles}
    assert "producer" not in readers
