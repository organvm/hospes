import pytest

from hospes import drafts, voice
from hospes.voice import VoiceConstitutionError


def _cand(name="Guest X", rel="C0", **extra):
    base = {
        "guest_name": name,
        "relationship_class": rel,
        "episode_thesis": "What success destroys after it solves the original problem",
        "why_now": "Just released a new special",
        "why_guest": "Clear counter-position on craft and recognition",
        "proposed_artifact": "A rule set for staying useful",
        "date_window": "Q3 2026",
    }
    base.update(extra)
    return base


def test_c0_generates_producer_cold(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    res = drafts.generate_draft(_cand(rel="C0"), drafts_dir=d, log_path=log)
    assert not res.refused
    assert res.template == "producer_cold"
    text = (d / "guest-x.md").read_text()
    assert "DRAFT" in text
    assert "NOT SENT" in text


def test_c2_includes_prior_collaborator_and_note(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    res = drafts.generate_draft(_cand(rel="C2"), drafts_dir=d, log_path=log)
    assert res.template == "prior_collaborator"
    text = (d / "guest-x.md").read_text()
    assert "personal note" in text.lower()


def test_c4_refused_protected(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    res = drafts.generate_draft(_cand(rel="C4"), drafts_dir=d, log_path=log)
    assert res.refused
    assert res.path is None
    assert "PROTECTED" in res.reason
    # No file written for a refused draft.
    assert not (d / "guest-x.md").exists()


def test_c5_refused_protected(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    res = drafts.generate_draft(_cand(rel="C5"), drafts_dir=d, log_path=log)
    assert res.refused


def test_explicitly_protected_flag_refused(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    cand = _cand(rel="C1")
    cand["protected"] = "true"
    res = drafts.generate_draft(cand, drafts_dir=d, log_path=log)
    assert res.refused


def test_banned_phrase_rejected_before_write(tmp_path):
    # A candidate whose fields carry a banned phrase must not silently ship;
    # the Voice Constitution rejects it.
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    cand = _cand(rel="C0", why_now="Your groundbreaking work reshaped the landscape")
    with pytest.raises(VoiceConstitutionError):
        drafts.generate_draft(cand, drafts_dir=d, log_path=log)


def test_generated_drafts_pass_voice(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    res = drafts.generate_draft(_cand(rel="C0"), drafts_dir=d, log_path=log)
    text = (d / "guest-x.md").read_text()
    assert voice.is_compliant(text)


def test_no_network_imports():
    # Static guarantee: drafts.py imports no network modules.
    import hospes.drafts as m
    with open(m.__file__, encoding="utf-8") as handle:
        src = handle.read()
    for banned in ("import socket", "import requests", "import urllib", "smtplib", "http.client"):
        assert banned not in src
