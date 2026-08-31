"""Non-technical, repeatable workspace bootstrap — the ``hospes init`` wizard.

The wizard asks exactly eight questions, in one fixed order, and the JSON
(``--answers``) form accepts exactly the same eight keys. Seven are required;
``github_repo_name`` is optional. A ninth key, or a missing seventh answer, is
an error rather than a silently-defaulted value: an operator who cannot read
YAML cannot audit what was guessed on their behalf.

The wizard is a full loop, not a file dropper:

1. **generate** the editable configuration (DNA, show, partnership, brand,
   analytics, notifications, and a canonical-header pipeline template),
2. **validate** the generated workspace with the same validators the engine
   uses (``dna.validate_all`` over the workspace ``dna/`` tree, the show
   contract's own id/mode/ref rules, and the shared no-secrets guard),
3. **demo** the workspace by running the real demo pipeline scoped to the new
   root, and
4. **gate** GitHub repository creation behind an explicit human authorization
   receipt.

Step 4 is the safety boundary. ``AGENTS.md`` requires that live outbound work
carry a human authorization receipt and that a missing external account be
recorded as visible ``unconfigured``/``blocked`` state rather than hidden or
silently substituted, so the gate has four honest outcomes — ``not_requested``,
``blocked`` (asked for, never authorized), ``unconfigured`` (authorized, but the
GitHub CLI is not on this host), and ``created``/``failed`` (authorized and
actually attempted). Nothing reaches the network without an authorization.

Every run appends its receipts to the workspace's own append-only audit log
(``<root>/out/audit.log``) through :mod:`hospes.audit`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from . import audit, branding, configuration, dna, pipeline, privacy


QUESTIONS = (
    ("show_name", "Show name"),
    ("host_names", "Host names (comma-separated)"),
    ("recording_cities", "Recording cities (LA/NYC/Austin/custom, comma-separated)"),
    ("show_format", "Show format (interview/narrative/conversation)"),
    ("primary_format", "Primary format (audio/video/both)"),
    ("partnership_type", "Partnership type (solo/co-host/network)"),
    (
        "notification_email_ref",
        "Email for operator notifications (opaque credential reference, e.g. credential://mail/operator)",
    ),
    ("github_repo_name", "GitHub repo name (optional)"),
)
#: The exact eight answer keys, in question order.
ANSWER_KEYS = tuple(key for key, _ in QUESTIONS)
#: The seven answers a workspace cannot be built without.
REQUIRED_ANSWER_KEYS = ANSWER_KEYS[:-1]
#: The single optional answer.
OPTIONAL_ANSWER_KEYS = ANSWER_KEYS[-1:]

SHOW_FORMATS = ("interview", "narrative", "conversation")
PRIMARY_FORMATS = ("audio", "video", "both")
PARTNERSHIP_TYPES = ("solo", "co-host", "network")
#: Suggested cities; the answer is free text so "custom" stays a real option.
SUGGESTED_CITIES = ("LA", "NYC", "Austin")

_CHOICES: dict[str, tuple[str, ...]] = {
    "show_format": SHOW_FORMATS,
    "primary_format": PRIMARY_FORMATS,
    "partnership_type": PARTNERSHIP_TYPES,
}

# Mirrors configuration._show_config_path: a show id becomes part of a route and
# a filename, so it is constrained at the point it is derived, not later.
_SHOW_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62})$")
# GitHub's own repository-name shape. The name is passed as one argv element to
# `gh` (never a shell string), and it is still constrained so it can never be
# read as a flag or a path.
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_AUTHORIZATION_REF = re.compile(r"^(?:receipt|authorization)://[^\s]{3,296}$")

GITHUB_NOT_REQUESTED = "not_requested"
GITHUB_BLOCKED = "blocked"
GITHUB_UNCONFIGURED = "unconfigured"
GITHUB_CREATED = "created"
GITHUB_FAILED = "failed"

RECEIPT_INITIALIZED = "onboarding.initialized"
RECEIPT_VALIDATED = "onboarding.validated"
RECEIPT_DEMO = "onboarding.demo"
RECEIPT_GITHUB = "onboarding.github"

NEXT_COMMAND = "hospes demo --open"
NEXT_MESSAGE = "Run `hospes demo --open` to start"


class OnboardingError(ValueError):
    """Raised when the wizard's answers or generated workspace are unusable."""


