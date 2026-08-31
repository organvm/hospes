from hospes import assets


def _cand(name="Guest X"):
    return {"guest_name": name}


def test_checklist_written_with_all_items(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "assets"
    res = assets.generate_asset_checklist(_cand(), assets_dir=d, log_path=log)
    text = (d / "guest-x.md").read_text()
    assert res.slug == "guest-x"
    assert len(res.items) == len(assets.ASSET_PACKAGE)
    for item in assets.ASSET_PACKAGE:
        assert item in text


def test_checklist_covers_required_deliverables(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "assets"
    assets.generate_asset_checklist(_cand(), assets_dir=d, log_path=log)
    text = (d / "guest-x.md").read_text()
    for needed in ("Trailer", "Horizontal clip 1", "Vertical clips", "Chapters",
                   "Artifact asset", "Guest delivery package", "Audio edition",
                   "Transcript + description", "Stills"):
        assert needed in text


def test_checkboxes_present(tmp_path):
    log = tmp_path / "audit.log"
    d = tmp_path / "assets"
    assets.generate_asset_checklist(_cand(), assets_dir=d, log_path=log)
    text = (d / "guest-x.md").read_text()
    assert text.count("- [ ]") == len(assets.ASSET_PACKAGE)
