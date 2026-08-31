from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from hospes import completion_registry


def _raw_registry() -> dict:
    return yaml.safe_load(
        files("hospes.resources")
        .joinpath("spec/completion-registry.yaml")
        .read_text(encoding="utf-8")
    )


def _assert_invalid(tmp_path: Path, name: str, raw: object, match: str) -> None:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(completion_registry.CompletionRegistryError, match=match):
        completion_registry.load_registry(path)


def test_completion_registry_has_exact_live_issue_map() -> None:
    registry = completion_registry.load_registry()
    assert {issue.number for issue in registry.issues} == {9, *range(19, 35)}
    assert registry.issue(19).title.startswith("[P1-1] Guest Suggestion")
    assert registry.issue(20).title.startswith("[P1-2] Contact Roster")
    assert registry.issue(34).title.startswith("[P4-3] AI Research Assistant")
    assert "earliest confirmed production window" in registry.issue(9).title
    assert registry.issue(19).predicate.endswith("test_issue_19.py -q")


def test_completion_registry_rejects_missing_and_unknown_dependencies(tmp_path: Path) -> None:
    raw = _raw_registry()
    raw["issues"][0]["dependencies"].append("issue:404")
    _assert_invalid(tmp_path, "dependency", raw, "unknown dependencies")


def test_completion_registry_rejects_malformed_control_plane(tmp_path: Path) -> None:
    with pytest.raises(completion_registry.CompletionRegistryError, match="cannot read"):
        completion_registry.load_registry(tmp_path / "missing.yaml")
    _assert_invalid(tmp_path, "root", [], "YAML object")

    raw = _raw_registry()
    raw["version"] = 0
    _assert_invalid(tmp_path, "version", raw, "positive integer")

    raw = _raw_registry()
    raw["substrates"] = {}
    _assert_invalid(tmp_path, "substrates", raw, "requires substrates")

    raw = _raw_registry()
    raw["substrates"]["invalid"] = raw["substrates"].pop("substrate.storage_migration")
    _assert_invalid(tmp_path, "substrate-name", raw, "named object")

    raw = _raw_registry()
    raw["substrates"]["substrate.storage_migration"]["title"] = ""
    _assert_invalid(tmp_path, "substrate-title", raw, "must be non-empty text")

    raw = _raw_registry()
    raw["issues"] = {}
    _assert_invalid(tmp_path, "issues-type", raw, "must be a list")

    raw = _raw_registry()
    raw["issues"][0] = "bad"
    _assert_invalid(tmp_path, "issue-type", raw, "must be an object")

    raw = _raw_registry()
    raw["issues"][1]["number"] = 9
    _assert_invalid(tmp_path, "duplicate", raw, "invalid or duplicated")

    raw = _raw_registry()
    raw["issues"][0]["dependencies"] = "bad"
    _assert_invalid(tmp_path, "dependencies-type", raw, "dependencies must be text")

    raw = _raw_registry()
    raw["issues"][0]["required_surfaces"] = []
    _assert_invalid(tmp_path, "surfaces-empty", raw, "required_surfaces")

    raw = _raw_registry()
    raw["issues"][0]["required_surfaces"] = ["unknown"]
    _assert_invalid(tmp_path, "surfaces-unknown", raw, "unknown surfaces")

    raw = _raw_registry()
    raw["issues"].pop()
    _assert_invalid(tmp_path, "issue-set", raw, "issue set mismatch")

    raw = _raw_registry()
    raw["issues"][0]["dependencies"] = ["issue:9"]
    _assert_invalid(tmp_path, "self", raw, "cannot depend on itself")

    raw = _raw_registry()
    raw["issues"][0]["receipt_owner"] = "github://wrong"
    _assert_invalid(tmp_path, "owner", raw, "receipt owner must be")


def test_managed_blocks_preserve_original_requirements_and_are_idempotent() -> None:
    registry = completion_registry.load_registry()
    original = "## Acceptance Criteria\n\n- [ ] Preserve me\n"
    block = completion_registry.managed_issue_block(registry, 19)
    first = completion_registry.replace_managed_block(original, block)
    second = completion_registry.replace_managed_block(first, block)
    assert "- [ ] Preserve me" in first
    assert first == second
    assert first.count(completion_registry.START_MARKER) == 1

    document = completion_registry.managed_document_block(registry)
    assert "| #9 |" in document
    assert "| #34 |" in document
    assert completion_registry.render_markdown_table(registry) in document
    with pytest.raises(completion_registry.CompletionRegistryError, match="not registered"):
        registry.issue(404)
    with pytest.raises(completion_registry.CompletionRegistryError, match="markers are malformed"):
        completion_registry.replace_managed_block(
            f"requirements\n{completion_registry.START_MARKER}\n", block
        )
