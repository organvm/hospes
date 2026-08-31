"""Canonical HOSPES 1.0 issue, predicate, and receipt registry."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

import yaml

EXPECTED_ISSUES = frozenset({9, *range(19, 35)})
KNOWN_SURFACES = frozenset(
    {
        "api",
        "app",
        "browser",
        "cli",
        "custody",
        "documentation",
        "human_receipts",
        "pdf",
        "provider_receipts",
        "receipts",
        "schema",
        "security",
        "service",
        "ui",
    }
)
START_MARKER = "<!-- hospes-completion-registry:start -->"
END_MARKER = "<!-- hospes-completion-registry:end -->"


class CompletionRegistryError(ValueError):
    """Raised when the completion control plane is incomplete or inconsistent."""


@dataclass(frozen=True)
class CompletionIssue:
    number: int
    title: str
    dependencies: tuple[str, ...]
    predicate: str
    required_surfaces: tuple[str, ...]
    receipt_owner: str
    close_condition: str
    # Set only when the predicate's artifact cannot yet exist because the issue
    # turns on a human-gated real-world event. `scripts/run_predicates.py`
    # treats a missing artifact as a failure UNLESS a reason is recorded here,
    # and treats a reason that outlives the artifact's arrival as drift. The
    # alternative — writing a passing test for an event that has not happened —
    # would fabricate the receipt the close condition exists to demand.
    predicate_absent_reason: str | None = None


@dataclass(frozen=True)
class CompletionRegistry:
    version: int
    repository: str
    milestone: str
    registry_owner: str
    substrates: Mapping[str, Mapping[str, str]]
    issues: tuple[CompletionIssue, ...]

    def issue(self, number: int) -> CompletionIssue:
        for item in self.issues:
            if item.number == number:
                return item
        raise CompletionRegistryError(f"issue #{number} is not registered")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompletionRegistryError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    """Absent is allowed; present-but-empty is not — an empty reason explains nothing."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CompletionRegistryError(f"{field} must be non-empty text when present")
    return value.strip()


