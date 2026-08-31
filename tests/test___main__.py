"""Tests for the isolated reusable demo runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hospes import demo_runner, pipeline
from hospes.demo_runner import DemoRunner, DemoRunnerError, run_demo


def row(name: str, relationship_class: str = "C1", status: str = "RESEARCHING"):
    return {
        "guest_name": name,
        "category": "fictional",
        "why_guest": "A fictional candidate demonstrates the isolated demo workflow.",
        "why_now": "The deterministic fixture needs an executable review path.",
        "episode_thesis": "A bounded synthetic workflow can make authority visible.",
        "proposed_artifact": "A synthetic artifact",
        "relationship_class": relationship_class,
        "relationship_owner": "demo_owner",
        "contact_route": "fixture://route/demo",
        "verified_contact": "fixture-verified",
        "preferred_city": "Los Angeles",
        "date_window": "Synthetic window",
        "social_cost_1_5": "1",
        "ari_effort": "low",
        "status": status,
        "next_action": "Review the fictional candidate.",
        "next_action_date": "2026-08-10",
        "source_provenance": "fixture://demo/candidate",
        "notes": "",
    }


def write_pipeline(path: Path, rows) -> Path:
    pipeline.save_candidates(path, list(rows))
    return path


def test_run_demo_isolates_every_output_under_out_dir(tmp_path: Path) -> None:
    csv_path = write_pipeline(
        tmp_path / "pipeline.csv",
        [
            row("Synthetic Eligible", "C2"),
            row("Synthetic Protected", "C4"),
            row("Synthetic Rejected", "C1", "REJECTED"),
        ],
    )
    out_dir = tmp_path / "selected-output"
    summary = DemoRunner(
        pipeline_csv=csv_path,
        sample_decisions=tmp_path / "missing-decisions.json",
        out_dir=out_dir,
        quiet=True,
    ).run()

    assert summary["candidates_loaded"] == 3
    assert summary["approved"] == 1
    assert summary["protected_refusals"] == 1
    assert summary["drafts"] == 2
    assert Path(summary["packet_sources"]).parent == out_dir
    assert (out_dir / "drafts" / "synthetic-eligible.md").is_file()
    assert not (out_dir / "drafts" / "synthetic-protected.md").exists()
    assert (out_dir / "briefs" / "synthetic-eligible.md").is_file()
    assert (out_dir / "assets" / "synthetic-eligible.md").is_file()
    assert (out_dir / "commitments.csv").is_file()
    events = [
        json.loads(line)
        for line in (out_dir / "audit.log").read_text(encoding="utf-8").splitlines()
    ]
    refusals = [
        event for event in events if event["action"] == "outreach.refused_protected"
    ]
    assert len(refusals) == 1
    assert not any("Synthetic Protected" in str(path) for path in out_dir.rglob("*.md"))


def test_invalid_pipeline_fails_before_downstream_artifacts(tmp_path: Path) -> None:
    invalid = row("Invalid Candidate")
    invalid["episode_thesis"] = ""
    csv_path = write_pipeline(tmp_path / "invalid.csv", [invalid])
    out_dir = tmp_path / "out"
    with pytest.raises(DemoRunnerError, match="episode_thesis"):
        DemoRunner(
            pipeline_csv=csv_path,
            sample_decisions=tmp_path / "missing.json",
            out_dir=out_dir,
            quiet=True,
        ).run()
    assert not out_dir.exists()


def test_missing_pipeline_uses_fixture_without_logging_type_error(
    tmp_path: Path,
) -> None:
    summary = DemoRunner(
        pipeline_csv=tmp_path / "missing.csv",
        sample_decisions=tmp_path / "missing.json",
        out_dir=tmp_path / "out",
        quiet=True,
    ).run()
    assert summary["candidates_loaded"] > 0


def test_empty_pipeline_is_a_valid_zero_candidate_demo(tmp_path: Path) -> None:
    csv_path = write_pipeline(tmp_path / "empty.csv", [])
    summary = DemoRunner(
        pipeline_csv=csv_path,
        sample_decisions=tmp_path / "missing.json",
        out_dir=tmp_path / "out",
        quiet=True,
    ).run()
    assert summary["candidates_loaded"] == 0
    assert summary["approved"] == 0
    assert summary["commitments_total"] == 2


def test_convenience_function_uses_selected_output(tmp_path: Path) -> None:
    csv_path = write_pipeline(tmp_path / "pipeline.csv", [row("Synthetic C1")])
    summary = run_demo(
        pipeline_csv=csv_path,
        sample_decisions=tmp_path / "missing.json",
        out_dir=tmp_path / "out",
        quiet=True,
    )
    assert summary["approved"] == 1
    assert Path(summary["packet_sources"]).is_file()


def test_selected_pipeline_ignores_partial_bundled_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_path = write_pipeline(
        tmp_path / "selected.csv",
        [row("Bundled Name", "C2"), row("Additional Name", "C3")],
    )
    partial = tmp_path / "partial-decisions.json"
    partial.write_text(
        json.dumps([{"guest_name": "Bundled Name", "decision": "APPROVE"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(demo_runner, "SAMPLE_DECISIONS", partial)
    summary = DemoRunner(
        pipeline_csv=csv_path,
        out_dir=tmp_path / "out",
        quiet=True,
    ).run()
    assert summary["decisions_applied"] == 2
    assert summary["approved"] == 2


def test_explicit_sample_decisions_opt_in_for_selected_pipeline(tmp_path: Path) -> None:
    csv_path = write_pipeline(
        tmp_path / "selected.csv",
        [row("Explicit Decision", "C2"), row("No Decision", "C3")],
    )
    explicit = tmp_path / "explicit-decisions.json"
    explicit.write_text(
        json.dumps([{"guest_name": "Explicit Decision", "decision": "APPROVE"}]),
        encoding="utf-8",
    )
    summary = DemoRunner(
        pipeline_csv=csv_path,
        sample_decisions=explicit,
        out_dir=tmp_path / "out",
        quiet=True,
    ).run()
    assert summary["decisions_applied"] == 1
    assert summary["approved"] == 1


def test_selected_pipeline_reports_derived_decisions_truthfully(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = write_pipeline(tmp_path / "selected.csv", [row("Selected Name")])
    DemoRunner(
        pipeline_csv=csv_path,
        out_dir=tmp_path / "out",
    ).run()

    output = capsys.readouterr().out
    assert (
        "derived decisions from relationship classes for the selected pipeline"
        in output
    )
    assert "sample decisions did not match" not in output
