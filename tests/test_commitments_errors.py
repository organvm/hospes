"""Tests for hospes.commitments error branches."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hospes import commitments
from hospes.paths import COMMITMENTS_CSV


class TestCommitmentsErrors:
    """Tests for commitments error branches."""

    def setup_method(self):
        """Create a temporary commitments CSV for each test."""
        self.temp_csv = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False)
        self.temp_csv.write("id,opportunity,description,owner,deadline,status\n")
        self.temp_csv.close()

    def teardown_method(self):
        """Clean up temporary CSV."""
        Path(self.temp_csv.name).unlink(missing_ok=True)

    def test_get_returns_none_when_not_found(self):
        """Test get returns None when commitment not found."""
        result = commitments.get("C9999", path=Path(self.temp_csv.name))
        assert result is None

    def test_update_rejects_invalid_field(self):
        """Test update rejects invalid field."""
        # Create a commitment first
        c = commitments.create(
            "test-opp", "Test description", owner="producer",
            deadline="2026-09-01", status="pending", path=Path(self.temp_csv.name)
        )
        with pytest.raises(commitments.CommitmentError) as exc_info:
            commitments.update(c.id, invalid_field="value", path=Path(self.temp_csv.name))
        assert exc_info.value.args[0] == "cannot update field 'invalid_field'"

    def test_update_rejects_id_change(self):
        """Test update rejects id change."""
        c = commitments.create(
            "test-opp", "Test description", owner="producer",
            deadline="2026-09-01", status="pending", path=Path(self.temp_csv.name)
        )
        with pytest.raises(commitments.CommitmentError) as exc_info:
            commitments.update(c.id, id="new-id", path=Path(self.temp_csv.name))
        assert exc_info.value.args[0] == "cannot update field 'id'"

    def test_update_rejects_invalid_status(self):
        """Test update rejects invalid status."""
        c = commitments.create(
            "test-opp", "Test description", owner="producer",
            deadline="2026-09-01", status="pending", path=Path(self.temp_csv.name)
        )
        with pytest.raises(commitments.CommitmentError) as exc_info:
            commitments.update(c.id, status="invalid_status", path=Path(self.temp_csv.name))
        assert "invalid status" in str(exc_info.value).lower()

    def test_update_raises_when_not_found(self):
        """Test update raises when commitment not found."""
        with pytest.raises(commitments.CommitmentError) as exc_info:
            commitments.update("C9999", description="New", path=Path(self.temp_csv.name))
        assert "not found" in str(exc_info.value).lower()

    def test_delete_returns_false_when_not_found(self):
        """Test delete returns False when commitment not found."""
        result = commitments.delete("C9999", path=Path(self.temp_csv.name))
        assert result is False

    def test_parse_deadline_empty_returns_none(self):
        """Test _parse_deadline empty string returns None."""
        from hospes.commitments import _parse_deadline
        result = _parse_deadline("")
        assert result is None

    def test_parse_deadline_invalid_format_returns_none(self):
        """Test _parse_deadline invalid format returns None."""
        from hospes.commitments import _parse_deadline
        result = _parse_deadline("not-a-date")
        assert result is None

    def test_parse_deadline_iso_format_with_timezone(self):
        """Test _parse_deadline ISO format with timezone."""
        from hospes.commitments import _parse_deadline
        from datetime import date
        result = _parse_deadline("2026-09-01T12:00:00+00:00")
        assert result == date(2026, 9, 1)

    def test_parse_deadline_date_only(self):
        """Test _parse_deadline with date only format."""
        from hospes.commitments import _parse_deadline
        from datetime import date
        result = _parse_deadline("2026-09-01")
        assert result == date(2026, 9, 1)

    def test_parse_deadline_datetime_without_timezone(self):
        """Test _parse_deadline with datetime without timezone."""
        from hospes.commitments import _parse_deadline
        from datetime import date
        result = _parse_deadline("2026-09-01T12:00:00")
        assert result == date(2026, 9, 1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])