@dataclass(frozen=True)
class GithubAuthorization:
    """One human's recorded authorization to create a real GitHub repository."""

    authorized_by: str
    authorization_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.authorized_by, str) or not self.authorized_by.strip():
            raise OnboardingError("github authorization requires the authorizing human's identifier")
        if not isinstance(self.authorization_ref, str) or not _AUTHORIZATION_REF.fullmatch(self.authorization_ref):
            raise OnboardingError(
                "github authorization_ref must be an opaque receipt reference (receipt:// or authorization://)"
            )

    def as_receipt(self) -> dict[str, str]:
        return {"authorized_by": self.authorized_by.strip(), "authorization_ref": self.authorization_ref}


class GithubCliAdapter:
    """The default ``gh`` adapter: probe first, and never run unauthorized.

    ``available()`` is a pure PATH probe so the wizard can report a visible
    ``unconfigured`` state instead of failing opaquely, and ``create()`` builds
    an argv list — never a shell string — so no answer is ever interpolated
    into a command line.
    """

    name = "github_cli"

    def available(self) -> bool:
        return shutil.which("gh") is not None

    def create(self, *, repo_name: str, root: Path, visibility: str = "private") -> dict[str, Any]:
        argv = ["gh", "repo", "create", repo_name, f"--{visibility}"]
        if (Path(root) / ".git").is_dir():
            argv += ["--source", str(root), "--push"]
        try:
            completed = subprocess.run(
                argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"argv": argv, "returncode": 1, "detail": f"gh repo create failed: {exc}"}
        detail = (completed.stderr or completed.stdout or "").strip()
        return {"argv": argv, "returncode": completed.returncode, "detail": detail[:500]}


def _slug(value: str) -> str:
    return "-".join("".join(char.lower() if char.isalnum() else " " for char in value).split()) or "show"


def _show_id_for(show_name: str) -> str:
    """Derive one route-safe, filename-safe show id from a free-text show name."""
    candidate = _slug(show_name)[:63].rstrip("-_") or "show"
    if not _SHOW_ID.fullmatch(candidate):
        raise OnboardingError(
            f"show name {show_name!r} does not yield a usable show id; use letters, digits, spaces, or dashes"
        )
    return candidate


def _pipeline_template() -> bytes:
    """The canonical pipeline header, so an operator's first row validates.

    A short hand-written header shipped columns the candidate validator
    requires (``contact_route``, ``preferred_city``, ``social_cost_1_5``, …)
    and so the very first row a new operator typed could not pass
    ``hospes demo``. The engine's own header order is the template.
    """
    return (",".join(pipeline.PIPELINE_HEADERS) + "\n").encode("utf-8")


