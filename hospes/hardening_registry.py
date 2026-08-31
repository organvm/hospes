"""Deferred review-finding registry — the hardening control plane.

Sibling of :mod:`hospes.completion_registry`. That module owns the shipped
surface; this one owns the deferred surface so review debt is a *checkable*
state rather than prose in eleven GitHub issues.

Two invariants are enforced here rather than documented:

``ALLOWED_FINDING_KEYS`` is closed
    An unknown key on a finding is an error. This is what makes "the registry
    carries no distance" structural instead of aspirational: there is no field
    to record an open count, a percentage, or a completion ratio in, because
    any such field fails to load. Totals are derived by
    ``scripts/check_hardening.py`` from the rows and from live issue state.

Absence must be named
    An empty ``files`` list requires ``attribution: unattributed`` *and* an
    ``attribution_note``. A ``severity`` whose rank differs from the GitHub
    ``priority`` requires a ``severity_note``. Neither can be left blank, so
    "N/A" is never a resting state.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

import yaml

from hospes.completion_registry import replace_managed_block as _replace_managed_block

# This registry projects onto a DIFFERENT issue set than the completion
# registry, so it carries its own markers. Sharing the swap function is right —
# it is idempotent and preserves human prose — but sharing the literal marker
# would label a hardening contract as a completion one, and would collide
# outright if the two issue sets ever overlapped.
START_MARKER = "<!-- hospes-hardening-registry:start -->"
END_MARKER = "<!-- hospes-hardening-registry:end -->"

EXPECTED_ISSUES = frozenset({70, 71, 72, 73, 76, 77, 78, 80, 81, 82, 83})
PRIORITIES = ("P1", "P2", "P3")
STATUSES = frozenset({"open", "closed"})
ATTRIBUTIONS = frozenset({"annotated", "prose", "symbol", "unattributed"})
EVIDENCE_KINDS = frozenset({"review-thread", "codeql"})

ALLOWED_FINDING_KEYS = frozenset(
    {
        "id",
        "issue",
        "priority",
        "severity",
        "severity_note",
        "theme",
        "files",
        "attribution",
        "attribution_note",
        "close_condition",
        "status",
        "evidence",
        "evidence_note",
        "alert_count",
        "contradiction",
        "blocked_by_phase",
        "landing_constraint",
        "sequence_first_in_track",
        "regression_class",
        "upgrade_risk",
        "partially_addressed",
    }
)


class HardeningRegistryError(ValueError):
    """Raised when the hardening control plane is incomplete or inconsistent."""


@dataclass(frozen=True)
class Finding:
    id: str
    issue: int
    priority: str
    severity: str
    theme: str
    files: tuple[str, ...]
    attribution: str
    close_condition: str
    status: str
    evidence: str
    receipt_owner: str
    contradiction: str | None = None
    blocked_by_phase: int | None = None
    landing_constraint: str | None = None
    alert_count: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def reranked(self) -> bool:
        """True when this registry's severity disagrees with the GitHub label."""
        return _rank(self.severity) != _rank(self.priority)


@dataclass(frozen=True)
class Theme:
    key: str
    title: str
    phase: int


@dataclass(frozen=True)
class Contradiction:
    id: str
    between: tuple[str, ...]
    conflict: str
    resolution: str
    needs_code_check: bool = False
    landing_constraint: str | None = None


@dataclass(frozen=True)
class HardeningRegistry:
    version: int
    repository: str
    registry_owner: str
    campaign: str
    plan: str
    severities: Mapping[str, Mapping[str, str]]
    themes: Mapping[str, Theme]
    contradictions: tuple[Contradiction, ...]
    findings: tuple[Finding, ...]

    def finding(self, identifier: str) -> Finding:
        for item in self.findings:
            if item.id == identifier:
                return item
        raise HardeningRegistryError(f"finding {identifier} is not registered")

    def for_issue(self, number: int) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.issue == number)

    def open_findings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.is_open)

    def by_theme(self, theme: str) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.theme == theme)


def _rank(label: str) -> int:
    """Numeric rank shared by the P- and S- scales, so they can be compared."""
    return int(label[1:])


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HardeningRegistryError(f"{field} must be non-empty text")
    return value.strip()


def _load_themes(raw: Any) -> dict[str, Theme]:
    if not isinstance(raw, Mapping) or not raw:
        raise HardeningRegistryError("hardening registry requires themes")
    themes: dict[str, Theme] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            raise HardeningRegistryError("themes require named object entries")
        phase = value.get("phase")
        if not isinstance(phase, int) or phase not in (1, 2, 3):
            raise HardeningRegistryError(f"theme {key} phase must be 1, 2, or 3")
        themes[key] = Theme(
            key=key,
            title=_required_text(value.get("title"), f"theme {key} title"),
            phase=phase,
        )
    return themes


