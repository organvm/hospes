"""Tests for spec/care_profile.schema.json — Guest delight and care profile (AA1)."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "spec" / "care_profile.schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), "spec/care_profile.schema.json must exist"


def test_schema_loads_as_json():
    schema = _load_schema()
    assert schema["title"] == "GuestCareProfile"
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert schema["$id"] == "https://hospes.organvm/spec/care_profile.schema.json"


def test_schema_has_required_fields():
    schema = _load_schema()
    required = schema.get("required", [])
    assert "care_profile_id" in required
    assert "person_id" in required


def test_schema_additionalProperties_false():
    schema = _load_schema()
    assert schema.get("additionalProperties") is False


def test_schema_has_all_expected_properties():
    schema = _load_schema()
    props = schema.get("properties", {})
    expected = [
        "care_profile_id",
        "person_id",
        "name_pronunciation",
        "pronouns",
        "dietary_restrictions",
        "allergies",
        "accessibility_needs",
        "preferred_beverage",
        "travel_preferences",
        "gift_restrictions",
        "employer_gift_policy",
        "topics_off_limits",
        "notes",
        "consent_reference",
        "created_at",
        "updated_at",
    ]
    for field in expected:
        assert field in props, f"Expected field '{field}' missing from care_profile schema"


def _validate_against_schema(instance: dict, schema: dict) -> list[str]:
    """Minimal draft-07 validator for the fields used in care_profile (no jsonschema dep)."""
    errors = []
    required = schema.get("required", [])
    for field in required:
        if field not in instance:
            errors.append(f"Missing required field: {field}")

    if schema.get("additionalProperties") is False:
        allowed = set(schema.get("properties", {}).keys())
        for key in instance:
            if key not in allowed:
                errors.append(f"Additional property not allowed: {key}")

    return errors


def test_valid_sample_profile_passes():
    schema = _load_schema()
    sample = {
        "care_profile_id": "cp-001",
        "person_id": "p-001",
        "name_pronunciation": "ˈærɪ ˈmænɪs",
        "pronouns": "he/him",
        "dietary_restrictions": ["vegan"],
        "allergies": [],
        "accessibility_needs": [],
        "preferred_beverage": "black coffee",
        "gift_restrictions": ["alcohol"],
        "employer_gift_policy": "gifts must not exceed $25",
        "topics_off_limits": ["family members"],
        "notes": "Prefers a quiet green room before recording.",
        "consent_reference": "consent-abc",
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:00Z",
    }
    errors = _validate_against_schema(sample, schema)
    assert errors == [], f"Valid sample should pass: {errors}"


def test_minimal_profile_with_only_required_fields_passes():
    schema = _load_schema()
    minimal = {
        "care_profile_id": "cp-002",
        "person_id": "p-002",
    }
    errors = _validate_against_schema(minimal, schema)
    assert errors == [], f"Minimal profile should pass: {errors}"


def test_unknown_field_rejected():
    schema = _load_schema()
    bad = {
        "care_profile_id": "cp-003",
        "person_id": "p-003",
        "favourite_color": "blue",  # not in schema
    }
    errors = _validate_against_schema(bad, schema)
    assert any("favourite_color" in e for e in errors), (
        "Unknown field 'favourite_color' should be rejected by additionalProperties: false"
    )


def test_person_schema_references_care_profile():
    """Person schema should reference care_profile_id linking to care_profile.schema.json."""
    person_schema = json.loads((ROOT / "spec" / "person.schema.json").read_text(encoding="utf-8"))
    props = person_schema.get("properties", {})
    assert "care_profile_id" in props, "person.schema.json must have care_profile_id property"
    desc = props["care_profile_id"].get("description", "")
    assert "care_profile" in desc.lower(), (
        "person.schema.json care_profile_id description should reference care_profile.schema.json"
    )
