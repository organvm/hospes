"""Predicates for the declared-predicate runner.

The registry has named a `predicate` per issue since 1.0 and nothing ever ran
them, so it could point at a test file that does not exist while `done.sh`
stayed green. These tests pin both halves of the fix: the runner notices a
missing artifact, AND it notices an explanation that has outlived its reason.

The second direction is the one that matters over time. A one-way check would
let `predicate_absent_reason` become a permanent excuse the moment someone
writes the test and forgets the field.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from hospes import completion_registry

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_predicates.py"


def _raw() -> dict:
    return yaml.safe_load(
        files("hospes.resources").joinpath("spec/completion-registry.yaml").read_text(encoding="utf-8")
    )


def _issue(raw: dict, number: int) -> dict:
    for item in raw["issues"]:
        if item["number"] == number:
            return item
    raise AssertionError(f"issue #{number} not in registry")


def _write(tmp_path: Path, name: str, raw: dict) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args], capture_output=True, text=True, cwd=str(ROOT), check=False
    )


# --------------------------------------------------------------------------
# The registry field
# --------------------------------------------------------------------------


def test_pilot_predicate_absence_is_recorded_not_silent() -> None:
    """Issue #9 turns on a real recording; its predicate cannot honestly exist yet."""
    registry = completion_registry.load_registry()
    issue = registry.issue(9)
    assert issue.predicate_absent_reason is not None
    assert "fabricate" in issue.predicate_absent_reason
    # Every other issue's predicate is expected to exist, so none of them may
    # carry the escape hatch.
    for other in registry.issues:
        if other.number != 9:
            assert other.predicate_absent_reason is None


def test_empty_reason_is_rejected(tmp_path: Path) -> None:
    """Present-but-blank explains nothing, so it is not a valid absence marker."""
    raw = deepcopy(_raw())
    _issue(raw, 9)["predicate_absent_reason"] = "   "
    with pytest.raises(completion_registry.CompletionRegistryError, match="non-empty text when present"):
        completion_registry.load_registry(_write(tmp_path, "blank", raw))


def test_reason_is_optional_for_every_other_issue(tmp_path: Path) -> None:
    raw = deepcopy(_raw())
    _issue(raw, 9).pop("predicate_absent_reason", None)
    registry = completion_registry.load_registry(_write(tmp_path, "none", raw))
    assert registry.issue(9).predicate_absent_reason is None


# --------------------------------------------------------------------------
# The runner reds — observed, not assumed
# --------------------------------------------------------------------------


def test_runner_is_green_on_the_shipped_registry() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stdout
    assert "declared-predicate gate OK" in result.stdout
    assert "test_issue_09.py absent — explained" in result.stdout


def test_runner_reds_on_a_missing_predicate_with_no_reason(tmp_path: Path) -> None:
    """The exact defect that shipped: a declared predicate pointing at nothing."""
    raw = deepcopy(_raw())
    _issue(raw, 9).pop("predicate_absent_reason", None)
    result = _run("--check", "--registry", str(_write(tmp_path, "unexplained", raw)))
    assert result.returncode == 1
    assert "test_issue_09.py, which does not exist" in result.stdout
    assert "record a predicate_absent_reason" in result.stdout


def test_runner_reds_when_an_explanation_outlives_its_artifact(tmp_path: Path) -> None:
    """A reason attached to a predicate that now exists is drift, not a stale note."""
    raw = deepcopy(_raw())
    # #19's predicate exists on disk; attaching a reason to it is the drift shape.
    _issue(raw, 19)["predicate_absent_reason"] = "no longer true"
    result = _run("--check", "--registry", str(_write(tmp_path, "stale", raw)))
    assert result.returncode == 1
    assert "now exists" in result.stdout
    assert "delete the reason" in result.stdout


def test_runner_reds_on_an_unparseable_predicate(tmp_path: Path) -> None:
    """An unrecognised command is an unchecked one; it must not pass silently."""
    raw = deepcopy(_raw())
    _issue(raw, 19)["predicate"] = "make some-target"
    result = _run("--check", "--registry", str(_write(tmp_path, "opaque", raw)))
    assert result.returncode == 1
    assert "no artifact recognised" in result.stdout


def test_runner_counts_every_declared_predicate() -> None:
    result = _run("--check")
    assert "20 predicates (3 substrate, 17 issue)" in result.stdout


# --------------------------------------------------------------------------
# Portability
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner_module():
    """Import the runner as a module.

    It must be registered in `sys.modules` BEFORE `exec_module`: the runner's
    `@dataclass` resolves its `str | None` annotations by looking its own module
    up in that table, and a module missing from it makes the lookup return None.
    """
    spec = importlib.util.spec_from_file_location("hospes_run_predicates_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_bare_python_is_resolved_to_the_running_interpreter(runner_module) -> None:
    """The registry stores `python -m pytest`; a stock macOS toolchain has no `python`."""
    argv = runner_module.resolve("python -m pytest tests/issue_predicates/test_issue_19.py -q")
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "pytest"]
    # A command that does not start with `python` is left alone.
    assert runner_module.resolve("bash scripts/verify-storage-substrate.sh")[0] == "bash"


def test_artifact_extraction_finds_both_python_and_shell_targets(runner_module) -> None:
    assert runner_module.artifacts_for("python -m pytest tests/issue_predicates/test_issue_19.py -q") == [
        "tests/issue_predicates/test_issue_19.py"
    ]
    assert runner_module.artifacts_for("bash scripts/verify-storage-substrate.sh") == [
        "scripts/verify-storage-substrate.sh"
    ]
    assert runner_module.artifacts_for("make target") == []