def _load_contradictions(raw: Any, known_ids: set[str]) -> tuple[Contradiction, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise HardeningRegistryError("contradictions must be a list")
    seen: set[str] = set()
    out: list[Contradiction] = []
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise HardeningRegistryError(f"contradictions[{index}] must be an object")
        identifier = _required_text(value.get("id"), f"contradictions[{index}].id")
        if identifier in seen:
            raise HardeningRegistryError(f"duplicate contradiction {identifier}")
        seen.add(identifier)
        between = value.get("between")
        if not isinstance(between, list) or len(between) < 1:
            raise HardeningRegistryError(f"{identifier} must name the findings it holds between")
        unknown = [item for item in between if item not in known_ids]
        if unknown:
            raise HardeningRegistryError(f"{identifier} references unknown findings: {sorted(unknown)}")
        out.append(
            Contradiction(
                id=identifier,
                between=tuple(between),
                conflict=_required_text(value.get("conflict"), f"{identifier}.conflict"),
                resolution=_required_text(value.get("resolution"), f"{identifier}.resolution"),
                needs_code_check=bool(value.get("needs_code_check", False)),
                landing_constraint=value.get("landing_constraint"),
            )
        )
    return tuple(out)


def _load_finding(value: Any, index: int, repository: str, themes: Mapping[str, Theme]) -> Finding:
    if not isinstance(value, Mapping):
        raise HardeningRegistryError(f"findings[{index}] must be an object")
    unknown_keys = set(value) - ALLOWED_FINDING_KEYS
    if unknown_keys:
        raise HardeningRegistryError(
            f"findings[{index}] has unknown keys {sorted(unknown_keys)}; "
            "the finding schema is closed so no row can carry a derived count"
        )
    identifier = _required_text(value.get("id"), f"findings[{index}].id")
    issue = value.get("issue")
    if not isinstance(issue, int) or issue not in EXPECTED_ISSUES:
        raise HardeningRegistryError(f"{identifier} issue must be one of {sorted(EXPECTED_ISSUES)}")
    if not identifier.startswith(f"H-{issue}."):
        raise HardeningRegistryError(f"{identifier} does not encode its own issue number")

    priority = value.get("priority")
    if priority not in PRIORITIES:
        raise HardeningRegistryError(f"{identifier} priority must be one of {PRIORITIES}")
    severity = value.get("severity")
    if not isinstance(severity, str) or not severity.startswith("S"):
        raise HardeningRegistryError(f"{identifier} severity must be an S-scale label")

    theme = value.get("theme")
    if theme not in themes:
        raise HardeningRegistryError(f"{identifier} has unknown theme {theme!r}")

    files_raw = value.get("files", [])
    if not isinstance(files_raw, list) or not all(isinstance(item, str) and item.strip() for item in files_raw):
        raise HardeningRegistryError(f"{identifier} files must be a list of non-empty paths")

    attribution = value.get("attribution")
    if attribution not in ATTRIBUTIONS:
        raise HardeningRegistryError(f"{identifier} attribution must be one of {sorted(ATTRIBUTIONS)}")

    # Absence must be named, in both directions.
    if not files_raw:
        if attribution != "unattributed":
            raise HardeningRegistryError(f"{identifier} has no files but is not marked unattributed")
        _required_text(value.get("attribution_note"), f"{identifier} attribution_note")
    elif attribution == "unattributed":
        raise HardeningRegistryError(f"{identifier} is marked unattributed but names files")

    if _rank(severity) != _rank(priority):
        _required_text(
            value.get("severity_note"),
            f"{identifier} severity_note (severity {severity} differs from label {priority})",
        )

    evidence = value.get("evidence")
    if evidence not in EVIDENCE_KINDS:
        raise HardeningRegistryError(f"{identifier} evidence must be one of {sorted(EVIDENCE_KINDS)}")
    alert_count = value.get("alert_count")
    if evidence == "codeql":
        if not isinstance(alert_count, int) or alert_count < 1:
            raise HardeningRegistryError(f"{identifier} codeql evidence requires a positive alert_count")
        _required_text(value.get("evidence_note"), f"{identifier} evidence_note")
    elif alert_count is not None:
        raise HardeningRegistryError(f"{identifier} carries alert_count without codeql evidence")

    status = value.get("status")
    if status not in STATUSES:
        raise HardeningRegistryError(f"{identifier} status must be one of {sorted(STATUSES)}")

    blocked_by_phase = value.get("blocked_by_phase")
    if blocked_by_phase is not None and blocked_by_phase not in (1, 2, 3):
        raise HardeningRegistryError(f"{identifier} blocked_by_phase must be 1, 2, or 3")

    return Finding(
        id=identifier,
        issue=issue,
        priority=priority,
        severity=severity,
        theme=theme,
        files=tuple(files_raw),
        attribution=attribution,
        close_condition=_required_text(value.get("close_condition"), f"{identifier} close_condition"),
        status=status,
        evidence=evidence,
        receipt_owner=f"github://{repository}/issues/{issue}",
        contradiction=value.get("contradiction"),
        blocked_by_phase=blocked_by_phase,
        landing_constraint=value.get("landing_constraint"),
        alert_count=alert_count,
    )


def load_registry(path: str | Path | None = None) -> HardeningRegistry:
    try:
        text = (
            Path(path).read_text(encoding="utf-8")
            if path
            else files("hospes.resources").joinpath("spec/hardening-registry.yaml").read_text(encoding="utf-8")
        )
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise HardeningRegistryError(f"cannot read hardening registry: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise HardeningRegistryError("hardening registry must be a YAML object")

    version = raw.get("version")
    if not isinstance(version, int) or version < 1:
        raise HardeningRegistryError("hardening registry version must be a positive integer")
    repository = _required_text(raw.get("repository"), "repository")

    severities_raw = raw.get("severities")
    if not isinstance(severities_raw, Mapping) or not severities_raw:
        raise HardeningRegistryError("hardening registry requires severities")
    severities: dict[str, Mapping[str, str]] = {}
    for key, value in severities_raw.items():
        if not isinstance(key, str) or not key.startswith("S") or not isinstance(value, Mapping):
            raise HardeningRegistryError("severities require S-scale object entries")
        severities[key] = {
            "title": _required_text(value.get("title"), f"{key}.title"),
            "definition": _required_text(value.get("definition"), f"{key}.definition"),
        }

    themes = _load_themes(raw.get("themes"))

    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list) or not findings_raw:
        raise HardeningRegistryError("hardening registry findings must be a non-empty list")

    findings: list[Finding] = []
    seen: set[str] = set()
    for index, value in enumerate(findings_raw):
        finding = _load_finding(value, index, repository, themes)
        if finding.id in seen:
            raise HardeningRegistryError(f"duplicate finding {finding.id}")
        seen.add(finding.id)
        if finding.severity not in severities:
            raise HardeningRegistryError(f"{finding.id} has unknown severity {finding.severity}")
        findings.append(finding)

    covered = {item.issue for item in findings}
    if covered != EXPECTED_ISSUES:
        missing = sorted(EXPECTED_ISSUES - covered)
        extra = sorted(covered - EXPECTED_ISSUES)
        raise HardeningRegistryError(f"hardening issue set mismatch: missing={missing}, extra={extra}")

    contradictions = _load_contradictions(raw.get("contradictions"), seen)
    known_contradictions = {item.id for item in contradictions}
    for finding in findings:
        if finding.contradiction and finding.contradiction not in known_contradictions:
            raise HardeningRegistryError(f"{finding.id} references unknown contradiction {finding.contradiction}")

    return HardeningRegistry(
        version=version,
        repository=repository,
        registry_owner=_required_text(raw.get("registry_owner"), "registry_owner"),
        campaign=_required_text(raw.get("campaign"), "campaign"),
        plan=_required_text(raw.get("plan"), "plan"),
        severities=severities,
        themes=themes,
        contradictions=contradictions,
        findings=tuple(findings),
    )


def render_markdown_table(registry: HardeningRegistry, issue: int) -> str:
    rows = [
        "| Finding | Label | Severity | Theme | Where the fix lands |",
        "|---|---|---|---|---|",
    ]
    for finding in registry.for_issue(issue):
        paths = ", ".join(f"`{item}`" for item in finding.files) or "_unattributed_"
        marker = " ⟲" if finding.reranked else ""
        rows.append(f"| {finding.id} | {finding.priority} | {finding.severity}{marker} | {finding.theme} | {paths} |")
    return "\n".join(rows)


def managed_issue_block(registry: HardeningRegistry, issue: int) -> str:
    findings = registry.for_issue(issue)
    if not findings:
        raise HardeningRegistryError(f"issue #{issue} has no registered findings")
    reranked = [item for item in findings if item.reranked]
    blocked = sorted({item.blocked_by_phase for item in findings if item.blocked_by_phase})
    themes = sorted({item.theme for item in findings})
    return "\n".join(
        (
            START_MARKER,
            "## HOSPES managed hardening contract",
            "",
            "The findings above remain in force verbatim. This generated block adds the "
            "attribution, severity re-anchoring, and sequencing contract each one is held to.",
            "",
            f"- Registry: `{registry.registry_owner}` (version {registry.version})",
            f"- Plan: `{registry.plan}`",
            f"- Themes present: {', '.join(themes)}",
            f"- Re-anchored severities (⟲): {len(reranked)} of {len(findings)}",
            f"- Blocked on plan phase: {', '.join(str(item) for item in blocked) or 'none'}",
            "",
            render_markdown_table(registry, issue),
            "",
            "Severity is this registry's own anchoring; the P-label is recorded as-found "
            "and never edited. Counts here describe this issue only — campaign totals are "
            "derived by `scripts/check_hardening.py`, never stored.",
            END_MARKER,
        )
    )


def replace_managed_block(value: str, block: str) -> str:
    """Swap this registry's managed block, bound to the hardening markers."""
    return _replace_managed_block(value, block, start_marker=START_MARKER, end_marker=END_MARKER)


__all__ = [
    "ALLOWED_FINDING_KEYS",
    "Contradiction",
    "END_MARKER",
    "EXPECTED_ISSUES",
    "Finding",
    "HardeningRegistry",
    "HardeningRegistryError",
    "START_MARKER",
    "Theme",
    "load_registry",
    "managed_issue_block",
    "render_markdown_table",
    "replace_managed_block",
]
