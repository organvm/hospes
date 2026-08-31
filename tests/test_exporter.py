"""Guest packet export contract and hostile-input tests."""

from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path

import pytest
import yaml
from jinja2 import UndefinedError

from hospes import packet_sources, privacy
from hospes.exporter import (
    PacketExportError,
    build_guest_packet,
    cmd_export_pilot_packet,
    export_pilot_packet,
    render_html_template,
)

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_KEY_MARKER = "-----BEGIN " + "PRIVATE KEY-----"


def source_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "sources"
    packet_sources.write_packet_sources(directory)
    return directory


def test_packet_source_domain_contract_matches_runtime_schema() -> None:
    kernel = yaml.safe_load(
        (ROOT / "config" / "domain_kernel.yaml").read_text(encoding="utf-8")
    )
    contract = kernel["packet_source_contract"]
    assert {"PacketSource", "PacketClaim"}.issubset(kernel["entities"])
    assert contract["version"] == 1
    assert set(contract["pilot_ids"]) == packet_sources.PILOT_IDS
    assert (
        set(contract["required_fields"]) == packet_sources.PACKET_SOURCE_REQUIRED_FIELDS
    )
    assert set(contract["claim_fields"]) == packet_sources.PACKET_CLAIM_REQUIRED_FIELDS


def mutate_source(directory: Path, pilot_id: str, mutate) -> None:
    path = directory / packet_sources.PACKET_SOURCE_NAME
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document["pilots"][pilot_id])
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("pilot_id", sorted(packet_sources.PILOT_IDS))
def test_exports_all_three_pilots(tmp_path: Path, pilot_id: str) -> None:
    sources = source_dir(tmp_path)
    output = export_pilot_packet(
        pilot_id,
        source_dir=sources,
        out_dir=tmp_path / "deep" / "packets",
    )
    assert output == tmp_path / "deep" / "packets" / f"{pilot_id}-guest-packet.html"
    html = output.read_text(encoding="utf-8")
    assert "DRAFT — NOT SENT — HUMAN REVIEW REQUIRED" in html
    assert 'name="robots" content="noindex, nofollow, noarchive"' in html
    assert "No correspondence was sent" in html


def test_unknown_pilot_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(packet_sources.PacketSourceError, match="unknown pilot"):
        export_pilot_packet(
            "pilot-z", source_dir=source_dir(tmp_path), out_dir=tmp_path
        )


def test_missing_source_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(packet_sources.PacketSourceError, match="missing"):
        export_pilot_packet("pilot-a", source_dir=tmp_path, out_dir=tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("host_bio"), "missing fields"),
        (
            lambda value: value.__setitem__("host_bio", "TODO: write this"),
            "placeholder",
        ),
        (lambda value: value.__setitem__("relationship_class", "C4"), "protected"),
        (
            lambda value: value.__setitem__("private_notes", "private fixture"),
            "private field",
        ),
        (
            lambda value: value["claims"][0].__setitem__(
                "approved_for_external_use", False
            ),
            "not approved",
        ),
        (
            lambda value: value.__setitem__(
                "episode_thesis", "A different uncited synthetic claim."
            ),
            "not backed",
        ),
        (
            lambda value: value.__setitem__(
                "host_bio", "An invented and uncited synthetic biography."
            ),
            "host bio is not backed",
        ),
        (
            lambda value: value.__setitem__(
                "invitation_note", "Subject: copied mailbox content"
            ),
            "private content",
        ),
        (
            lambda value: value.__setitem__(
                "invitation_note", "CONFIDENTIALITY AGREEMENT between the parties"
            ),
            "private content",
        ),
        (
            lambda value: value.__setitem__("host_bio", PRIVATE_KEY_MARKER),
            "private content",
        ),
        (
            lambda value: value.__setitem__("protected", "true"),
            "protected marker must be boolean",
        ),
    ],
)
def test_rejects_unsafe_packet_sources(tmp_path: Path, mutation, message: str) -> None:
    sources = source_dir(tmp_path)
    mutate_source(sources, "pilot-a", mutation)
    with pytest.raises(packet_sources.PacketSourceError, match=message):
        export_pilot_packet("pilot-a", source_dir=sources, out_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "heading",
    [
        "MUTUAL NON-DISCLOSURE AGREEMENT",
        "GUEST APPEARANCE AGREEMENT",
        "TALENT RELEASE AGREEMENT",
        "MUTUAL NON-DISCLOSURE AGREEMENT (the Agreement)",
        "GUEST APPEARANCE AGREEMENT — effective immediately",
        "GUEST APPEARANCE AGREEMENT: effective immediately",
        "GUEST APPEARANCE AGREEMENT - effective immediately",
        "TALENT RELEASE AGREEMENT August 10, 2026",
        "HOST SERVICES AGREEMENT dated 2026-08-10",
        "SERVICES AGREEMENT: August 10, 2026",
        "SERVICES AGREEMENT — (the Agreement)",
        "SERVICES AGREEMENT — FINAL.",
        "SERVICES AGREEMENT (effective as of August 10, 2026)",
        "SERVICES AGREEMENT (dated 2026-08-10)",
        "SERVICES AGREEMENT version v2.1",
        "SERVICES AGREEMENT (version 2)",
    ],
)
def test_private_text_rejects_qualified_agreement_headings(heading: str) -> None:
    assert privacy.private_text_kind(heading) is not None


