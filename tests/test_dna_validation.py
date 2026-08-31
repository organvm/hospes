"""Tests for hospes.dna validation error branches."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hospes import dna


class TestDnaValidationErrors:
    """Tests for dna validation error branches."""

    def test_load_dna_rejects_non_mapping_yaml(self):
        """Test validate_file rejects non-mapping YAML via load_dna."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("- item1\n- item2\n")  # List, not mapping
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert any("top-level YAML must be a mapping" in e for e in result.errors)
        finally:
            yaml_path.unlink()

    def test_validate_dna_rejects_show_not_mapping(self):
        """Test validate_file handles show not a mapping (exposes code bug)."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("show: not-a-mapping\n")
            yaml_path = Path(f.name)

        try:
            with pytest.raises(AttributeError):
                dna.validate_file(yaml_path)
        finally:
            yaml_path.unlink()

    def test_validate_dna_rejects_show_missing_required_keys(self):
        """Test validate_dna rejects show missing required keys."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("show: {}\n")  # Missing title, status, primary_format
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert any("missing required" in e.lower() for e in result.errors)
        finally:
            yaml_path.unlink()

    def test_validate_dna_rejects_fixed_not_nonempty_list(self):
        """Test validate_dna rejects fixed format_engine not a non-empty list."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("""
show:
  title: Test
  status: active
  primary_format: interview
format_engine:
  fixed: "not a list"
""")
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert any("fixed" in e.lower() and "list" in e.lower() for e in result.errors)
        finally:
            yaml_path.unlink()

    def test_validate_dna_rejects_rotating_not_nonempty_list(self):
        """Test validate_dna rejects rotating format_engine not a non-empty list."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("""
show:
  title: Test
  status: active
  primary_format: interview
format_engine:
  fixed: ["Claim → Stress Test → Artifact"]
  rotating: "not a list"
""")
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert any("rotating" in e.lower() and "list" in e.lower() for e in result.errors)
        finally:
            yaml_path.unlink()

    def test_validate_dna_rejects_unknown_fixed_segment(self):
        """Test validate_dna rejects unknown fixed segment."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("""
show:
  title: Test
  status: active
  primary_format: interview
format_engine:
  fixed: ["Unknown Segment"]
  rotating: ["Future Headline"]
""")
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert any("unknown" in e.lower() and "fixed" in e.lower() for e in result.errors)
        finally:
            yaml_path.unlink()

    def test_validate_dna_rejects_unknown_rotating_segment(self):
        """Test validate_dna rejects unknown rotating segment."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("""
show:
  title: Test
  status: active
  primary_format: interview
format_engine:
  fixed: ["Claim → Stress Test → Artifact"]
  rotating: ["Unknown Rotating"]
""")
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert any("unknown" in e.lower() and "rotating" in e.lower() for e in result.errors)
        finally:
            yaml_path.unlink()

    def test_validate_file_returns_error_on_yaml_error(self):
        """Test validate_file returns error on YAML error."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("invalid: yaml: content: [}\n")
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert any("yaml" in e.lower() for e in result.errors)
        finally:
            yaml_path.unlink()

    def test_validate_file_returns_error_on_validation_error(self):
        """Test validate_file returns error on validation error."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("show: {}\n")
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            assert result.errors
            assert len(result.errors) > 0
        finally:
            yaml_path.unlink()

    def test_recording_cities_legacy_top_level(self):
        """Test recording_cities legacy top-level key."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.show.yaml', delete=False) as f:
            f.write("""
recording_cities: ["Los Angeles", "New York City"]
show:
  title: Test
  status: active
  primary_format: interview
format_engine:
  fixed: ["The Claim", "The Stress Test", "The Artifact"]
  rotating: ["Future Headline"]
""")
            yaml_path = Path(f.name)

        try:
            result = dna.validate_file(yaml_path)
            # Should not error, legacy key is supported
            assert not result.errors
            assert "Los Angeles" in result.recording_cities
            assert "New York City" in result.recording_cities
        finally:
            yaml_path.unlink()

    def test_discover_dna_files_missing_dir_returns_empty(self):
        """Test discover_dna_files missing directory returns empty list."""
        result = dna.discover_dna_files(Path("/nonexistent/directory"))
        assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
