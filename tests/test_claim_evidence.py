"""Tests for claim-evidence provenance (AA3).

Guards:
- spec/claim_evidence.schema.json: schema shape and required fields
- hospes/drafts.validate_claims: rejects unapproved/incomplete claims
- hospes/drafts.generate_draft: refuses drafts with unapproved claims; accepts clean drafts
"""
import json
from pathlib import Path

import pytest

from hospes import drafts
from hospes.drafts import ClaimNotApprovedError, validate_claims

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "spec" / "claim_evidence.schema.json"


# -----------------------------------------------------------------------
# Schema tests
# -----------------------------------------------------------------------

def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), "spec/claim_evidence.schema.json must exist"


def test_schema_loads_as_json():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["title"] == "ClaimEvidence"
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert schema["$id"] == "https://hospes.organvm/spec/claim_evidence.schema.json"


def test_schema_required_fields():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    required = schema.get("required", [])
    for field in ("claim", "source_url", "verified_date", "approved_for_external_use"):
        assert field in required, f"'{field}' must be in required"


def test_schema_additionalProperties_false():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema.get("additionalProperties") is False


def test_schema_approved_for_external_use_is_boolean():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    prop = schema["properties"]["approved_for_external_use"]
    assert prop.get("type") == "boolean"


# -----------------------------------------------------------------------
# validate_claims unit tests
# -----------------------------------------------------------------------

def _good_claim(**overrides) -> dict:
    base = {
        "claim": "Published three books on the history of jazz in America.",
        "source_url": "https://example.com/author-page",
        "verified_date": "2026-07-01",
        "approved_for_external_use": True,
    }
    base.update(overrides)
    return base


def test_empty_claims_list_passes():
    validate_claims([])  # must not raise


def test_single_approved_claim_passes():
    validate_claims([_good_claim()])  # must not raise


def test_multiple_approved_claims_pass():
    validate_claims([_good_claim(), _good_claim(claim="Won a Peabody Award in 2019.")])


def test_unapproved_claim_raises():
    bad = _good_claim(approved_for_external_use=False)
    with pytest.raises(ClaimNotApprovedError, match="not approved"):
        validate_claims([bad])


def test_missing_approved_field_raises():
    bad = {
        "claim": "Won a Peabody Award.",
        "source_url": "https://example.com",
        "verified_date": "2026-07-01",
        # approved_for_external_use missing
    }
    with pytest.raises(ClaimNotApprovedError, match="missing required fields"):
        validate_claims([bad])


def test_missing_source_url_raises():
    bad = {
        "claim": "Won a Peabody Award.",
        # source_url missing
        "verified_date": "2026-07-01",
        "approved_for_external_use": True,
    }
    with pytest.raises(ClaimNotApprovedError, match="missing required fields"):
        validate_claims([bad])


def test_missing_verified_date_raises():
    bad = {
        "claim": "Won a Peabody Award.",
        "source_url": "https://example.com",
        # verified_date missing
        "approved_for_external_use": True,
    }
    with pytest.raises(ClaimNotApprovedError, match="missing required fields"):
        validate_claims([bad])


def test_approved_false_but_fields_otherwise_complete_raises():
    bad = _good_claim(approved_for_external_use=False)
    with pytest.raises(ClaimNotApprovedError):
        validate_claims([bad])


def test_mixed_claims_raises_on_first_bad():
    claims = [
        _good_claim(),
        _good_claim(approved_for_external_use=False),  # second is bad
    ]
    with pytest.raises(ClaimNotApprovedError):
        validate_claims(claims)


# -----------------------------------------------------------------------
# generate_draft integration tests
# -----------------------------------------------------------------------

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


def test_draft_with_approved_claims_passes(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    cand = _cand(claims=[_good_claim()])
    result = drafts.generate_draft(cand, drafts_dir=d, log_path=log)
    assert not result.refused
    assert result.path is not None


def test_draft_with_no_claims_passes(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    cand = _cand()  # no claims key
    result = drafts.generate_draft(cand, drafts_dir=d, log_path=log)
    assert not result.refused


def test_draft_with_empty_claims_list_passes(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    cand = _cand(claims=[])
    result = drafts.generate_draft(cand, drafts_dir=d, log_path=log)
    assert not result.refused


def test_draft_with_unapproved_claim_raises(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    cand = _cand(claims=[_good_claim(approved_for_external_use=False)])
    with pytest.raises(ClaimNotApprovedError):
        drafts.generate_draft(cand, drafts_dir=d, log_path=log)


def test_draft_with_missing_claim_field_raises(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    bad_claim = {
        "claim": "Great work on the new album.",
        "source_url": "https://example.com",
        # verified_date and approved_for_external_use both missing
    }
    cand = _cand(claims=[bad_claim])
    with pytest.raises(ClaimNotApprovedError, match="missing required fields"):
        drafts.generate_draft(cand, drafts_dir=d, log_path=log)


def test_no_draft_file_written_when_claim_refused(tmp_path):
    """A draft file must not be written if claims validation fails."""
    log = tmp_path / "audit.log"
    d = tmp_path / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    cand = _cand(claims=[_good_claim(approved_for_external_use=False)])
    with pytest.raises(ClaimNotApprovedError):
        drafts.generate_draft(cand, drafts_dir=d, log_path=log)
    # No draft file should exist.
    assert list(d.iterdir()) == [], "No draft file should be written when claims are rejected"