def normalize_answers(answers: Mapping[str, Any]) -> dict[str, str]:
    """Validate the exact eight answers and return them normalized.

    Unknown keys are rejected rather than ignored: a typo'd key would otherwise
    silently fall back to a guessed value the operator never chose.
    """
    if not isinstance(answers, Mapping):
        raise OnboardingError("onboarding answers must be a mapping of the eight question keys")
    unknown = sorted(str(key) for key in answers if str(key) not in ANSWER_KEYS)
    if unknown:
        raise OnboardingError(f"unknown onboarding answers: {unknown}; the wizard asks exactly {list(ANSWER_KEYS)}")
    missing = sorted(key for key in REQUIRED_ANSWER_KEYS if not str(answers.get(key, "")).strip())
    if missing:
        raise OnboardingError(f"missing onboarding answers: {missing}")
    normalized = {key: str(answers.get(key, "") or "").strip() for key in ANSWER_KEYS}
    for key, allowed in _CHOICES.items():
        value = normalized[key].lower()
        if value not in allowed:
            raise OnboardingError(f"{key} must be one of {list(allowed)}; got {normalized[key]!r}")
        normalized[key] = value
    reference = normalized["notification_email_ref"]
    if not configuration.CREDENTIAL_REF.fullmatch(reference):
        kind = privacy.contact_kind(reference) or "a literal value"
        raise OnboardingError(
            f"notification_email_ref must be an opaque credential-wall reference (credential:// or op://), never {kind}"
        )
    if normalized["github_repo_name"] and not _GITHUB_REPO.fullmatch(normalized["github_repo_name"]):
        raise OnboardingError(
            "github_repo_name must be a plain GitHub repository name (letters, digits, '.', '_', '-')"
        )
    for key in ("host_names", "recording_cities"):
        if not _split(normalized[key]):
            raise OnboardingError(f"{key} must name at least one entry")
    return normalized


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _managed_files(answers: Mapping[str, str], show_id: str) -> dict[Path, bytes]:
    """Every file the wizard owns, as bytes, keyed by workspace-relative path."""
    cities = _split(answers["recording_cities"])
    hosts = _split(answers["host_names"])
    tenant_id = _slug(answers.get("github_repo_name") or "hospes")[:63].rstrip("-_") or "hospes"
    dna_document = {
        "version": 1,
        "show": {
            "id": show_id,
            "title": answers["show_name"],
            "status": "active",
            "primary_format": answers["primary_format"],
            "recording_cities": cities,
            "hosts": hosts,
            "partnership_type": answers["partnership_type"],
        },
        "format_engine": {
            "fixed": ["The Claim", "The Stress Test", "The Artifact"],
            "rotating": ["Receipts", "Object Lesson"],
        },
        # Keep these convenience fields for operators who inspect the file
        # directly; the canonical validator reads the nested show contract.
        "show_id": show_id,
        "show_name": answers["show_name"],
        "format": answers["show_format"],
    }
    show_document = {
        "version": 1,
        "tenant_id": tenant_id,
        "show_id": show_id,
        "label": answers["show_name"],
        "enabled": True,
        "dna_ref": f"dna/{show_id}.show.yaml",
        "modes": {"guest_interaction": "operator_packet", "outbound": "draft_only"},
        "roles": ["producer", "relationship_owner", "editorial_owner"],
        "provider_precedence": {},
    }
    partnership_document = {
        "partnership_key": show_id,
        "label": answers["show_name"],
        "purpose": "Operator-managed guest operations",
        "items": [],
    }
    brand_document = {**branding.DEFAULT_BRAND, "show_name": answers["show_name"]}
    analytics_document = {
        "version": 1,
        "fetch_schedule": "manual_or_operator_authorized",
        "trend_window": 12,
        "providers": {
            "spotify_creator_csv": {"enabled": True, "credential_ref": None, "import_path": None},
            "youtube_analytics": {
                "enabled": False,
                "credential_ref": "credential://hospes/youtube-analytics",
                "import_path": None,
            },
        },
    }
    notifications_document = {
        "version": 1,
        "operator_email_credential_ref": answers["notification_email_ref"],
        "delivery": "draft_only",
    }

    def _yaml(document: Mapping[str, Any]) -> bytes:
        return yaml.safe_dump(dict(document), sort_keys=False).encode("utf-8")

    managed: dict[Path, bytes] = {}
    for source in sorted(configuration.CONFIG_DIR.iterdir()):
        if source.is_file():
            managed[Path("config") / source.name] = source.read_bytes()
    managed.update(
        {
            Path("dna") / f"{show_id}.show.yaml": _yaml(dna_document),
            Path("config") / "shows" / f"{show_id}.yaml": _yaml(show_document),
            Path("config") / "partnerships" / f"{show_id}.yaml": _yaml(partnership_document),
            Path("config") / "brand.yaml": _yaml(brand_document),
            Path("config") / "analytics.yaml": _yaml(analytics_document),
            Path("config") / "notifications.yaml": _yaml(notifications_document),
            Path("data") / "pipeline.csv": _pipeline_template(),
        }
    )
    return managed


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover - unreadable generated file
        raise OnboardingError(f"cannot read generated file {path}: {exc}") from exc