@pytest.mark.parametrize(
    "summary",
    [
        "Services agreement renewal remains pending human review.",
        "Services agreement: renewal remains pending human review.",
        "Services agreement — renewal remains pending human review.",
        "Services agreement (renewal remains pending human review)",
        "Services agreement (renewal (Q3) remains pending human review)",
        "Services agreement version pending.",
        "Services agreement version review.",
        "Services agreement version status.",
        "Services agreement (version pending)",
    ],
)
def test_private_text_allows_bounded_agreement_summary(summary: str) -> None:
    assert privacy.private_text_kind(summary) is None


def test_jinja_autoescapes_dynamic_fields(tmp_path: Path) -> None:
    sources = source_dir(tmp_path)
    payload = "<img src=x onerror=alert(1)>"

    def inject(value) -> None:
        value["invitation_note"] = payload

    mutate_source(sources, "pilot-a", inject)
    output = export_pilot_packet(
        "pilot-a", source_dir=sources, out_dir=tmp_path / "out"
    )
    html = output.read_text(encoding="utf-8")
    assert payload not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_export_is_independent_of_current_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    sources = source_dir(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    output = export_pilot_packet(
        "pilot-b", source_dir=sources, out_dir=tmp_path / "out"
    )
    assert output.is_file()


def test_pdf_requires_declared_optional_extra(tmp_path: Path, monkeypatch) -> None:
    sources = source_dir(tmp_path)
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "weasyprint":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(PacketExportError, match="optional extra"):
        export_pilot_packet(
            "pilot-c", format="pdf", source_dir=sources, out_dir=tmp_path / "out"
        )
    assert not (tmp_path / "out" / "pilot-c-guest-packet.pdf").exists()


def test_invalid_format_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PacketExportError, match="html or pdf"):
        export_pilot_packet(
            "pilot-a",
            format="text",
            source_dir=source_dir(tmp_path),
            out_dir=tmp_path,
        )


def test_strict_template_rejects_missing_fields(tmp_path: Path) -> None:
    source = build_guest_packet("pilot-a", source_dir=source_dir(tmp_path))
    del source["host_bio"]
    with pytest.raises(UndefinedError):
        render_html_template(source)


def test_cli_returns_path_success_and_bounded_error(tmp_path: Path, capsys) -> None:
    sources = source_dir(tmp_path)

    class ValidArgs:
        pilot_id = "pilot-a"
        format = "html"
        source_dir = str(sources)
        out_dir = str(tmp_path / "packets")

    assert cmd_export_pilot_packet(ValidArgs()) == 0
    assert "draft packet written" in capsys.readouterr().out

    class MissingArgs:
        pilot_id = "pilot-a"
        format = "html"
        source_dir = str(tmp_path / "missing")
        out_dir = str(tmp_path / "packets")

    assert cmd_export_pilot_packet(MissingArgs()) == 2
    assert "missing" in capsys.readouterr().err


