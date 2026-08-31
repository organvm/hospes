from hospes import dna
from hospes.paths import DNA_DIR


def test_dna_files_discovered():
    files = dna.discover_dna_files()
    names = {p.name for p in files}
    assert "flagship.show.yaml" in names


def test_flagship_validates_clean():
    result = dna.validate_file(DNA_DIR / "flagship.show.yaml")
    assert result.ok, result.errors
    assert result.show_id == "flagship"
    assert "Los Angeles" in result.recording_cities


def test_all_dna_valid():
    for result in dna.validate_all():
        assert result.ok, (result.path, result.errors)


def test_missing_format_engine_flagged():
    data = {"show": {"title": "X", "status": "concept", "primary_format": "y"}}
    errors = dna.validate_dna(data, "test")
    assert any("format_engine" in e for e in errors)


def test_unknown_segment_flagged():
    data = {
        "show": {"title": "X", "status": "concept", "primary_format": "y"},
        "format_engine": {"fixed": ["The Claim"], "rotating": ["Totally Made Up Segment"]},
    }
    errors = dna.validate_dna(data, "test")
    assert any("unknown rotating segment" in e for e in errors)


def test_known_segments_accepted():
    data = {
        "show": {"title": "X", "status": "concept", "primary_format": "y"},
        "format_engine": {
            "fixed": [{"name": "The Claim"}, "The Stress Test", "The Artifact"],
            "rotating": ["Receipts", "Object Lesson"],
        },
    }
    errors = dna.validate_dna(data, "test")
    assert errors == []


def test_recording_cities_union():
    cities = dna.recording_cities()
    assert "Los Angeles" in cities