def validate_workspace(root: str | Path) -> list[str]:
    """Validate one generated workspace in place; ``[]`` means green.

    This is the ``hospes config validate`` contract applied to an arbitrary
    root: the engine's module-level ``CONFIG_DIR`` is bound at import time, so
    a freshly-created workspace elsewhere on disk needs the checks driven from
    explicit paths.
    """
    target = Path(root).expanduser().resolve()
    errors: list[str] = []
    for relative in (
        Path("config") / "brand.yaml",
        Path("config") / "analytics.yaml",
        Path("config") / "notifications.yaml",
        Path("config") / "runtime.yaml",
        Path("data") / "pipeline.csv",
    ):
        if not (target / relative).is_file():
            errors.append(f"missing generated file {relative}")

    dna_dir = target / "dna"
    dna_results = dna.validate_all(dna_dir)
    if not dna_results:
        errors.append("workspace declares no dna/*.show.yaml document")
    for result in dna_results:
        errors.extend(result.errors)

    shows_dir = target / "config" / "shows"
    show_paths = sorted(shows_dir.glob("*.yaml")) if shows_dir.is_dir() else []
    if not show_paths:
        errors.append("workspace declares no config/shows/*.yaml document")
    for path in show_paths:
        document = _load_yaml(path)
        if not isinstance(document, dict):
            errors.append(f"{path.name}: show configuration must be a YAML object")
            continue
        try:
            configuration.assert_no_secrets(document, f"config/shows/{path.name}")
        except configuration.ConfigurationError as exc:
            errors.append(str(exc))
        show_id = str(document.get("show_id", ""))
        if show_id != path.stem or not _SHOW_ID.fullmatch(show_id):
            errors.append(f"{path.name}: show_id must be a safe identifier matching the filename")
        if not str(document.get("tenant_id", "")):
            errors.append(f"{path.name}: show configuration requires a tenant_id")
        modes = document.get("modes")
        if not isinstance(modes, dict):
            errors.append(f"{path.name}: show modes must be an object")
        else:
            if modes.get("guest_interaction") not in {"operator_packet", "portal"}:
                errors.append(f"{path.name}: guest_interaction must be operator_packet or portal")
            if modes.get("outbound") not in {"draft_only", "manual_receipt", "provider_connected"}:
                errors.append(f"{path.name}: outbound must be draft_only, manual_receipt, or provider_connected")
        dna_ref = document.get("dna_ref")
        if not isinstance(dna_ref, str) or not dna_ref.startswith("dna/"):
            errors.append(f"{path.name}: dna_ref must be a repository-relative dna/ YAML path")
        elif not (target / dna_ref).is_file():
            errors.append(f"{path.name}: dna_ref {dna_ref} does not resolve inside the workspace")

    for relative in (
        Path("config") / "brand.yaml",
        Path("config") / "analytics.yaml",
        Path("config") / "notifications.yaml",
    ):
        path = target / relative
        if not path.is_file():
            continue
        document = _load_yaml(path)
        if not isinstance(document, dict):
            errors.append(f"{relative}: must be a YAML object")
            continue
        try:
            configuration.assert_no_secrets(document, str(relative))
        except configuration.ConfigurationError as exc:
            errors.append(str(exc))

    pipeline_csv = target / "data" / "pipeline.csv"
    if pipeline_csv.is_file():
        rows = pipeline.load_candidates(pipeline_csv)
        header = (
            pipeline_csv.read_text(encoding="utf-8").splitlines()[0].split(",") if pipeline_csv.stat().st_size else []
        )
        if header != pipeline.PIPELINE_HEADERS:
            errors.append("data/example-pipeline.csv must carry the canonical candidate header")
        result = pipeline.validate_candidates(rows)
        errors.extend(
            f"data/example-pipeline.csv row {error.row_index} {error.field_name}: {error.message}" for error in result.errors
        )
    return errors


