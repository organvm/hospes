"""The eight START_HERE "almost working" clauses, each as an end-to-end test.

From podcast_os_v0_starter_pack/START_HERE.md, "the system is ready for first
use when":

1. candidates can be entered into the pipeline;
2. each candidate has an episode thesis and contact route;
3. Ari can approve, reject, protect, or add a personal note;
4. approved candidates generate correspondence drafts;
5. accepted guests are routed to LA, NYC, or Austin;
6. the producer receives a research and segment brief;
7. the recording produces a predefined asset package;
8. every promise and follow-up is tracked.

Each test exercises the real modules against the pipeline fixture.
"""

from datetime import date

from hospes import (
    approvals,
    assets,
    briefs,
    commitments,
    drafts,
    pipeline,
    routing,
)
from hospes.paths import PIPELINE_FIXTURE


def test_1_candidates_can_be_entered_into_the_pipeline():
    rows = pipeline.load_candidates(PIPELINE_FIXTURE)
    result = pipeline.validate_candidates(rows)
    assert result.ok
    assert len(result.valid) >= 6


def test_2_each_candidate_has_thesis_and_contact_route():
    rows = pipeline.load_candidates(PIPELINE_FIXTURE)
    for row in rows:
        assert row["episode_thesis"].strip()
        assert row["contact_route"].strip()


def test_3_ari_can_approve_reject_protect_or_note(tmp_path):
    log = tmp_path / "audit.log"
    rows = pipeline.load_candidates(PIPELINE_FIXTURE)
    decisions = [
        {"guest_name": rows[0]["guest_name"], "decision": "APPROVE"},
        {"guest_name": rows[6]["guest_name"], "decision": "REJECT", "note": "not a fit"},
        {"guest_name": rows[4]["guest_name"], "decision": "PROTECT", "note": "friend"},
        {"guest_name": rows[3]["guest_name"], "decision": "NOTE", "note": "hold for fall"},
    ]
    res = approvals.apply_decisions(rows, decisions, log_path=log)
    assert res.ok
    assert rows[0]["status"] == "APPROVED"
    assert rows[6]["status"] == "DECLINED"
    assert approvals.is_protected(rows[4])
    assert "hold for fall" in rows[3]["notes"]


def test_4_approved_candidates_generate_correspondence_drafts(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    rows = pipeline.load_candidates(PIPELINE_FIXTURE)
    approvals.apply_decision(rows[0], "APPROVE", log_path=log)
    res = drafts.generate_draft(rows[0], drafts_dir=d, log_path=log)
    assert not res.refused
    assert res.path is not None
    with open(res.path, encoding="utf-8") as handle:
        text = handle.read()
    assert "NOT SENT" in text


def test_5_accepted_guests_routed_to_la_nyc_or_austin():
    rows = pipeline.load_candidates(PIPELINE_FIXTURE)
    routed = [routing.route_to_studio(r) for r in rows if r["preferred_city"] in
              ("Los Angeles", "New York City", "Austin")]
    assert routed
    for d in routed:
        assert d.studio in ("LA", "NYC", "AUSTIN")


def test_6_producer_receives_research_and_segment_brief(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "briefs"
    rows = pipeline.load_candidates(PIPELINE_FIXTURE)
    res = briefs.generate_brief(rows[0], briefs_dir=d, log_path=log)
    with open(res.path, encoding="utf-8") as handle:
        text = handle.read()
    assert "The Claim" in text and "The Stress Test" in text and "The Artifact" in text
    assert res.rotating_segment in text


def test_7_recording_produces_predefined_asset_package(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "assets"
    rows = pipeline.load_candidates(PIPELINE_FIXTURE)
    res = assets.generate_asset_checklist(rows[0], assets_dir=d, log_path=log)
    assert len(res.items) == len(assets.ASSET_PACKAGE)
    assert "Transcript + description" in res.items


def test_8_every_promise_and_followup_is_tracked(tmp_path):
    csv = tmp_path / "commitments.csv"
    log = tmp_path / "audit.log"
    commitments.create("opp-1", "Send transcript", deadline="2020-01-01",
                       path=csv, log_path=log)
    commitments.create("opp-1", "Confirm title", deadline="2999-01-01",
                       path=csv, log_path=log)
    assert len(commitments.list_all(csv)) == 2
    due = commitments.list_due(as_of=date(2026, 7, 13), path=csv)
    assert any(c.description == "Send transcript" for c in due)
