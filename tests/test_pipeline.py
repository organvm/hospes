from hospes import pipeline
from hospes.paths import PIPELINE_FIXTURE


def test_fixture_loads_with_expected_headers(fixture_rows):
    assert len(fixture_rows) >= 6
    for header in pipeline.PIPELINE_HEADERS:
        assert header in fixture_rows[0]


def test_all_fixture_rows_valid(fixture_rows):
    result = pipeline.validate_candidates(fixture_rows)
    assert result.ok, [e.message for e in result.errors]
    assert len(result.valid) == len(fixture_rows)


def test_missing_required_field_produces_structured_error(fixture_rows):
    row = dict(fixture_rows[0])
    row["episode_thesis"] = ""
    errs = pipeline.validate_candidate(row, 0)
    assert any(e.field_name == "episode_thesis" for e in errs)
    assert errs[0].guest_name == row["guest_name"]


def test_placeholder_counts_as_missing():
    row = {h: "x" for h in pipeline.PIPELINE_HEADERS}
    row["guest_name"] = "[CANDIDATE A]"
    row["relationship_class"] = "C0"
    errs = pipeline.validate_candidate(row, 0)
    assert any(e.field_name == "guest_name" for e in errs)


def test_bad_relationship_class_flagged():
    row = {h: "x" for h in pipeline.PIPELINE_HEADERS}
    row["relationship_class"] = "C9"
    errs = pipeline.validate_candidate(row, 0)
    assert any(e.field_name == "relationship_class" for e in errs)


def test_save_roundtrip(tmp_path, fixture_rows):
    out = tmp_path / "pipeline.csv"
    pipeline.save_candidates(out, fixture_rows)
    reloaded = pipeline.load_candidates(out)
    assert len(reloaded) == len(fixture_rows)
    assert reloaded[0]["guest_name"] == fixture_rows[0]["guest_name"]


def test_required_field_map_covers_workflow_contract():
    # Every workflow candidate_required_field maps to a real CSV column.
    for column in pipeline.REQUIRED_FIELD_TO_COLUMN.values():
        assert column in pipeline.PIPELINE_HEADERS