def load_registry(path: str | Path | None = None) -> CompletionRegistry:
    try:
        text = (
            Path(path).read_text(encoding="utf-8")
            if path
            else files("hospes.resources").joinpath("spec/completion-registry.yaml").read_text(encoding="utf-8")
        )
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise CompletionRegistryError(f"cannot read completion registry: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CompletionRegistryError("completion registry must be a YAML object")
    version = raw.get("version")
    if not isinstance(version, int) or version < 1:
        raise CompletionRegistryError("completion registry version must be a positive integer")
    substrates_raw = raw.get("substrates")
    if not isinstance(substrates_raw, Mapping) or not substrates_raw:
        raise CompletionRegistryError("completion registry requires substrates")
    substrates: dict[str, Mapping[str, str]] = {}
    for key, value in substrates_raw.items():
        if not isinstance(key, str) or not key.startswith("substrate.") or not isinstance(value, Mapping):
            raise CompletionRegistryError("substrates require named object entries")
        substrates[key] = {
            "title": _required_text(value.get("title"), f"{key}.title"),
            "predicate": _required_text(value.get("predicate"), f"{key}.predicate"),
            "receipt_owner": _required_text(value.get("receipt_owner"), f"{key}.receipt_owner"),
        }
    issues_raw = raw.get("issues")
    if not isinstance(issues_raw, list):
        raise CompletionRegistryError("completion registry issues must be a list")
    issues: list[CompletionIssue] = []
    seen: set[int] = set()
    for index, value in enumerate(issues_raw):
        if not isinstance(value, Mapping):
            raise CompletionRegistryError(f"issues[{index}] must be an object")
        number = value.get("number")
        if not isinstance(number, int) or number in seen:
            raise CompletionRegistryError(f"issues[{index}].number is invalid or duplicated")
        seen.add(number)
        dependencies_raw = value.get("dependencies", [])
        surfaces_raw = value.get("required_surfaces")
        if not isinstance(dependencies_raw, list) or not all(isinstance(item, str) for item in dependencies_raw):
            raise CompletionRegistryError(f"issue #{number} dependencies must be text")
        if (
            not isinstance(surfaces_raw, list)
            or not surfaces_raw
            or not all(isinstance(item, str) for item in surfaces_raw)
        ):
            raise CompletionRegistryError(f"issue #{number} required_surfaces must be non-empty text")
        unknown_surfaces = set(surfaces_raw) - KNOWN_SURFACES
        if unknown_surfaces:
            raise CompletionRegistryError(f"issue #{number} has unknown surfaces: {sorted(unknown_surfaces)}")
        issues.append(
            CompletionIssue(
                number=number,
                title=_required_text(value.get("title"), f"issue #{number} title"),
                dependencies=tuple(dependencies_raw),
                predicate=_required_text(value.get("predicate"), f"issue #{number} predicate"),
                required_surfaces=tuple(surfaces_raw),
                receipt_owner=_required_text(value.get("receipt_owner"), f"issue #{number} receipt_owner"),
                close_condition=_required_text(value.get("close_condition"), f"issue #{number} close_condition"),
                predicate_absent_reason=_optional_text(
                    value.get("predicate_absent_reason"), f"issue #{number} predicate_absent_reason"
                ),
            )
        )
    if seen != EXPECTED_ISSUES:
        missing = sorted(EXPECTED_ISSUES - seen)
        extra = sorted(seen - EXPECTED_ISSUES)
        raise CompletionRegistryError(f"completion issue set mismatch: missing={missing}, extra={extra}")
    known_dependencies = set(substrates) | {f"issue:{number}" for number in seen}
    for issue in issues:
        unknown = set(issue.dependencies) - known_dependencies
        if unknown:
            raise CompletionRegistryError(f"issue #{issue.number} has unknown dependencies: {sorted(unknown)}")
        if f"issue:{issue.number}" in issue.dependencies:
            raise CompletionRegistryError(f"issue #{issue.number} cannot depend on itself")
        expected_owner = f"github://{raw.get('repository')}/issues/{issue.number}"
        if issue.receipt_owner != expected_owner:
            raise CompletionRegistryError(f"issue #{issue.number} receipt owner must be {expected_owner}")
    return CompletionRegistry(
        version=version,
        repository=_required_text(raw.get("repository"), "repository"),
        milestone=_required_text(raw.get("milestone"), "milestone"),
        registry_owner=_required_text(raw.get("registry_owner"), "registry_owner"),
        substrates=substrates,
        issues=tuple(sorted(issues, key=lambda item: item.number)),
    )


def render_markdown_table(registry: CompletionRegistry) -> str:
    rows = [
        "| Issue | Live title | Dependencies | Predicate | Receipt owner |",
        "|---|---|---|---|---|",
    ]
    for issue in registry.issues:
        title = issue.title.replace("|", "\\|")
        dependencies = ", ".join(issue.dependencies) or "none"
        rows.append(f"| #{issue.number} | {title} | {dependencies} | `{issue.predicate}` | `{issue.receipt_owner}` |")
    return "\n".join(rows)


def managed_document_block(registry: CompletionRegistry) -> str:
    return "\n".join((START_MARKER, render_markdown_table(registry), END_MARKER))


def managed_issue_block(registry: CompletionRegistry, number: int) -> str:
    issue = registry.issue(number)
    dependencies = ", ".join(issue.dependencies) or "none"
    surfaces = ", ".join(issue.required_surfaces)
    return "\n".join(
        (
            START_MARKER,
            "## HOSPES 1.0 managed completion contract",
            "",
            "The original requirements above remain in force. This generated block adds the shared dependency, evidence, and closeout contract.",
            "",
            f"- Registry: `{registry.registry_owner}` (version {registry.version})",
            f"- Dependencies: {dependencies}",
            f"- Predicate: `{issue.predicate}`",
            f"- Required evidence surfaces: {surfaces}",
            f"- Durable receipt owner: `{issue.receipt_owner}`",
            f"- Close condition: {issue.close_condition}",
            END_MARKER,
        )
    )


def replace_managed_block(
    value: str,
    block: str,
    *,
    start_marker: str = START_MARKER,
    end_marker: str = END_MARKER,
) -> str:
    """Swap the delimited managed region, leaving all surrounding prose intact.

    The markers are parameters rather than constants because a second registry
    (the hardening registry) projects its own block onto a DIFFERENT set of
    issues. Sharing this function is correct — it is idempotent and prose-safe —
    but sharing the literal `completion-registry` marker would label a hardening
    contract as a completion one, and would collide outright if the two issue
    sets ever overlapped. Defaults preserve the original behaviour exactly.
    """
    start = value.find(start_marker)
    end = value.find(end_marker)
    if start == -1 and end == -1:
        return f"{value.rstrip()}\n\n{block}\n"
    if start == -1 or end == -1 or end < start:
        raise CompletionRegistryError("managed completion block markers are malformed")
    end += len(end_marker)
    return f"{value[:start]}{block}{value[end:]}".rstrip() + "\n"


__all__ = [
    "CompletionIssue",
    "CompletionRegistry",
    "CompletionRegistryError",
    "END_MARKER",
    "EXPECTED_ISSUES",
    "START_MARKER",
    "load_registry",
    "managed_document_block",
    "managed_issue_block",
    "render_markdown_table",
    "replace_managed_block",
]
