from hospes import briefs


def _cand(name="Guest X"):
    return {
        "guest_name": name,
        "episode_thesis": "What people misunderstand about expertise",
        "why_now": "New book out this fall",
        "why_guest": "Cannot be reduced to promotional questions",
        "proposed_artifact": "A public test for expertise",
    }


def test_brief_written_with_sections(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "briefs"
    res = briefs.generate_brief(_cand(), briefs_dir=d, log_path=log)
    text = (d / "guest-x.md").read_text()
    assert "The Claim" in text
    assert "The Stress Test" in text
    assert "The Artifact" in text
    assert res.rotating_segment in briefs.ROTATING_SEGMENTS
    assert res.rotating_segment in text


def test_brief_has_research_and_dayof_fields(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "briefs"
    briefs.generate_brief(_cand(), briefs_dir=d, log_path=log)
    text = (d / "guest-x.md").read_text()
    for f in ("Episode thesis", "Ten primary questions", "Topics to avoid or approach carefully"):
        assert f in text
    for f in ("Today's timeline", "Release status", "Emergency escalation path"):
        assert f in text


def test_rotating_selection_is_deterministic():
    a = briefs.select_rotating_segment(_cand("Same Name"))
    b = briefs.select_rotating_segment(_cand("Same Name"))
    assert a == b


def test_thesis_appears_as_claim(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "briefs"
    briefs.generate_brief(_cand(), briefs_dir=d, log_path=log)
    text = (d / "guest-x.md").read_text()
    assert "What people misunderstand about expertise" in text