def test_rejects_invalid_json_and_document_shapes(tmp_path: Path) -> None:
    directory = tmp_path / "sources"
    directory.mkdir()
    path = directory / packet_sources.PACKET_SOURCE_NAME
    path.write_text("{", encoding="utf-8")
    with pytest.raises(packet_sources.PacketSourceError, match="invalid JSON"):
        packet_sources.load_packet_sources(directory)
    path.write_text(json.dumps({"version": 2, "pilots": {}}), encoding="utf-8")
    with pytest.raises(packet_sources.PacketSourceError, match="version 1"):
        packet_sources.load_packet_sources(directory)
    path.write_text(json.dumps({"version": 1, "pilots": []}), encoding="utf-8")
    with pytest.raises(packet_sources.PacketSourceError, match="pilots object"):
        packet_sources.load_packet_sources(directory)


def test_loaded_document_requires_exact_pilot_set(tmp_path: Path) -> None:
    directory = source_dir(tmp_path)
    path = directory / packet_sources.PACKET_SOURCE_NAME
    document = json.loads(path.read_text(encoding="utf-8"))
    document["pilots"]["pilot-d"] = document["pilots"]["pilot-a"]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(packet_sources.PacketSourceError, match="exactly"):
        packet_sources.load_packet_sources(directory)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: None, "must be an object"),
        (lambda value: {**value, "pilot_id": "pilot-b"}, "mismatched"),
        (lambda value: {**value, "relationship_class": "C9"}, "invalid relationship"),
        (lambda value: {**value, "asset_items": []}, "asset item"),
        (lambda value: {**value, "claims": []}, "cited claim"),
        (lambda value: {**value, "claims": ["claim"]}, "must be an object"),
        (
            lambda value: {
                **value,
                "claims": [
                    {**value["claims"][0], "source_url": "ftp://invalid/source"}
                ],
            },
            "invalid source URL",
        ),
        (
            lambda value: {
                **value,
                "claims": [{**value["claims"][0], "verified_date": "not-a-date"}],
            },
            "invalid verified date",
        ),
        (lambda value: {**value, "host_bio": "producer@example.com"}, "contact data"),
        (lambda value: {**value, "host_bio": ""}, "is required"),
    ],
)
def test_packet_source_validation_error_branches(
    tmp_path: Path, mutation, message: str
) -> None:
    sources = source_dir(tmp_path)
    document = json.loads(
        (sources / packet_sources.PACKET_SOURCE_NAME).read_text(encoding="utf-8")
    )
    original = document["pilots"]["pilot-a"]
    candidate = mutation(original)
    if candidate is None:
        candidate = None
    with pytest.raises(packet_sources.PacketSourceError, match=message):
        packet_sources.validate_packet_source("pilot-a", candidate)


def test_manifest_requires_exact_pilot_set(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": 2}), encoding="utf-8")
    with pytest.raises(packet_sources.PacketSourceError, match="version 1"):
        packet_sources.write_packet_sources(tmp_path / "out", manifest_path=manifest)
    manifest.write_text(json.dumps({"version": 1, "pilots": {}}), encoding="utf-8")
    with pytest.raises(packet_sources.PacketSourceError, match="exactly"):
        packet_sources.write_packet_sources(tmp_path / "out", manifest_path=manifest)


def test_packet_source_write_cleans_temporary_file_on_replace_failure(
    tmp_path: Path, monkeypatch
) -> None:
    out_dir = tmp_path / "packet-sources"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(packet_sources.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        packet_sources.write_packet_sources(out_dir)
    assert list(out_dir.iterdir()) == []


def test_pdf_renderer_success_and_failure_are_atomic(
    tmp_path: Path, monkeypatch
) -> None:
    sources = source_dir(tmp_path)
    module = types.ModuleType("weasyprint")

    class SuccessfulHTML:
        def __init__(self, **_kwargs):
            pass

        def write_pdf(self, output: str) -> None:
            Path(output).write_bytes(b"%PDF-synthetic")

    module.HTML = SuccessfulHTML
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    output = export_pilot_packet(
        "pilot-a", format="pdf", source_dir=sources, out_dir=tmp_path / "pdf"
    )
    assert output.read_bytes().startswith(b"%PDF")

    class FailingHTML(SuccessfulHTML):
        def write_pdf(self, output: str) -> None:
            raise RuntimeError("renderer failed")

    module.HTML = FailingHTML
    with pytest.raises(PacketExportError, match="renderer failed"):
        export_pilot_packet(
            "pilot-b", format="pdf", source_dir=sources, out_dir=tmp_path / "pdf"
        )
    assert not (tmp_path / "pdf" / "pilot-b-guest-packet.pdf").exists()
