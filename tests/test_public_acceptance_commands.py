"""Public projection selection never falls back to private acceptance inputs."""
from pathlib import Path
import runpy
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, monkeypatch, tmp_path, args):
    main = runpy.run_path(str(ROOT / "scripts" / name))["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", [name, *args])
    return main


def test_public_ledger_is_explicit_and_requires_artifacts(monkeypatch, tmp_path):
    ledger = tmp_path / "public.md"
    artifact = tmp_path / "engine.py"
    artifact.write_text("# synthetic engine")
    ledger.write_text("| Check | Requirement | Artifact |\n|---|---|---|\n| P1 | engine | `engine.py` |\n")
    main = load_script("check_asks.py", monkeypatch, tmp_path, ["--ledger", "public.md"])
    monkeypatch.setitem(main.__globals__, "CONTENT_CHECKS", [])
    assert main() == 0
    artifact.unlink()
    assert main() == 1
    ledger.write_text("No declared artifact.\n")
    assert main() == 1


def test_historical_ledger_does_not_fall_back_to_public(monkeypatch, tmp_path):
    (tmp_path / "public.md").write_text("public projection")
    main = load_script("check_asks.py", monkeypatch, tmp_path, [])
    assert main() == 1


def test_ledger_cannot_leave_repository(monkeypatch, tmp_path):
    main = load_script("check_asks.py", monkeypatch, tmp_path, ["--ledger", "../outside.md"])
    assert main() == 1


@pytest.mark.parametrize("artifact", ["../outside.txt", "/tmp/outside.txt"])
def test_public_ledger_artifact_cannot_leave_repository(monkeypatch, tmp_path, artifact):
    ledger = tmp_path / "public.md"
    ledger.write_text(
        "| Check | Requirement | Artifact |\n"
        "|---|---|---|\n"
        f"| P1 | engine | `{artifact}` |\n"
    )
    main = load_script("check_asks.py", monkeypatch, tmp_path, ["--ledger", "public.md"])
    monkeypatch.setitem(main.__globals__, "CONTENT_CHECKS", [])
    assert main() == 1


def test_public_ledger_artifact_cannot_escape_through_symlink(monkeypatch, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("external evidence")
    (tmp_path / "linked.txt").symlink_to(outside)
    ledger = tmp_path / "public.md"
    ledger.write_text(
        "| Check | Requirement | Artifact |\n"
        "|---|---|---|\n"
        "| P1 | engine | `linked.txt` |\n"
    )
    main = load_script("check_asks.py", monkeypatch, tmp_path, ["--ledger", "public.md"])
    monkeypatch.setitem(main.__globals__, "CONTENT_CHECKS", [])
    assert main() == 1


def test_projection_document_selection_and_missing_input(monkeypatch, tmp_path):
    from hospes import completion_registry
    document = tmp_path / "public.md"
    document.write_text(completion_registry.managed_document_block(completion_registry.load_registry()) + "\n")
    main = load_script("check_completion_registry.py", monkeypatch, tmp_path, ["--check", "--document", "public.md"])
    assert main() == 0
    document.unlink()
    assert main() == 1


def test_projection_cannot_leave_repository(monkeypatch, tmp_path):
    main = load_script("check_completion_registry.py", monkeypatch, tmp_path, ["--check", "--document", "../outside.md"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_storage_acceptance_uses_public_completion_projections():
    command = (ROOT / "scripts" / "verify-storage-substrate.sh").read_text()
    assert "--document docs/ROADMAP.md" in command
    assert "--document docs/public-completion.md" in command
