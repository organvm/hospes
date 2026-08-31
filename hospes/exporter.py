"""Custody-safe guest packet exporter.

Packets are rendered only from generated, typed synthetic source JSON.  The
exporter never reads private demo databases, never falls back to invented copy,
and never sends the resulting draft.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .packet_sources import (
    PILOT_IDS,
    PacketSource,
    PacketSourceError,
    load_packet_sources,
)
from .paths import BRIEFS_DIR_SOURCE, OUT_DIR

TEMPLATE_NAME = "guest-packet.html"


class PacketExportError(RuntimeError):
    """Raised when a validated packet cannot be rendered or written."""


def build_guest_packet(
    pilot_id: str,
    *,
    source_dir: str | Path = OUT_DIR,
) -> PacketSource:
    """Return one validated typed source record."""
    if pilot_id not in PILOT_IDS:
        raise PacketSourceError(f"unknown pilot id: {pilot_id}")
    return load_packet_sources(source_dir)[pilot_id]


def render_html_template(data: PacketSource) -> str:
    """Render the tracked template with strict variables and autoescaping."""
    environment = Environment(
        loader=FileSystemLoader(str(BRIEFS_DIR_SOURCE)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
    )
    template = environment.get_template(TEMPLATE_NAME)
    return template.render(**data)


def _atomic_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def _atomic_pdf(path: Path, html: str) -> Path:
    try:
        from weasyprint import HTML
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise PacketExportError(
            "PDF export requires the declared optional extra: pip install -e '.[pdf]'"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        HTML(string=html, base_url=str(BRIEFS_DIR_SOURCE.parent)).write_pdf(str(temporary_path))
        if not temporary_path.exists() or temporary_path.stat().st_size == 0:
            raise PacketExportError("PDF renderer produced no output")
        os.replace(temporary_path, path)
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        if isinstance(exc, PacketExportError):
            raise
        raise PacketExportError(f"PDF export failed: {exc}") from exc
    return path


def export_pilot_packet(
    pilot_id: str,
    *,
    format: str = "html",
    source_dir: str | Path = OUT_DIR,
    out_dir: str | Path = OUT_DIR,
) -> Path:
    """Render one reviewed synthetic packet and return its output path.

    Only ``pilot-a``, ``pilot-b``, and ``pilot-c`` are admitted.  Source
    validation rejects missing or placeholder fields, protected candidates,
    private fields, and claims without approved citations before any file is
    created.
    """
    normalized_format = format.lower().strip()
    if normalized_format not in {"html", "pdf"}:
        raise PacketExportError("format must be html or pdf")
    data = build_guest_packet(pilot_id, source_dir=source_dir)
    html = render_html_template(data)
    target = Path(out_dir) / f"{pilot_id}-guest-packet.{normalized_format}"
    if normalized_format == "html":
        return _atomic_text(target, html)
    return _atomic_pdf(target, html)


def cmd_export_pilot_packet(args: argparse.Namespace) -> int:
    """CLI entrypoint for ``export-pilot-packet``."""
    try:
        output = export_pilot_packet(
            args.pilot_id,
            format=args.format,
            source_dir=Path(args.source_dir),
            out_dir=Path(args.out_dir),
        )
    except (OSError, PacketExportError, PacketSourceError) as exc:
        print(f"[export] {exc}", file=sys.stderr)
        return 2
    print(f"[export] draft packet written to {output}")
    return 0


def build_export_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Add the packet exporter to the public CLI parser."""
    parser = subparsers.add_parser(
        "export-pilot-packet",
        help="render a synthetic, review-required HTML/PDF packet",
    )
    parser.add_argument("pilot_id", choices=sorted(PILOT_IDS))
    parser.add_argument("--format", choices=("html", "pdf"), default="html")
    parser.add_argument(
        "--source-dir",
        default=str(OUT_DIR),
        help="directory containing packet-sources.json",
    )
    parser.add_argument(
        "--out-dir",
        default=str(OUT_DIR),
        help="packet output directory",
    )
    parser.set_defaults(func=cmd_export_pilot_packet)
    return parser


__all__ = [
    "PacketExportError",
    "build_export_parser",
    "build_guest_packet",
    "cmd_export_pilot_packet",
    "export_pilot_packet",
    "render_html_template",
]
