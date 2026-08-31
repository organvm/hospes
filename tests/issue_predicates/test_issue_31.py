"""Executable completion predicate for HOSPES issue #31.

Close condition: the exact eight-question interactive and JSON wizard produces
complete editable config, validates and demos, reruns safely, and gates GitHub
creation on authorization.

Required surfaces: cli, security, documentation, receipts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from hospes import completion_registry, dna, onboarding, pipeline, privacy
from hospes.__main__ import main


ROOT = Path(__file__).resolve().parents[2]

ANSWERS = {
    "show_name": "Issue 31 Show",
    "host_names": "Ari, Anthony",
    "recording_cities": "LA, NYC, Austin",
    "show_format": "conversation",
    "primary_format": "both",
    "partnership_type": "co-host",
    "notification_email_ref": "credential://hospes/operator-mail",
    "github_repo_name": "issue-31-show",
}
SHOW_ID = "issue-31-show"
AUTHORIZED_BY = "anthony_operator"
AUTHORIZATION_REF = "receipt://hospes/github-repo-create/issue-31"


class RecordingGithubAdapter:
    """A `gh` stand-in that records every invocation and never touches GitHub."""

    def __init__(self, *, available: bool = True, returncode: int = 0) -> None:
        self._available = available
        self._returncode = returncode
        self.calls: list[dict[str, Any]] = []

    def available(self) -> bool:
        return self._available

    def create(self, *, repo_name: str, root: Path, visibility: str = "private") -> dict[str, Any]:
        argv = ["gh", "repo", "create", repo_name, f"--{visibility}"]
        self.calls.append({"repo_name": repo_name, "root": Path(root), "argv": argv})
        return {"argv": argv, "returncode": self._returncode, "detail": "recorded"}


def _answers(**overrides: Any) -> dict[str, Any]:
    merged = dict(ANSWERS)
    merged.update(overrides)
    return merged


def _authorization() -> onboarding.GithubAuthorization:
    return onboarding.GithubAuthorization(
        authorized_by=AUTHORIZED_BY,
        authorization_ref=AUTHORIZATION_REF,
    )


def _init(root: Path, **kwargs: Any) -> dict[str, Any]:
    answers = kwargs.pop("answers", None) or _answers()
    return onboarding.init_workspace(root, answers, **kwargs)


def _audit_records(root: Path) -> list[dict[str, Any]]:
    log = root / "out" / "audit.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "out" not in path.relative_to(root).parts
    }


# ---------------------------------------------------------------------------
# The exact eight questions — interactive and JSON
# ---------------------------------------------------------------------------


def test_wizard_asks_exactly_eight_questions_in_one_declared_order() -> None:
    assert len(onboarding.QUESTIONS) == 8
    assert onboarding.ANSWER_KEYS == (
        "show_name",
        "host_names",
        "recording_cities",
        "show_format",
        "primary_format",
        "partnership_type",
        "notification_email_ref",
        "github_repo_name",
    )
    # Seven required, exactly one optional: the GitHub repository name.
    assert onboarding.REQUIRED_ANSWER_KEYS == onboarding.ANSWER_KEYS[:7]
    assert onboarding.OPTIONAL_ANSWER_KEYS == ("github_repo_name",)

    prompts = dict(onboarding.QUESTIONS)
    for city in onboarding.SUGGESTED_CITIES:
        assert city in prompts["recording_cities"]
    for choice in onboarding.SHOW_FORMATS:
        assert choice in prompts["show_format"]
    for choice in onboarding.PRIMARY_FORMATS:
        assert choice in prompts["primary_format"]
    for choice in onboarding.PARTNERSHIP_TYPES:
        assert choice in prompts["partnership_type"]
    assert "optional" in prompts["github_repo_name"].lower()


def test_interactive_and_json_forms_ask_for_and_produce_the_same_thing(tmp_path: Path) -> None:
    asked: list[str] = []
    replies = iter(ANSWERS[key] for key in onboarding.ANSWER_KEYS)

    def _input(prompt: str) -> str:
        asked.append(prompt)
        return next(replies)

    interactive_root = tmp_path / "interactive"
    interactive = onboarding.interactive_init(interactive_root, _input, demo=False)

    # The wizard asked eight questions, in declared order, and nothing else.
    assert len(asked) == 8
    assert [prompt.rstrip(": ") for prompt in asked] == [prompt for _, prompt in onboarding.QUESTIONS]

    json_root = tmp_path / "json"
    from_json = _init(json_root, demo=False)

    assert interactive["show_id"] == from_json["show_id"] == SHOW_ID
    assert interactive["questions"] == from_json["questions"] == list(onboarding.ANSWER_KEYS)
    assert _snapshot(interactive_root) == _snapshot(json_root)


def test_a_ninth_answer_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    with pytest.raises(onboarding.OnboardingError, match="unknown onboarding answers"):
        _init(tmp_path / "ws", answers=_answers(rss_feed_url="https://example.invalid/feed.xml"))
    # A typo'd key is an unknown key, not a silent default.
    typo = _answers()
    typo["show_fromat"] = typo.pop("show_format")
    with pytest.raises(onboarding.OnboardingError, match="show_fromat"):
        _init(tmp_path / "typo", answers=typo)
    assert not (tmp_path / "ws").exists()
    assert not (tmp_path / "typo").exists()


def test_required_answers_and_vocabularies_are_enforced(tmp_path: Path) -> None:
    for key in onboarding.REQUIRED_ANSWER_KEYS:
        with pytest.raises(onboarding.OnboardingError, match="missing onboarding answers"):
            _init(tmp_path / f"missing-{key}", answers=_answers(**{key: "   "}))
    for key, bad in (
        ("show_format", "podcast"),
        ("primary_format", "film"),
        ("partnership_type", "syndicate"),
    ):
        with pytest.raises(onboarding.OnboardingError, match=key):
            _init(tmp_path / f"bad-{key}", answers=_answers(**{key: bad}))
    for key in ("host_names", "recording_cities"):
        with pytest.raises(onboarding.OnboardingError, match="at least one entry"):
            _init(tmp_path / f"empty-{key}", answers=_answers(**{key: " , , "}))
    # The optional answer stays optional.
    result = _init(tmp_path / "no-repo", answers=_answers(github_repo_name=""), demo=False)
    assert result["github"]["status"] == onboarding.GITHUB_NOT_REQUESTED


# ---------------------------------------------------------------------------
# security — private contact data never becomes committed configuration
# ---------------------------------------------------------------------------


def test_notification_answer_must_be_an_opaque_credential_reference(tmp_path: Path) -> None:
    with pytest.raises(onboarding.OnboardingError, match="email-like content"):
        _init(tmp_path / "raw-email", answers=_answers(notification_email_ref="owner@example.com"))
    with pytest.raises(onboarding.OnboardingError, match="opaque credential-wall reference"):
        _init(tmp_path / "plain", answers=_answers(notification_email_ref="operator"))
    assert not (tmp_path / "raw-email").exists()

    root = tmp_path / "ws"
    _init(root, demo=False)
    notifications = yaml.safe_load((root / "config" / "notifications.yaml").read_text(encoding="utf-8"))
    assert notifications["operator_email_credential_ref"] == ANSWERS["notification_email_ref"]
    assert notifications["delivery"] == "draft_only"
    # No wizard-authored document carries private contact data.
    for relative in (
        Path("config") / "notifications.yaml",
        Path("config") / "brand.yaml",
        Path("config") / "analytics.yaml",
        Path("config") / "shows" / f"{SHOW_ID}.yaml",
        Path("config") / "partnerships" / f"{SHOW_ID}.yaml",
        Path("dna") / f"{SHOW_ID}.show.yaml",
    ):
        assert privacy.contact_kind((root / relative).read_text(encoding="utf-8")) is None, relative


def test_github_repository_name_cannot_be_read_as_a_flag_or_a_path(tmp_path: Path) -> None:
    for hostile in ("--private", "../escape", "owner/repo", "repo name", "repo;rm -rf /"):
        with pytest.raises(onboarding.OnboardingError, match="plain GitHub repository name"):
            _init(tmp_path / "hostile", answers=_answers(github_repo_name=hostile))
    assert not (tmp_path / "hostile").exists()


# ---------------------------------------------------------------------------
# complete, editable configuration
# ---------------------------------------------------------------------------


def test_wizard_produces_complete_editable_configuration(tmp_path: Path) -> None:
    root = tmp_path / "new-podcast"
    result = _init(root, demo=False)

    for relative in (
        f"dna/{SHOW_ID}.show.yaml",
        f"config/shows/{SHOW_ID}.yaml",
        f"config/partnerships/{SHOW_ID}.yaml",
        "config/brand.yaml",
        "config/analytics.yaml",
        "config/notifications.yaml",
        "config/runtime.yaml",
        "data/pipeline.csv",
    ):
        assert (root / relative).is_file(), relative
        assert relative in result["generated"]

    show_dna = dna.validate_file(root / "dna" / f"{SHOW_ID}.show.yaml")
    assert show_dna.errors == []
    assert show_dna.title == ANSWERS["show_name"]
    assert show_dna.recording_cities == ["LA", "NYC", "Austin"]
    document = yaml.safe_load((root / "dna" / f"{SHOW_ID}.show.yaml").read_text(encoding="utf-8"))
    assert document["show"]["hosts"] == ["Ari", "Anthony"]
    assert document["show"]["primary_format"] == "both"
    assert document["show"]["partnership_type"] == "co-host"
    assert document["format"] == "conversation"

    show = yaml.safe_load((root / "config" / "shows" / f"{SHOW_ID}.yaml").read_text(encoding="utf-8"))
    assert show["show_id"] == SHOW_ID
    assert show["dna_ref"] == f"dna/{SHOW_ID}.show.yaml"
    # Safe defaults: operator-mediated intake, draft-only outbound.
    assert show["modes"] == {"guest_interaction": "operator_packet", "outbound": "draft_only"}

    brand = yaml.safe_load((root / "config" / "brand.yaml").read_text(encoding="utf-8"))
    assert brand["show_name"] == ANSWERS["show_name"]
    partnership = yaml.safe_load((root / "config" / "partnerships" / f"{SHOW_ID}.yaml").read_text(encoding="utf-8"))
    assert partnership["partnership_key"] == SHOW_ID


def test_pipeline_template_carries_the_canonical_header_the_validator_requires(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root, demo=False)
    template = root / "data" / "pipeline.csv"
    header = template.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == pipeline.PIPELINE_HEADERS
    # Every column the candidate validator requires is present, so an operator's
    # very first hand-typed row validates instead of failing on a missing column.
    for column in pipeline.REQUIRED_FIELD_TO_COLUMN.values():
        assert column in header
    assert pipeline.load_candidates(template) == []


# ---------------------------------------------------------------------------
# validates and demos
# ---------------------------------------------------------------------------


def test_one_run_validates_and_demos_the_new_workspace(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    result = _init(root)

    assert result["validation"] == {"ok": True, "errors": []}
    assert onboarding.validate_workspace(root) == []

    assert result["demo"]["ran"] is True
    assert result["demo"]["error"] is None
    summary = result["demo"]["summary"]
    assert summary["candidates_loaded"] > 0
    assert summary["errors"] == 0
    assert summary["drafts"] >= 1
    # The demo ran scoped to the new workspace, not the engine checkout.
    assert Path(summary["packet_sources"]).is_relative_to(root.resolve())
    assert (root / "out" / "drafts").is_dir()
    assert (root / "out" / "briefs").is_dir()

    assert result["ok"] is True
    assert result["next"] == onboarding.NEXT_COMMAND == "hospes demo --open"


def test_validation_failure_is_reported_and_stops_the_run(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root, demo=False)
    (root / "dna" / f"{SHOW_ID}.show.yaml").write_text("show: {}\n", encoding="utf-8")

    adapter = RecordingGithubAdapter()
    result = _init(
        root,
        merge=True,
        github_authorization=_authorization(),
        github_adapter=adapter,
    )
    assert result["validation"]["ok"] is False
    assert result["validation"]["errors"]
    assert result["ok"] is False
    # A broken workspace never demos and never reaches the network.
    assert result["demo"]["ran"] is False
    assert "validation failed" in result["demo"]["error"]
    assert result["github"]["status"] == onboarding.GITHUB_BLOCKED
    assert result["github"]["executed"] is False
    assert adapter.calls == []


def test_validate_workspace_catches_each_generated_contract(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root, demo=False)
    show_path = root / "config" / "shows" / f"{SHOW_ID}.yaml"
    show = yaml.safe_load(show_path.read_text(encoding="utf-8"))

    show_path.write_text(yaml.safe_dump({**show, "dna_ref": "dna/absent.show.yaml"}), encoding="utf-8")
    assert any("does not resolve" in error for error in onboarding.validate_workspace(root))

    show_path.write_text(
        yaml.safe_dump({**show, "modes": {"guest_interaction": "operator_packet", "outbound": "send_now"}}),
        encoding="utf-8",
    )
    assert any("outbound must be" in error for error in onboarding.validate_workspace(root))

    show_path.write_text(yaml.safe_dump(show), encoding="utf-8")
    assert onboarding.validate_workspace(root) == []

    notifications = root / "config" / "notifications.yaml"
    notifications.write_text(
        yaml.safe_dump({"version": 1, "operator_email_credential_ref": "owner@example.com"}),
        encoding="utf-8",
    )
    assert any("opaque credential-wall reference" in error for error in onboarding.validate_workspace(root))


# ---------------------------------------------------------------------------
# reruns safely
# ---------------------------------------------------------------------------


def test_default_rerun_is_refused_and_merge_is_non_destructive_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    first = _init(root, demo=False)
    assert first["generated"]
    assert first["preserved"] == []

    edited = root / "config" / "brand.yaml"
    edited.write_text(
        yaml.safe_dump({"version": 1, "show_name": "Operator Edited", "primary_color": "#123456"}),
        encoding="utf-8",
    )
    before = _snapshot(root)

    with pytest.raises(onboarding.OnboardingError, match="already exist"):
        _init(root, demo=False)
    assert _snapshot(root) == before, "a refused rerun must not touch a single byte"

    merged = _init(root, merge=True, demo=False)
    assert merged["generated"] == []
    assert merged["preserved"]
    assert _snapshot(root) == before, "merge must never rewrite an operator's edit"

    again = _init(root, merge=True, demo=False)
    assert again["generated"] == []
    assert _snapshot(root) == before
    # The merge rerun re-proves the workspace rather than assuming it.
    assert again["validation"]["ok"] is True

    root.joinpath("config", "analytics.yaml").unlink()
    filled = _init(root, merge=True, demo=False)
    assert filled["generated"] == ["config/analytics.yaml"]
    assert root.joinpath("config", "analytics.yaml").is_file()


# ---------------------------------------------------------------------------
# gates GitHub creation on authorization
# ---------------------------------------------------------------------------


def test_github_creation_is_blocked_without_an_authorization_receipt(tmp_path: Path) -> None:
    adapter = RecordingGithubAdapter()
    result = _init(tmp_path / "ws", demo=False, github_adapter=adapter)
    github = result["github"]
    assert github["status"] == onboarding.GITHUB_BLOCKED
    assert github["repo_name"] == ANSWERS["github_repo_name"]
    assert github["executed"] is False
    assert "authorization receipt" in github["reason"]
    assert adapter.calls == [], "an unauthorized request must never reach the adapter"
    # A blocked gate is not a failed run: the workspace itself is complete.
    assert result["validation"]["ok"] is True


def test_authorized_creation_without_the_github_cli_is_visibly_unconfigured(tmp_path: Path) -> None:
    adapter = RecordingGithubAdapter(available=False)
    result = _init(
        tmp_path / "ws",
        demo=False,
        github_authorization=_authorization(),
        github_adapter=adapter,
    )
    github = result["github"]
    assert github["status"] == onboarding.GITHUB_UNCONFIGURED
    assert github["executed"] is False
    assert github["authorized_by"] == AUTHORIZED_BY
    assert github["authorization_ref"] == AUTHORIZATION_REF
    assert adapter.calls == [], "an unavailable CLI is reported, never silently substituted"


def test_authorized_creation_runs_exactly_once_with_a_safe_argv(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    adapter = RecordingGithubAdapter()
    result = _init(
        root,
        demo=False,
        github_authorization=_authorization(),
        github_adapter=adapter,
    )
    github = result["github"]
    assert github["status"] == onboarding.GITHUB_CREATED
    assert github["executed"] is True
    assert github["returncode"] == 0
    assert github["authorized_by"] == AUTHORIZED_BY
    assert github["authorization_ref"] == AUTHORIZATION_REF
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["repo_name"] == ANSWERS["github_repo_name"]
    assert call["root"] == root.resolve()
    assert call["argv"] == ["gh", "repo", "create", ANSWERS["github_repo_name"], "--private"]

    failing = RecordingGithubAdapter(returncode=1)
    failed = _init(
        tmp_path / "failing",
        demo=False,
        github_authorization=_authorization(),
        github_adapter=failing,
    )
    assert failed["github"]["status"] == onboarding.GITHUB_FAILED
    assert failed["github"]["executed"] is True
    assert failed["ok"] is False


def test_authorization_requires_both_halves_and_an_opaque_reference() -> None:
    with pytest.raises(onboarding.OnboardingError, match="authorizing human"):
        onboarding.GithubAuthorization(authorized_by="  ", authorization_ref=AUTHORIZATION_REF)
    for bad in ("", "yes", "https://github.com/organvm/hospes", "receipt://ab"):
        with pytest.raises(onboarding.OnboardingError, match="opaque receipt reference"):
            onboarding.GithubAuthorization(authorized_by=AUTHORIZED_BY, authorization_ref=bad)
    authorization = _authorization()
    assert authorization.as_receipt() == {
        "authorized_by": AUTHORIZED_BY,
        "authorization_ref": AUTHORIZATION_REF,
    }


def test_default_adapter_probes_the_cli_and_builds_argv_never_a_shell_string(tmp_path: Path) -> None:
    adapter = onboarding.GithubCliAdapter()
    assert isinstance(adapter.available(), bool)
    gate = onboarding.gate_github_repository(
        repo_name="",
        root=tmp_path,
        authorization=_authorization(),
        adapter=adapter,
    )
    # No repository requested: the default adapter is never consulted at all.
    assert gate["status"] == onboarding.GITHUB_NOT_REQUESTED
    assert gate["executed"] is False


# ---------------------------------------------------------------------------
# receipts
# ---------------------------------------------------------------------------


def test_every_run_appends_its_receipts_to_the_workspace_audit_log(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    result = _init(
        root,
        demo=False,
        github_authorization=_authorization(),
        github_adapter=RecordingGithubAdapter(),
    )
    assert result["receipt_log"] == str(root.resolve() / "out" / "audit.log")
    assert result["receipts"] == [
        onboarding.RECEIPT_INITIALIZED,
        onboarding.RECEIPT_VALIDATED,
        onboarding.RECEIPT_DEMO,
        onboarding.RECEIPT_GITHUB,
    ]

    records = _audit_records(root)
    assert [record["action"] for record in records] == result["receipts"]
    by_action = {record["action"]: record for record in records}
    assert by_action[onboarding.RECEIPT_INITIALIZED]["show_id"] == SHOW_ID
    assert by_action[onboarding.RECEIPT_VALIDATED]["ok"] is True
    assert by_action[onboarding.RECEIPT_DEMO]["ran"] is False
    github_receipt = by_action[onboarding.RECEIPT_GITHUB]
    assert github_receipt["status"] == onboarding.GITHUB_CREATED
    assert github_receipt["authorized_by"] == AUTHORIZED_BY
    assert github_receipt["authorization_ref"] == AUTHORIZATION_REF
    assert github_receipt["executed"] is True
    for record in records:
        assert record["ts"].endswith("+00:00")

    # The log is append-only: a second run adds, never rewrites.
    _init(root, merge=True, demo=False)
    assert [record["action"] for record in _audit_records(root)] == result["receipts"] * 2


def test_failure_detail_is_returned_to_the_caller_but_kept_out_of_the_durable_log(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    result = _init(
        root,
        demo=False,
        github_authorization=_authorization(),
        github_adapter=RecordingGithubAdapter(returncode=1),
    )
    assert result["github"]["detail"] == "recorded"
    github_receipt = [r for r in _audit_records(root) if r["action"] == onboarding.RECEIPT_GITHUB][0]
    assert "detail" not in github_receipt


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def _write_answers(path: Path, **overrides: Any) -> Path:
    path.write_text(json.dumps(_answers(**overrides)), encoding="utf-8")
    return path


def test_cli_init_runs_the_wizard_and_prints_the_next_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = _write_answers(tmp_path / "answers.json")
    root = tmp_path / "new-podcast"
    exit_code = main(["init", "--root", str(root), "--answers", str(answers)])
    captured = capsys.readouterr()
    assert exit_code == 0

    payload = json.loads(captured.out.splitlines()[0])
    assert payload["show_id"] == SHOW_ID
    assert payload["validation"]["ok"] is True
    assert payload["demo"]["ran"] is True
    assert payload["questions"] == list(onboarding.ANSWER_KEYS)
    assert onboarding.NEXT_MESSAGE in captured.out
    assert "Run `hospes demo --open` to start" in captured.out
    # The withheld GitHub repository is announced, not hidden.
    assert "github blocked" in captured.err
    assert (root / "data" / "pipeline.csv").is_file()
    assert (root / "out" / "audit.log").is_file()


def test_cli_init_rejects_a_half_declared_authorization(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    answers = _write_answers(tmp_path / "answers.json")
    for flag, value in (
        ("--github-authorized-by", AUTHORIZED_BY),
        ("--github-authorization-ref", AUTHORIZATION_REF),
    ):
        exit_code = main(
            ["init", "--root", str(tmp_path / f"half{flag}"), "--answers", str(answers), flag, value, "--skip-demo"]
        )
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "requires both" in captured.err


def test_cli_init_creates_only_under_a_complete_authorization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = RecordingGithubAdapter()
    monkeypatch.setattr(onboarding, "GithubCliAdapter", lambda: adapter)
    answers = _write_answers(tmp_path / "answers.json")
    root = tmp_path / "authorized"
    exit_code = main(
        [
            "init",
            "--root",
            str(root),
            "--answers",
            str(answers),
            "--skip-demo",
            "--github-authorized-by",
            AUTHORIZED_BY,
            "--github-authorization-ref",
            AUTHORIZATION_REF,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out.splitlines()[0])
    assert payload["github"]["status"] == onboarding.GITHUB_CREATED
    assert len(adapter.calls) == 1


def test_cli_init_refuses_a_destructive_rerun_and_accepts_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = _write_answers(tmp_path / "answers.json")
    root = tmp_path / "ws"
    assert main(["init", "--root", str(root), "--answers", str(answers), "--skip-demo"]) == 0
    capsys.readouterr()

    assert main(["init", "--root", str(root), "--answers", str(answers), "--skip-demo"]) == 2
    assert "already exist" in capsys.readouterr().err

    assert main(["init", "--root", str(root), "--answers", str(answers), "--skip-demo", "--merge"]) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[0])
    assert payload["generated"] == []
    assert payload["preserved"]


def test_cli_init_reports_an_unusable_answer_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path / "ws"), "--answers", str(broken)]) == 2
    assert "[init]" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# documentation + registry contract
# ---------------------------------------------------------------------------


def test_documentation_surface_explains_the_wizard() -> None:
    documentation = (ROOT / "docs" / "onboarding-wizard.md").read_text(encoding="utf-8")
    for marker in (
        "hospes init",
        "--answers",
        "--merge",
        "--github-authorized-by",
        "--github-authorization-ref",
        "credential://",
        "not_requested",
        "blocked",
        "unconfigured",
        "created",
        "out/audit.log",
        "PIPELINE_HEADERS",
    ):
        assert marker in documentation, marker
    for key in onboarding.ANSWER_KEYS:
        assert key in documentation, key
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/onboarding-wizard.md" in readme
    assert "hospes init" in readme


def test_registry_contract_for_issue_31() -> None:
    issue = completion_registry.load_registry().issue(31)
    assert issue.predicate == "python -m pytest tests/issue_predicates/test_issue_31.py -q"
    assert set(issue.required_surfaces) == {"cli", "security", "documentation", "receipts"}
    assert issue.receipt_owner == "github://organvm/hospes/issues/31"
    assert "substrate.storage_migration" in issue.dependencies
