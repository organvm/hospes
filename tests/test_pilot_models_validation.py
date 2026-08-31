"""Tests for hospes.pilot_models validation error branches."""

from __future__ import annotations

import pytest

from hospes.pilot_models import (
    PilotError,
    _bounded_string,
    _positive_int,
    _required_mapping,
    aware_datetime,
    opaque_id,
)


class TestPilotModelsValidation:
    """Tests for pilot_models validation error branches."""

    def test_required_mapping_rejects_non_mapping(self):
        """Test _required_mapping rejects non-mapping."""
        from hospes.pilot_models import _required_mapping
        with pytest.raises(PilotError) as exc_info:
            _required_mapping("not a mapping", "test")
        assert exc_info.value.status_code == 422
        assert "must be an object" in str(exc_info.value.detail)

    def test_positive_int_rejects_bool(self):
        """Test _positive_int rejects boolean."""
        from hospes.pilot_models import _positive_int
        with pytest.raises(PilotError) as exc_info:
            _positive_int(True, "test", minimum=1, maximum=10)
        assert exc_info.value.status_code == 422
        assert "must be an integer" in str(exc_info.value.detail)

    def test_positive_int_rejects_out_of_range(self):
        """Test _positive_int rejects out of range."""
        from hospes.pilot_models import _positive_int
        with pytest.raises(PilotError) as exc_info:
            _positive_int(0, "test", minimum=1, maximum=10)
        assert exc_info.value.status_code == 422
        assert "must be an integer" in str(exc_info.value.detail)

        with pytest.raises(PilotError) as exc_info:
            _positive_int(11, "test", minimum=1, maximum=10)
        assert exc_info.value.status_code == 422

    def test_bounded_string_rejects_non_string(self):
        """Test _bounded_string rejects non-string."""
        from hospes.pilot_models import _bounded_string
        with pytest.raises(PilotError) as exc_info:
            _bounded_string(123, "test", minimum=1, maximum=10)
        assert exc_info.value.status_code == 422
        assert "must be a string" in str(exc_info.value.detail)

    def test_bounded_string_rejects_length_violation(self):
        """Test _bounded_string rejects length violation."""
        from hospes.pilot_models import _bounded_string
        with pytest.raises(PilotError) as exc_info:
            _bounded_string("a", "test", minimum=2, maximum=10)
        assert exc_info.value.status_code == 422
        assert "characters" in str(exc_info.value.detail).lower()

        with pytest.raises(PilotError) as exc_info:
            _bounded_string("a" * 11, "test", minimum=2, maximum=10)
        assert exc_info.value.status_code == 422

    def test_bounded_string_rejects_private_content(self):
        """Test _bounded_string rejects private content."""
        from hospes.pilot_models import _bounded_string
        with pytest.raises(PilotError) as exc_info:
            _bounded_string("email@example.com", "test", minimum=1, maximum=50)
        assert exc_info.value.status_code == 422
        assert "private" in str(exc_info.value.detail).lower()

    def test_opaque_id_rejects_invalid_format(self):
        """Test opaque_id rejects invalid format."""
        from hospes.pilot_models import opaque_id
        with pytest.raises(PilotError) as exc_info:
            opaque_id("INVALID", "test")  # uppercase
        assert exc_info.value.status_code == 422
        assert "opaque" in str(exc_info.value.detail).lower()

        with pytest.raises(PilotError) as exc_info:
            opaque_id("valid:format", "test")  # contains colon
        assert exc_info.value.status_code == 422
        assert "opaque" in str(exc_info.value.detail).lower()

        with pytest.raises(PilotError) as exc_info:
            opaque_id("ValidFormat", "test")  # uppercase
        assert exc_info.value.status_code == 422

    def test_aware_datetime_rejects_non_datetime_string(self):
        """Test aware_datetime rejects non-datetime/string."""
        from hospes.pilot_models import aware_datetime
        with pytest.raises(PilotError) as exc_info:
            aware_datetime(123, "test")
        assert exc_info.value.status_code == 422
        assert "timestamp" in str(exc_info.value.detail).lower()

    def test_aware_datetime_rejects_invalid_iso_format(self):
        """Test aware_datetime rejects invalid ISO format."""
        from hospes.pilot_models import aware_datetime
        with pytest.raises(PilotError) as exc_info:
            aware_datetime("not-a-date", "test")
        assert exc_info.value.status_code == 422
        assert "timestamp" in str(exc_info.value.detail).lower()

    def test_aware_datetime_rejects_naive_datetime(self):
        """Test aware_datetime rejects naive datetime."""
        from datetime import datetime
        from hospes.pilot_models import aware_datetime
        with pytest.raises(PilotError) as exc_info:
            aware_datetime(datetime(2026, 1, 1), "test")
        assert exc_info.value.status_code == 422
        assert "timezone" in str(exc_info.value.detail).lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])