def gate_github_repository(
    *,
    repo_name: str,
    root: str | Path,
    authorization: GithubAuthorization | None = None,
    adapter: Any | None = None,
) -> dict[str, Any]:
    """Decide, and only then perform, GitHub repository creation.

    Returns the gate record. ``executed`` is ``True`` only when the adapter was
    actually invoked, which requires an authorization receipt *and* a
    configured GitHub CLI.
    """
    target = Path(root).expanduser().resolve()
    name = (repo_name or "").strip()
    if not name:
        return {
            "status": GITHUB_NOT_REQUESTED,
            "repo_name": None,
            "executed": False,
            "reason": "no GitHub repository was requested",
        }
    if not _GITHUB_REPO.fullmatch(name):
        raise OnboardingError(
            "github_repo_name must be a plain GitHub repository name (letters, digits, '.', '_', '-')"
        )
    if authorization is None:
        return {
            "status": GITHUB_BLOCKED,
            "repo_name": name,
            "executed": False,
            "reason": "GitHub repository creation requires an explicit human authorization receipt",
        }
    receipt = authorization.as_receipt()
    resolved = adapter if adapter is not None else GithubCliAdapter()
    if not resolved.available():
        return {
            "status": GITHUB_UNCONFIGURED,
            "repo_name": name,
            "executed": False,
            "reason": "the GitHub CLI is not available on this host",
            **receipt,
        }
    outcome = resolved.create(repo_name=name, root=target)
    returncode = int(outcome.get("returncode", 1))
    record = {
        "status": GITHUB_CREATED if returncode == 0 else GITHUB_FAILED,
        "repo_name": name,
        "executed": True,
        "returncode": returncode,
        "reason": (
            "authorized GitHub repository creation succeeded"
            if returncode == 0
            else "authorized GitHub repository creation failed"
        ),
        **receipt,
    }
    if returncode != 0:
        record["detail"] = str(outcome.get("detail", ""))[:500]
    return record


def _record(root: Path, action: str, **fields: Any) -> dict[str, Any]:
    return audit.append(action, log_path=root / "out" / "audit.log", **fields)


