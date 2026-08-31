"""Canonical filesystem locations for the HOSPES engine.

All paths are derived from the package location so the commands work from the
repo root regardless of the caller's cwd. Runtime outputs live ONLY under
``out/`` (gitignored). Nothing here performs I/O beyond resolving paths and
creating output directories on demand.
"""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

from . import __version__

# hospes/paths.py -> hospes/ -> repo root
PACKAGE_ROOT = Path(__file__).resolve().parent
_SOURCE_ROOT = PACKAGE_ROOT.parent
_SOURCE_CHECKOUT = (_SOURCE_ROOT / "pyproject.toml").exists()
REPO_ROOT = _SOURCE_ROOT if _SOURCE_CHECKOUT else Path.cwd()


def _copy_resource(node: object, destination: Path) -> None:
    if node.is_dir():  # type: ignore[attr-defined]
        destination.mkdir(parents=True, exist_ok=True)
        for child in node.iterdir():  # type: ignore[attr-defined]
            _copy_resource(child, destination / child.name)
        return
    if not node.is_file():  # type: ignore[attr-defined]
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with node.open("rb") as source:  # type: ignore[attr-defined]
        content = source.read()
    if not destination.exists() or destination.read_bytes() != content:
        destination.write_bytes(content)


def _copy_overlay(source: Path, destination: Path) -> None:
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_overlay(child, destination / child.name)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_bytes() != source.read_bytes():
        shutil.copy2(source, destination)


def _resource_path(relative: str) -> Path:
    """Use source assets or a versioned packaged-resource overlay in ``out``."""
    candidate = REPO_ROOT / relative
    if _SOURCE_CHECKOUT and candidate.exists():
        return candidate
    cache_version = __version__.replace("/", "_")
    target = REPO_ROOT / "out" / ".resources" / cache_version / relative
    try:
        resource = files("hospes.resources").joinpath(relative)
        if not resource.is_file() and not resource.is_dir():
            return candidate
        _copy_resource(resource, target)
        if candidate.exists():
            _copy_overlay(candidate, target)
        return target
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return candidate

SPEC_DIR = _resource_path("spec")
CONFIG_DIR = _resource_path("config")
DATA_DIR = _resource_path("data")
DNA_DIR = _resource_path("dna")
BRIEFS_DIR_SOURCE = _resource_path("briefs")
DASHBOARD_DIR = _resource_path("dashboard")
OUT_DIR = REPO_ROOT / "out"

# Runtime output locations (all under out/, which is gitignored).
DRAFTS_DIR = OUT_DIR / "drafts"
BRIEFS_DIR = OUT_DIR / "briefs"
ASSETS_DIR = OUT_DIR / "assets"
AUDIT_LOG = OUT_DIR / "audit.log"
COMMITMENTS_CSV = OUT_DIR / "commitments.csv"

# The guest-operations service store (sqlite3). A runtime artifact under out/,
# overridable via the HOSPES_DB env var (service.py reads the override).
SERVICE_DB = OUT_DIR / "hospes.sqlite3"

# Optional data another builder owns; the engine falls back to the fixture.
PIPELINE_CSV = DATA_DIR / "pipeline.csv"
SAMPLE_DECISIONS = DATA_DIR / "sample_decisions.json"

# Test fixtures the engine falls back to when data/ is empty.
TESTS_DIR = REPO_ROOT / "tests"
PIPELINE_FIXTURE = TESTS_DIR / "fixtures" / "pipeline_fixture.csv"


def ensure_out_dirs() -> None:
    """Create the runtime output directories (idempotent)."""
    for d in (OUT_DIR, DRAFTS_DIR, BRIEFS_DIR, ASSETS_DIR):
        d.mkdir(parents=True, exist_ok=True)
