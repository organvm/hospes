"""Tests for the no-promotion rule as a machine gate (AA2).

Guards:
- spec/permission-matrix.yaml: request_guest_promotion is in never_delegated
- spec/consent.schema.json: promotion_required const is false
"""
import json
from pathlib import Path

import pytest

try:
    import yaml  # PyYAML
    HAS_YAML = True
except ImportError:  # pragma: no cover
    HAS_YAML = False

ROOT = Path(__file__).resolve().parent.parent
PERMISSION_MATRIX = ROOT / "spec" / "permission-matrix.yaml"
CONSENT_SCHEMA = ROOT / "spec" / "consent.schema.json"


def _load_permission_matrix() -> dict:
    if not HAS_YAML:
        pytest.skip("PyYAML not installed")
    import yaml as _yaml
    return _yaml.safe_load(PERMISSION_MATRIX.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------
# Permission matrix tests
# -----------------------------------------------------------------------

def test_permission_matrix_has_never_delegated():
    matrix = _load_permission_matrix()
    assert "never_delegated" in matrix.get("tiers", {}), (
        "permission-matrix.yaml must have a never_delegated tier"
    )


def test_request_guest_promotion_is_in_never_delegated():
    """The request_guest_promotion action must appear in never_delegated."""
    text = PERMISSION_MATRIX.read_text(encoding="utf-8")
    assert "request_guest_promotion" in text, (
        "request_guest_promotion must appear in spec/permission-matrix.yaml"
    )
    # Verify it is under the never_delegated section, not automatic or trusted_later.
    # Simple heuristic: it must come after the never_delegated: header.
    never_idx = text.find("never_delegated:")
    promo_idx = text.find("request_guest_promotion")
    assert promo_idx > never_idx, (
        "request_guest_promotion must be listed under never_delegated, not a permissive tier"
    )


def test_request_guest_promotion_not_in_automatic():
    """request_guest_promotion must NOT appear in the automatic tier."""
    text = PERMISSION_MATRIX.read_text(encoding="utf-8")
    auto_idx = text.find("automatic:")
    trusted_idx = text.find("trusted_later:")
    promo_idx = text.find("request_guest_promotion")
    # The promotion action must come after trusted_later (i.e., in never_delegated block).
    if trusted_idx == -1:
        return  # defensive; section must exist
    assert promo_idx > trusted_idx, (
        "request_guest_promotion appears before never_delegated — it must not be in automatic/trusted_later"
    )


def test_request_guest_promotion_blocked_note_present():
    """The entry must include an explanatory note about the design rule."""
    text = PERMISSION_MATRIX.read_text(encoding="utf-8")
    # The note explains no automatic requirement to promote.
    assert "no automatic requirement" in text.lower() or "never required" in text.lower() or "BLOCKED" in text, (
        "permission-matrix.yaml should include a note explaining the no-promotion design rule"
    )


# -----------------------------------------------------------------------
# Consent schema tests
# -----------------------------------------------------------------------

def _load_consent_schema() -> dict:
    return json.loads(CONSENT_SCHEMA.read_text(encoding="utf-8"))


def test_consent_schema_has_promotion_required():
    schema = _load_consent_schema()
    props = schema.get("properties", {})
    assert "promotion_required" in props, (
        "consent.schema.json must have promotion_required property"
    )


def test_consent_promotion_required_is_const_false():
    schema = _load_consent_schema()
    prop = schema["properties"]["promotion_required"]
    assert prop.get("const") is False, (
        "consent.schema.json promotion_required must be const: false "
        "(guests are never required to promote)"
    )


def test_consent_promotion_required_has_description():
    schema = _load_consent_schema()
    prop = schema["properties"]["promotion_required"]
    assert prop.get("description"), "promotion_required should have a description"


def _validate_consent(instance: dict, schema: dict) -> list[str]:
    """Minimal const-checker for the promotion_required field."""
    errors = []
    props = schema.get("properties", {})
    if "promotion_required" in instance and "promotion_required" in props:
        expected_const = props["promotion_required"].get("const")
        if expected_const is not None and instance["promotion_required"] != expected_const:
            errors.append(
                f"promotion_required must be {expected_const!r}, got {instance['promotion_required']!r}"
            )
    return errors


def test_consent_with_promotion_required_true_fails():
    schema = _load_consent_schema()
    bad_consent = {
        "consent_id": "c-1",
        "person_id": "p-1",
        "consent_type": "recording_release",
        "status": "granted",
        "promotion_required": True,  # must fail
    }
    errors = _validate_consent(bad_consent, schema)
    assert errors, "Consent record with promotion_required=True must fail validation"


def test_consent_with_promotion_required_false_passes():
    schema = _load_consent_schema()
    good_consent = {
        "consent_id": "c-2",
        "person_id": "p-2",
        "consent_type": "recording_release",
        "status": "granted",
        "promotion_required": False,
    }
    errors = _validate_consent(good_consent, schema)
    assert not errors, f"Consent record with promotion_required=False must pass: {errors}"


def test_consent_without_promotion_required_passes():
    schema = _load_consent_schema()
    consent = {
        "consent_id": "c-3",
        "person_id": "p-3",
        "consent_type": "recording_release",
        "status": "granted",
        # promotion_required absent — that's fine; field is optional
    }
    errors = _validate_consent(consent, schema)
    assert not errors, f"Consent record without promotion_required must pass: {errors}"