def init_workspace(
    root: str | Path,
    answers: Mapping[str, Any],
    *,
    merge: bool = False,
    github_authorization: GithubAuthorization | None = None,
    github_adapter: Any | None = None,
    demo: bool = True,
) -> dict[str, Any]:
    """Create (or non-destructively complete) one editable show workspace.

    The default rerun is refused rather than applied: managed files that
    already exist are the operator's edits, and overwriting them is the one
    unrecoverable thing this command could do. ``merge=True`` is the explicit,
    fill-the-gaps rerun; it never rewrites a byte that is already on disk.
    """
    target = Path(root).expanduser().resolve()
    normalized = normalize_answers(answers)
    show_id = _show_id_for(normalized["show_name"])
    managed = _managed_files(normalized, show_id)

    conflicts = sorted(str(path) for path in managed if (target / path).exists())
    if conflicts and not merge:
        raise OnboardingError(
            "managed onboarding files already exist; rerun with explicit non-destructive merge mode: "
            + ", ".join(conflicts)
        )
    generated: list[str] = []
    preserved: list[str] = []
    for relative, content in managed.items():
        destination = target / relative
        if destination.exists():
            preserved.append(str(relative))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        generated.append(str(relative))

    receipts: list[str] = []
    _record(
        target,
        RECEIPT_INITIALIZED,
        show_id=show_id,
        merge=merge,
        generated=len(generated),
        preserved=len(preserved),
    )
    receipts.append(RECEIPT_INITIALIZED)

    validation_errors = validate_workspace(target)
    _record(target, RECEIPT_VALIDATED, show_id=show_id, ok=not validation_errors, errors=validation_errors)
    receipts.append(RECEIPT_VALIDATED)

    demo_record: dict[str, Any] = {"ran": False, "summary": None, "error": None}
    if not demo:
        demo_record["error"] = "demo was skipped by request"
    elif validation_errors:
        demo_record["error"] = "demo was skipped because workspace validation failed"
    else:
        from .demo_runner import DemoRunnerError, run_demo

        try:
            demo_record["summary"] = run_demo(out_dir=target / "out", quiet=True, use_sample_decisions=True)
            demo_record["ran"] = True
        except (DemoRunnerError, OSError, ValueError) as exc:
            demo_record["error"] = str(exc)
    _record(target, RECEIPT_DEMO, show_id=show_id, ran=demo_record["ran"], error=demo_record["error"])
    receipts.append(RECEIPT_DEMO)

    if validation_errors:
        github = {
            "status": GITHUB_BLOCKED if normalized["github_repo_name"] else GITHUB_NOT_REQUESTED,
            "repo_name": normalized["github_repo_name"] or None,
            "executed": False,
            "reason": "GitHub repository creation is withheld while workspace validation fails",
        }
    else:
        github = gate_github_repository(
            repo_name=normalized["github_repo_name"],
            root=target,
            authorization=github_authorization,
            adapter=github_adapter,
        )
    _record(
        target,
        RECEIPT_GITHUB,
        show_id=show_id,
        status=github["status"],
        repo_name=github.get("repo_name"),
        executed=github["executed"],
        reason=github["reason"],
        authorized_by=github.get("authorized_by"),
        authorization_ref=github.get("authorization_ref"),
    )
    receipts.append(RECEIPT_GITHUB)

    return {
        "root": str(target),
        "show_id": show_id,
        "show_name": normalized["show_name"],
        "questions": list(ANSWER_KEYS),
        "generated": sorted(generated),
        "preserved": sorted(preserved),
        "merge": merge,
        "validation": {"ok": not validation_errors, "errors": validation_errors},
        "demo": demo_record,
        "github": github,
        "receipts": receipts,
        "receipt_log": str(target / "out" / "audit.log"),
        "ok": not validation_errors and (demo_record["ran"] or not demo) and github["status"] != GITHUB_FAILED,
        "next": NEXT_COMMAND,
    }


def interactive_init(
    root: str | Path,
    input_fn: Callable[[str], str] = input,
    *,
    merge: bool = False,
    github_authorization: GithubAuthorization | None = None,
    github_adapter: Any | None = None,
    demo: bool = True,
) -> dict[str, Any]:
    """Ask the eight questions in order, then run the same wizard."""
    answers = {key: input_fn(prompt + ": ").strip() for key, prompt in QUESTIONS}
    return init_workspace(
        root,
        answers,
        merge=merge,
        github_authorization=github_authorization,
        github_adapter=github_adapter,
        demo=demo,
    )


__all__ = [
    "ANSWER_KEYS",
    "GITHUB_BLOCKED",
    "GITHUB_CREATED",
    "GITHUB_FAILED",
    "GITHUB_NOT_REQUESTED",
    "GITHUB_UNCONFIGURED",
    "GithubAuthorization",
    "GithubCliAdapter",
    "NEXT_COMMAND",
    "NEXT_MESSAGE",
    "OPTIONAL_ANSWER_KEYS",
    "OnboardingError",
    "PARTNERSHIP_TYPES",
    "PRIMARY_FORMATS",
    "QUESTIONS",
    "REQUIRED_ANSWER_KEYS",
    "SHOW_FORMATS",
    "SUGGESTED_CITIES",
    "gate_github_repository",
    "init_workspace",
    "interactive_init",
    "normalize_answers",
    "validate_workspace",
]
