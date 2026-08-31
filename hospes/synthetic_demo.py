"""Resettable, explicitly non-authoritative Host demonstration stores.

The factory builds two isolated SQLite databases through the same domain
services used by the operator.  It never opens or copies the canonical private
Pilot database, and it will replace only a database that already carries the
expected ``engine.synthetic_demo`` marker.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import yaml

from . import (
    contact_roster,
    encryption,
    generation,
    migrations,
    partnerships,
    pilot_service,
    platform,
    service,
    store,
    touchpoints,
)
from .paths import CONFIG_DIR
from .pilot_models import PilotDecisionInput

MARKER_KEY = "engine.synthetic_demo"
MARKER_VERSION = 2
BUILD_RECEIPT_VERSION = 2
RUNTIME_KIND = "synthetic_demo"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_PILOT_DIR = Path.home() / "Library" / "Application Support" / "HOSPES" / "private_pilot"
DEFAULT_DEMO_DIR = PRIVATE_PILOT_DIR / "demo"
CANONICAL_DB = PRIVATE_PILOT_DIR / "hospes.sqlite3"
BUILD_RECEIPT_NAME = "synthetic-demo-build-receipt.json"
LOCK_NAME = ".synthetic-demo.lock"
JOURNAL_NAME = ".synthetic-demo-install.journal.json"
JOURNAL_VERSION = 1
INSTALL_PHASES = (
    "prepared",
    "backed_up",
    "review_installed",
    "complete_installed",
    "receipt_installed",
    "committed",
)
SCENARIOS = {
    "review_ready": {
        "filename": "ari-review.synthetic-demo.sqlite3",
        "tenant_id": "ari_demo_review",
    },
    "complete": {
        "filename": "ari-complete.synthetic-demo.sqlite3",
        "tenant_id": "ari_demo_complete",
    },
}
PARTNERSHIP_TEMPLATE = CONFIG_DIR / "partnerships" / "ari-synthetic-demo.yaml"

_FIGURES = (
    {
        "key": "bixby",
        "name": "[SYNTHETIC] Bixby Mortarboard",
        "relationship_class": "C2",
        "social_cost": 1,
        "owner": "demo_owner_bixby",
        "thesis": "A fictional workflow can expose decision boundaries without borrowing real authority.",
        "artifact": "A synthetic boundary map",
    },
    {
        "key": "mildred",
        "name": "[SYNTHETIC] Mildred Punchclock",
        "relationship_class": "C2",
        "social_cost": 1,
        "owner": "demo_owner_mildred",
        "thesis": "A resettable specimen can make human gates observable without performing external work.",
        "artifact": "A synthetic review clock",
    },
    {
        "key": "rufus",
        "name": "[SYNTHETIC] Rufus Quibble",
        "relationship_class": "C3",
        "social_cost": 2,
        "owner": "demo_owner_rufus",
        "thesis": "A planner is trustworthy only when every recommendation remains a revision-checked human choice.",
        "artifact": "A synthetic planner ledger",
    },
    {
        "key": "tallulah",
        "name": "[SYNTHETIC] Tallulah Sidecar",
        "relationship_class": "C4",
        "social_cost": 3,
        "owner": "demo_owner_tallulah",
        "thesis": "A protected fictional relationship should remain outside every outreach and slate path.",
        "artifact": "A synthetic protection exercise",
    },
    {
        "key": "crumbweather",
        "name": "[SYNTHETIC] Professor Crumbweather",
        "relationship_class": "C1",
        "social_cost": 2,
        "owner": "demo_owner_crumbweather",
        "thesis": "A fictional rejection should stay attributable without becoming a real-world assertion.",
        "artifact": "A synthetic rejection exercise",
    },
)


class SyntheticDemoError(RuntimeError):
    """The requested demo build would cross a runtime or authority boundary."""


def _aware_now(value: datetime | None) -> datetime:
    current = value or generation.now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise SyntheticDemoError("synthetic demo build time must include a timezone")
    return current.astimezone(UTC)


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    path.chmod(0o600)
    _fsync_directory(path.parent)


class DemoLease(AbstractContextManager["DemoLease"]):
    """A held private generation/operator lease."""

    def __init__(self, handle: Any, path: Path) -> None:
        self._handle = handle
        self.path = path

    def close(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __exit__(self, *_args: Any) -> None:
        self.close()


def acquire_demo_lease(
    output_dir: str | Path,
    *,
    shared: bool,
    blocking: bool = False,
) -> DemoLease:
    """Acquire the bundle lock shared by builders and running operators."""
    output = Path(output_dir).expanduser()
    if output.is_symlink() or not output.is_dir():
        raise SyntheticDemoError("private demo output must be a real directory")
    lock_path = output / LOCK_NAME
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise SyntheticDemoError("synthetic demo lock path is unsafe")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "a+b")
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    if not blocking:
        mode |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), mode)
    except BlockingIOError as exc:
        handle.close()
        raise SyntheticDemoError("synthetic demo bundle is leased by another operator") from exc
    details = os.fstat(handle.fileno())
    if not stat.S_ISREG(details.st_mode):
        handle.close()
        raise SyntheticDemoError("synthetic demo lock path is unsafe")
    os.fchmod(handle.fileno(), 0o600)
    return DemoLease(handle, lock_path)


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _require_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise SyntheticDemoError(f"{label} must be a regular file")
    if any(sidecar.exists() for sidecar in _sidecars(path)):
        raise SyntheticDemoError(f"{label} has an unexpected SQLite sidecar")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    _require_regular(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyntheticDemoError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise SyntheticDemoError(f"{label} is malformed")
    return value


def probe_synthetic_marker(path: str | Path) -> dict[str, Any] | None:
    """Return a valid marker, or ``None`` for an ordinary authority database."""
    database = Path(path).expanduser()
    _require_regular(database, "operator database")
    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        metadata_table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'runtime_metadata'"
        ).fetchone()
        if metadata_table is None:
            return None
        row = connection.execute(
            "SELECT tenant_id, metadata_value FROM runtime_metadata WHERE metadata_key = ?",
            (MARKER_KEY,),
        ).fetchone()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
        raise SyntheticDemoError("operator database is not a valid HOSPES SQLite store") from exc
    finally:
        if "connection" in locals():
            connection.close()
    if row is None:
        return None
    try:
        value = json.loads(str(row["metadata_value"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise SyntheticDemoError("synthetic demo marker is malformed") from exc
    if not isinstance(value, dict):
        raise SyntheticDemoError("synthetic demo marker is malformed")
    marker = {**value, "tenant_id": str(row["tenant_id"])}
    if (
        marker.get("runtime_kind") != RUNTIME_KIND
        or marker.get("marker_version") != MARKER_VERSION
        or marker.get("receipt_version") != BUILD_RECEIPT_VERSION
        or marker.get("scenario") not in SCENARIOS
        or marker.get("tenant_id") != SCENARIOS[str(marker.get("scenario"))]["tenant_id"]
        or not isinstance(marker.get("generated_at"), str)
        or not isinstance(marker.get("generation_id"), str)
    ):
        raise SyntheticDemoError("synthetic demo marker is not recognized")
    try:
        UUID(str(marker["generation_id"]))
        generated_at = datetime.fromisoformat(str(marker["generated_at"]))
    except (ValueError, TypeError) as exc:
        raise SyntheticDemoError("synthetic demo marker is not recognized") from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise SyntheticDemoError("synthetic demo marker is not recognized")
    expected_generation_id = generation.deterministic_generation_id(generated_at, int(marker["receipt_version"]))
    if marker["generation_id"] != expected_generation_id:
        raise SyntheticDemoError("synthetic demo marker generation binding is invalid")
    return marker


def read_synthetic_marker(path: str | Path) -> dict[str, Any]:
    """Read a marker without migrating or otherwise mutating the database."""
    marker = probe_synthetic_marker(path)
    if marker is None:
        raise SyntheticDemoError("database is not marked engine.synthetic_demo")
    return marker


def validate_synthetic_database(
    path: str | Path,
    *,
    tenant_id: str,
    scenario: str | None = None,
) -> dict[str, Any]:
    marker = read_synthetic_marker(path)
    if marker["tenant_id"] != tenant_id:
        raise SyntheticDemoError("operator tenant does not match the demo marker")
    if scenario is not None and marker["scenario"] != scenario:
        raise SyntheticDemoError("operator scenario does not match the demo marker")
    database = Path(path).expanduser()
    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise SyntheticDemoError("synthetic demo database integrity check failed") from exc
        if integrity != [("ok",)]:
            raise SyntheticDemoError("synthetic demo database integrity check failed")
        try:
            foreign_key_drift = connection.execute("PRAGMA foreign_key_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise SyntheticDemoError("synthetic demo database foreign-key check failed") from exc
        if foreign_key_drift is not None:
            raise SyntheticDemoError("synthetic demo database foreign-key check failed")
        ledger = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        expected = [(item.version, item.name) for item in migrations.MIGRATIONS]
        if ledger != expected:
            raise SyntheticDemoError("synthetic demo database migration ledger drifted")
    except sqlite3.DatabaseError as exc:
        raise SyntheticDemoError("synthetic demo database validation failed") from exc
    finally:
        connection.close()
    return marker


def _receipt_builds(receipt: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    builds = receipt.get("builds")
    if not isinstance(builds, list) or len(builds) != len(SCENARIOS):
        raise SyntheticDemoError("synthetic demo build receipt is malformed")
    indexed: dict[str, dict[str, Any]] = {}
    for item in builds:
        if not isinstance(item, dict) or not isinstance(item.get("scenario"), str):
            raise SyntheticDemoError("synthetic demo build receipt is malformed")
        scenario = str(item["scenario"])
        if scenario in indexed or scenario not in SCENARIOS:
            raise SyntheticDemoError("synthetic demo build receipt is malformed")
        indexed[scenario] = item
    if set(indexed) != set(SCENARIOS):
        raise SyntheticDemoError("synthetic demo build receipt is malformed")
    return indexed


def validate_synthetic_bundle(
    output_dir: str | Path,
    *,
    enforce_review_checksum: bool = False,
) -> dict[str, Any]:
    """Validate marker, schema, checksums, and generation cohesion."""
    output = Path(output_dir).expanduser()
    if output.is_symlink() or not output.is_dir():
        raise SyntheticDemoError("private demo output must be a real directory")
    receipt = _load_json(output / BUILD_RECEIPT_NAME, "synthetic demo build receipt")
    if (
        receipt.get("receipt_version") != BUILD_RECEIPT_VERSION
        or receipt.get("runtime_kind") != RUNTIME_KIND
        or not isinstance(receipt.get("generation_id"), str)
        or not isinstance(receipt.get("generated_at"), str)
    ):
        raise SyntheticDemoError("synthetic demo build receipt is malformed")
    try:
        UUID(str(receipt["generation_id"]))
        generated = datetime.fromisoformat(str(receipt["generated_at"]))
    except (ValueError, TypeError) as exc:
        raise SyntheticDemoError("synthetic demo build receipt is malformed") from exc
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise SyntheticDemoError("synthetic demo build receipt is malformed")
    expected_generation_id = generation.deterministic_generation_id(generated, int(receipt["receipt_version"]))
    if receipt["generation_id"] != expected_generation_id:
        raise SyntheticDemoError("synthetic demo build receipt generation binding is invalid")
    builds = _receipt_builds(receipt)
    markers: dict[str, dict[str, Any]] = {}
    for scenario, config in SCENARIOS.items():
        path = output / str(config["filename"])
        item = builds[scenario]
        if (
            item.get("filename") != path.name
            or item.get("tenant_id") != config["tenant_id"]
            or item.get("generation_id") != receipt["generation_id"]
            or item.get("marker_version") != MARKER_VERSION
            or item.get("checksum_bound") is not (scenario == "complete")
            or not isinstance(item.get("sha256"), str)
            or SHA256_HEX.fullmatch(str(item["sha256"])) is None
        ):
            raise SyntheticDemoError("synthetic demo build receipt is malformed")
        marker = validate_synthetic_database(path, tenant_id=str(config["tenant_id"]), scenario=scenario)
        if (
            marker["generation_id"] != receipt["generation_id"]
            or marker["generated_at"] != receipt["generated_at"]
            or marker["generation_id"] != item["generation_id"]
        ):
            raise SyntheticDemoError("synthetic demo bundle mixes generations")
        if scenario == "complete" or enforce_review_checksum:
            if _sha256(path) != item["sha256"]:
                raise SyntheticDemoError(f"{scenario.replace('_', '-')} specimen checksum drifted")
        markers[scenario] = marker
    return {"receipt": receipt, "markers": markers}


def _safe_transaction_dir(output: Path, name: Any, prefix: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name.startswith(prefix):
        raise SyntheticDemoError("synthetic demo install journal is unsafe")
    return output / name


def _remove_transaction_dir(path: Path, prefix: str) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir() or not path.name.startswith(prefix):
        raise SyntheticDemoError("synthetic demo transaction directory is unsafe")
    shutil.rmtree(path)


def _write_journal(output: Path, document: dict[str, Any], phase: str) -> None:
    if phase not in INSTALL_PHASES:
        raise SyntheticDemoError("synthetic demo install phase is invalid")
    document["phase"] = phase
    _atomic_json(output / JOURNAL_NAME, document)


def recover_synthetic_install(output_dir: str | Path) -> str:
    """Recover a previously journaled install without trusting journal paths."""
    output = Path(output_dir).expanduser()
    journal_path = output / JOURNAL_NAME
    if not journal_path.exists():
        return "clean"
    journal = _load_json(journal_path, "synthetic demo install journal")
    if (
        journal.get("journal_version") != JOURNAL_VERSION
        or journal.get("phase") not in INSTALL_PHASES
        or not isinstance(journal.get("generation_id"), str)
    ):
        raise SyntheticDemoError("synthetic demo install journal is malformed")
    phase = str(journal["phase"])
    staging = _safe_transaction_dir(output, journal.get("staging_dir"), ".synthetic-demo-stage-")
    backup = _safe_transaction_dir(output, journal.get("backup_dir"), ".synthetic-demo-backup-")
    targets = {
        "review_ready": output / str(SCENARIOS["review_ready"]["filename"]),
        "complete": output / str(SCENARIOS["complete"]["filename"]),
        "receipt": output / BUILD_RECEIPT_NAME,
    }
    installed_by_phase = {
        "prepared": (),
        "backed_up": (),
        "review_installed": ("review_ready",),
        "complete_installed": ("review_ready", "complete"),
        "receipt_installed": ("review_ready", "complete", "receipt"),
        "committed": ("review_ready", "complete", "receipt"),
    }
    if phase == "committed":
        try:
            validate_synthetic_bundle(output)
        except SyntheticDemoError:
            pass
        else:
            _remove_transaction_dir(staging, ".synthetic-demo-stage-")
            _remove_transaction_dir(backup, ".synthetic-demo-backup-")
            journal_path.unlink()
            _fsync_directory(output)
            return "committed"
    if phase != "prepared":
        for key in installed_by_phase[phase]:
            target = targets[key]
            if target.exists():
                _require_regular(
                    target,
                    "partially installed synthetic demo artifact",
                )
                target.unlink()
        for key, target in targets.items():
            saved = backup / target.name
            if saved.exists():
                _require_regular(saved, "synthetic demo backup artifact")
                os.replace(saved, target)
    _remove_transaction_dir(staging, ".synthetic-demo-stage-")
    _remove_transaction_dir(backup, ".synthetic-demo-backup-")
    journal_path.unlink()
    _fsync_directory(output)
    return "rolled_back"


def _insert_marker(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    scenario: str,
    timestamp: datetime,
    generation_id: str,
) -> None:
    store.insert(
        connection,
        "runtime_metadata",
        {
            "id": generation.new_id("runtime_metadata"),
            "tenant_id": tenant_id,
            "metadata_key": MARKER_KEY,
            "metadata_value": {
                "marker_version": MARKER_VERSION,
                "receipt_version": BUILD_RECEIPT_VERSION,
                "runtime_kind": RUNTIME_KIND,
                "scenario": scenario,
                "generation_id": generation_id,
                "generated_at": timestamp.isoformat(),
            },
            "created_at": timestamp.isoformat(),
        },
    )
    connection.commit()


def _write_policy(path: Path, generated_at: datetime) -> None:
    policy = {
        "version": 1,
        "policy": {
            "key": "ari_demo.synthetic_14_day",
            "version": 1,
            "deadline_at": (generated_at + timedelta(days=14)).isoformat(),
            "timezone": "America/Los_Angeles",
            "candidate_eligibility": {
                "count": 3,
                "network_id": "ari_demo_network",
                "city": "Los Angeles",
                "relationship_classes": ["C2", "C3"],
                "max_social_cost": 2,
            },
            "follow_up_limit": 1,
            "response_windows": {"initial_hours": 48, "follow_up_hours": 24},
            "production_gate_durations": {
                "booking": 12,
                "consent": 6,
                "brief": 8,
                "asset_preflight": 8,
                "rehearsal": 12,
            },
            "safety_reserve_hours": 24,
            "human_authority_rules": {
                "no_autonomous_sending": True,
                "multi_activation_requires_owner_approval": True,
                "decision_roles": [
                    "producer",
                    "editorial_owner",
                    "relationship_owner",
                ],
            },
            "relationship_exposure_budget": 2,
            "rehearsal_lead_hours": 48,
        },
    }
    path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


def _seed_candidates(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    verified_at: datetime,
) -> dict[str, dict[str, Any]]:
    producer = service.HumanActor("demo_producer", service.HumanRole.PRODUCER, tenant_id)
    seeded: dict[str, dict[str, Any]] = {}
    for figure in _FIGURES:
        created = service.create_opportunity(
            connection,
            service.OpportunityCreate.from_dict(
                {
                    "tenant_id": tenant_id,
                    "network_id": "ari_demo_network",
                    "show_id": "synthetic_private_pilot",
                    "guest_name": figure["name"],
                    "why_guest": ("This explicitly fictional figure exercises a bounded HOSPES demonstration branch."),
                    "why_now": ("The resettable synthetic specimen needs a visible decision and evidence path."),
                    "proposed_artifact": figure["artifact"],
                    "relationship_class": figure["relationship_class"],
                    "relationship_owner": figure["owner"],
                    "social_cost_1_5": figure["social_cost"],
                    "ari_effort": "synthetic_review_only",
                    "preferred_city": "Los Angeles",
                    "next_action": "Await an explicit synthetic human decision.",
                    "source_provenance": "fixture://ari-synthetic-demo/candidate",
                }
            ),
            producer,
        )
        prepared = service.attach_thesis_contact(
            connection,
            created["id"],
            service.ThesisContactUpdate.from_dict(
                {
                    "episode_thesis": figure["thesis"],
                    "route_type": "fixture_route",
                    "route_label": f"fixture://route/{figure['key']}",
                    "source_provenance": f"fixture://provenance/{figure['key']}",
                    "verified_at": verified_at.isoformat(),
                }
            ),
            producer,
        )
        seeded[str(figure["key"])] = prepared
    return seeded


#: Encrypted private context attached to the review specimen.  Every value is
#: fictional: names carry the [SYNTHETIC] prefix, addresses use the reserved
#: example.com domain, and telephone numbers use the reserved 555 range.
_PRIVATE_CONTEXT = {
    "bixby": {
        "roster": {
            "publicist": {
                "name": "[SYNTHETIC] Wendell Foldaway",
                "email": "wendell.foldaway@example.com",
                "provenance_ref": "fixture://roster/bixby/publicist",
                "usable": True,
                "preferred": True,
                "permission_status": "permitted",
            },
            "direct": {
                "name": "[SYNTHETIC] Bixby Mortarboard",
                "phone": "+1 555 0100",
                "provenance_ref": "fixture://roster/bixby/direct",
                "permission_status": "pending_verification",
            },
        },
        "touchpoints": [
            {
                "channel": "hallway",
                "days_ago": 12,
                "notes": (
                    "Ran into them after a fictional panel. They raised the boundary-map "
                    "idea unprompted and asked who else was being invited."
                ),
            },
            {
                "channel": "text",
                "days_ago": 4,
                "notes": (
                    "Follow-up thread. Warm but non-committal until the synthetic March "
                    "window is confirmed. No ask made."
                ),
            },
        ],
    },
    "mildred": {
        "roster": {
            "manager": {
                "name": "[SYNTHETIC] Odette Ledgerwood",
                "email": "odette.ledgerwood@example.com",
                "provenance_ref": "fixture://roster/mildred/manager",
                "usable": True,
                "preferred": True,
                "permission_status": "permitted",
            },
        },
        "touchpoints": [
            {
                "channel": "ig",
                "days_ago": 21,
                "notes": (
                    "Replied to a fictional story about the review clock. Offered to "
                    "introduce a third synthetic figure."
                ),
            },
        ],
    },
    "rufus": {
        "roster": {
            "agent": {
                "name": "[SYNTHETIC] Harcourt Bellweather",
                "email": "harcourt.bellweather@example.com",
                "phone": "+1 555 0142",
                "provenance_ref": "fixture://roster/rufus/agent",
                "usable": True,
                "permission_status": "permitted",
            },
        },
        "touchpoints": [
            {
                "channel": "phone",
                "days_ago": 8,
                "notes": (
                    "Fifteen fictional minutes on scheduling. Agent asked for the "
                    "artifact brief before committing to anything."
                ),
            },
        ],
    },
    "crumbweather": {
        "roster": {
            "publicist": {
                "name": "[SYNTHETIC] Prudence Gatekeep",
                "email": "prudence.gatekeep@example.com",
                "provenance_ref": "fixture://roster/crumbweather/publicist",
                "permission_status": "opted_out",
            },
        },
        "touchpoints": [
            {
                "channel": "email",
                "days_ago": 45,
                "notes": (
                    "Fictional publicist declined on the guest's behalf and asked not to "
                    "be contacted again this season. Recorded so nobody re-approaches."
                ),
            },
        ],
    },
    "tallulah": {
        "roster": {
            "direct": {
                "name": "[SYNTHETIC] Tallulah Sidecar",
                "email": "tallulah.sidecar@example.com",
                "provenance_ref": "fixture://roster/tallulah/direct",
                "permission_status": "do_not_use",
            },
        },
        "touchpoints": [
            {
                "channel": "in-person",
                "days_ago": 30,
                "notes": (
                    "Long fictional conversation. This relationship is worth more than "
                    "any booking; the correct action here is to protect, not to ask."
                ),
            },
        ],
    },
}


def _field_vault() -> encryption.FieldVault | None:
    """Return an environment-backed vault, or None when custody is unconfigured.

    Private context is genuinely optional: without a master key the operator
    surface hides the contact-roster and touchpoint panels entirely, so seeding
    them would produce rows no surface could decrypt or display.  A bundle built
    without custody is complete and valid — it simply carries no private layer.
    """
    if not os.environ.get("HOSPES_MASTER_KEY_B64"):
        return None
    try:
        provider = encryption.EnvironmentMasterKeyProvider()
        # Resolve the key here rather than letting a malformed value surface
        # later as a ContactRosterError from the first encryption, which
        # cmd_demo_open does not handle and which reaches the operator as a
        # traceback instead of a readable message.
        provider.resolve(provider.credential_ref)
        return encryption.FieldVault(encryption.TenantKeyManager(provider))
    except encryption.EncryptionError:
        return None


def _seed_private_context(
    connection: store.DatabaseConnection,
    *,
    tenant_id: str,
    partnership_id: str,
    candidates: Mapping[str, Mapping[str, Any]],
    generated_at: datetime,
) -> dict[str, int]:
    """Attach encrypted contact routes and informal touchpoints to the specimen.

    These are the two surfaces gated on private-field custody.  Seeding them
    means a demonstration shows the encrypted layer working rather than an
    empty panel that reads as an unimplemented feature.
    """
    vault = _field_vault()
    if vault is None:
        return {"rosters": 0, "touchpoints": 0}
    actor_id = "demo_relationship_owner"
    actor_role = "relationship_owner"
    show_id = "synthetic_private_pilot"
    seeded = {"rosters": 0, "touchpoints": 0}
    for key, context in _PRIVATE_CONTEXT.items():
        candidate = candidates.get(key)
        if not candidate:
            continue
        opportunity_id = str(candidate["id"])
        # A usable route must carry a verification timestamp, so it is stamped
        # relative to the build rather than frozen into the fixture.
        roster_payload = {
            role: {
                **entry,
                **({"verified_at": (generated_at - timedelta(days=2)).isoformat()} if entry.get("usable") else {}),
            }
            for role, entry in context["roster"].items()
        }
        entries = contact_roster.parse_contact_roster(
            roster_payload,
            source_key=f"fixture://roster/{key}",
            now=generated_at,
        )
        contact_roster.upsert_contact_roster(
            connection,
            vault,
            entries,
            tenant_id=tenant_id,
            show_id=show_id,
            opportunity_id=opportunity_id,
            actor_role=actor_role,
            timestamp=generated_at.isoformat(),
        )
        seeded["rosters"] += len(entries)
        for note in context["touchpoints"]:
            occurred_at = generated_at - timedelta(days=int(note["days_ago"]))
            touchpoints.record_touchpoint(
                connection,
                vault,
                touchpoints.TouchpointInput.from_mapping(
                    {
                        "opportunity_id": opportunity_id,
                        "partnership_id": partnership_id,
                        "channel": note["channel"],
                        "notes": note["notes"],
                        "occurred_at": occurred_at.isoformat(),
                    },
                    now=generated_at,
                ),
                tenant_id=tenant_id,
                show_id=show_id,
                actor_id=actor_id,
                actor_role=actor_role,
                now=generated_at,
            )
            seeded["touchpoints"] += 1
    connection.commit()
    return seeded


def _review_payload(
    review_kind: str,
    occurred_at: datetime,
    *,
    decisions_count: int,
    coverage_met: int,
    coverage_total: int,
    reference: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "review_kind": review_kind,
        "decisions_count": decisions_count,
        "coverage_met": coverage_met,
        "coverage_total": coverage_total,
        "occurred_at": occurred_at.isoformat(),
    }
    if reference:
        payload["external_reference"] = reference
    return payload


def _receipt(
    connection: sqlite3.Connection,
    opportunity_id: str,
    actor: service.HumanActor,
    *,
    receipt_type: str,
    reference: str,
    occurred_at: datetime,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return service.record_receipt(
        connection,
        opportunity_id,
        service.ReceiptCreate.from_dict(
            {
                "receipt_type": receipt_type,
                "external_reference": reference,
                "occurred_at": occurred_at.isoformat(),
                "details": dict(details),
            }
        ),
        actor,
    )


def _complete_scenario(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    partnership_id: str,
    policy: Mapping[str, Any],
    candidates: Mapping[str, Mapping[str, Any]],
    generated_at: datetime,
) -> dict[str, Any]:
    owner = service.HumanActor("ari_demo_owner", service.HumanRole.RELATIONSHIP_OWNER, tenant_id)
    producer = service.HumanActor("demo_producer", service.HumanRole.PRODUCER, tenant_id)
    logical_start = generated_at - timedelta(hours=3)

    for key in ("bixby", "mildred", "rufus"):
        service.record_decision(
            connection,
            str(candidates[key]["id"]),
            service.DecisionCreate.from_dict({"action": "approve"}),
            owner,
        )
    service.record_decision(
        connection,
        str(candidates["tallulah"]["id"]),
        service.DecisionCreate.from_dict({"action": "protect"}),
        owner,
    )
    service.record_decision(
        connection,
        str(candidates["crumbweather"]["id"]),
        service.DecisionCreate.from_dict({"action": "reject"}),
        owner,
    )
    for slot, key in enumerate(("bixby", "mildred", "rufus"), start=1):
        partnerships.select_pilot_candidate(
            connection,
            partnership_id,
            str(candidates[key]["id"]),
            slot,
            tenant_id=tenant_id,
            actor_id=owner.actor_id,
            actor_role=owner.role.value,
        )
    partnerships.record_review(
        connection,
        partnership_id,
        _review_payload(
            "ari_review",
            logical_start + timedelta(minutes=5),
            decisions_count=5,
            coverage_met=5,
            coverage_total=5,
            reference="fixture://review/ari-complete",
        ),
        tenant_id=tenant_id,
        actor_id=owner.actor_id,
        actor_role=owner.role.value,
        now=generated_at,
    )
    plan = pilot_service.start_run(
        connection,
        partnership_id,
        {"policy_id": policy["id"]},
        tenant_id=tenant_id,
        actor_id=owner.actor_id,
        actor_role=owner.role.value,
        now=logical_start + timedelta(minutes=10),
    )
    if plan["action_kind"] != "activate_set":
        raise SyntheticDemoError("complete specimen did not produce an activation plan")
    plan = pilot_service.record_decision(
        connection,
        partnership_id,
        plan["run_id"],
        PilotDecisionInput.from_mapping(
            {
                "decision_kind": "activate_set",
                "expected_revision": plan["revision"],
                "assignments": plan["ranked_action"]["assignments"],
            }
        ),
        tenant_id=tenant_id,
        actor_id=owner.actor_id,
        actor_role=owner.role.value,
        now=logical_start + timedelta(minutes=11),
    )

    primary_id = str(candidates["bixby"]["id"])
    service.create_draft(
        connection,
        primary_id,
        service.DraftCreate.from_dict({"kind": "invitation"}),
        producer,
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="outreach.sent",
        reference="fixture://receipt/outreach",
        occurred_at=logical_start + timedelta(minutes=12),
        details={},
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="reply.classified",
        reference="fixture://receipt/reply",
        occurred_at=logical_start + timedelta(minutes=14),
        details={"classification": "POSITIVE_INTEREST"},
    )
    service.route_to_studio(
        connection,
        primary_id,
        service.StudioRoutingCreate.from_dict(
            {
                "city": "Los Angeles",
                "studio_reference": "fixture://studio/synthetic-room",
            }
        ),
        producer,
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="booking.confirmed",
        reference="fixture://receipt/booking",
        occurred_at=logical_start + timedelta(minutes=20),
        details={
            "studio_ref": "fixture://studio/synthetic-room",
            "producer_ref": "fixture://producer/synthetic-producer",
            "recording_time": (logical_start + timedelta(minutes=30)).isoformat(),
        },
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="consent.signed",
        reference="fixture://receipt/consent",
        occurred_at=logical_start + timedelta(minutes=25),
        details={"private_pilot": True, "clip_scope": "none"},
    )
    service.create_brief(
        connection,
        primary_id,
        service.BriefCreate.from_dict(
            {
                "research_claims": [
                    {
                        "claim": "This claim describes only a fictional demo specimen.",
                        "evidence_source": "fixture://brief/verified-claim",
                        "verified": True,
                    }
                ],
                "segments": [
                    {
                        "title": "Synthetic Boundary Test",
                        "objective": ("Show how a fictional artifact moves through explicit human and evidence gates."),
                    }
                ],
            }
        ),
        producer,
    )
    package = service.declare_assets(
        connection,
        primary_id,
        service.AssetPackageCreate.from_dict(
            {
                "assets": [
                    {
                        "kind": kind,
                        "custody_target": f"fixture://assets/{kind}",
                    }
                    for kind in sorted(service.REQUIRED_PREFLIGHT_ASSETS)
                ]
            }
        ),
        producer,
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="recording.ready",
        reference="fixture://receipt/preflight",
        occurred_at=logical_start + timedelta(minutes=35),
        details={
            "preflight_ref": "fixture://preflight/synthetic-check",
            "asset_package_id": package["id"],
        },
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="recording.completed",
        reference="fixture://receipt/rehearsal",
        occurred_at=logical_start + timedelta(minutes=40),
        details={"session_kind": "technical_rehearsal"},
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="recording.completed",
        reference="fixture://receipt/guest-pilot",
        occurred_at=logical_start + timedelta(minutes=50),
        details={"session_kind": "guest_pilot"},
    )
    _receipt(
        connection,
        primary_id,
        producer,
        receipt_type="media.ingested",
        reference="fixture://receipt/media",
        occurred_at=logical_start + timedelta(minutes=55),
        details={
            "master_ref": "fixture://media/synthetic-master",
            "checksum_ref": "fixture://checksum/synthetic-sha256",
        },
    )
    partnerships.record_review(
        connection,
        partnership_id,
        _review_payload(
            "pilot_scorecard",
            logical_start + timedelta(minutes=60),
            decisions_count=1,
            coverage_met=14,
            coverage_total=14,
            reference="fixture://scorecard/synthetic-complete",
        ),
        tenant_id=tenant_id,
        actor_id=producer.actor_id,
        actor_role=producer.role.value,
        now=generated_at,
    )
    completed = pilot_service.get_plan(
        connection,
        partnership_id,
        plan["run_id"],
        tenant_id=tenant_id,
        actor_id=owner.actor_id,
        actor_role=owner.role.value,
        now=generated_at,
    )
    if completed["lifecycle_state"] != "COMPLETED":
        raise SyntheticDemoError("complete specimen did not reach COMPLETED")
    return completed


def _build_database(
    path: Path,
    *,
    scenario: str,
    generated_at: datetime,
    generation_id: str,
    policy_path: Path,
) -> dict[str, Any]:
    tenant_id = str(SCENARIOS[scenario]["tenant_id"])
    connection = store.connect(path)
    try:
        platform.register_show(
            connection,
            tenant_id=tenant_id,
            show_id="synthetic_private_pilot",
            label="Synthetic Private Pilot",
            config_ref="synthetic-demo://private-pilot",
            now=generated_at,
        )
        _insert_marker(
            connection,
            tenant_id=tenant_id,
            scenario=scenario,
            timestamp=generated_at,
            generation_id=generation_id,
        )
        imported = partnerships.import_template(
            connection,
            PARTNERSHIP_TEMPLATE,
            tenant_id=tenant_id,
            show_id="synthetic_private_pilot",
            actor_id="demo_producer",
            actor_role="producer",
            now=generated_at - timedelta(hours=3),
        )
        connection.commit()
        candidates = _seed_candidates(
            connection,
            tenant_id=tenant_id,
            verified_at=generated_at - timedelta(days=1),
        )
        policy = pilot_service.import_policy(
            connection,
            imported.partnership_id,
            policy_path,
            tenant_id=tenant_id,
            actor_id="demo_producer",
            actor_role="producer",
            now=generated_at,
        )
        if scenario == "review_ready":
            _seed_private_context(
                connection,
                tenant_id=tenant_id,
                partnership_id=imported.partnership_id,
                candidates=candidates,
                generated_at=generated_at - timedelta(minutes=5),
            )
        run: dict[str, Any] | None = None
        if scenario == "complete":
            run = _complete_scenario(
                connection,
                tenant_id=tenant_id,
                partnership_id=imported.partnership_id,
                policy=policy,
                candidates=candidates,
                generated_at=generated_at,
            )
        center = partnerships.command_center(connection, imported.partnership_id, tenant_id)
        eligible = connection.execute(
            "SELECT COUNT(*) FROM appearance_opportunities "
            "WHERE tenant_id = ? AND network_id = ? AND preferred_city = ? "
            "AND relationship_class IN ('C2', 'C3') "
            "AND social_cost_1_5 <= ?",
            (
                tenant_id,
                policy["candidate_network_id"],
                policy["candidate_city"],
                policy["max_social_cost"],
            ),
        ).fetchone()[0]
        decisions = connection.execute("SELECT COUNT(*) FROM decisions WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise SyntheticDemoError(f"SQLite quick_check failed: {quick_check}")
        if scenario == "review_ready" and (eligible != 3 or decisions != 0):
            raise SyntheticDemoError("review-ready specimen must contain three eligible candidates and no decisions")
        if scenario == "complete" and (
            center["pilot_readiness"]["met"] != 14
            or center["pilot_readiness"]["total"] != 14
            or not run
            or run["lifecycle_state"] != "COMPLETED"
        ):
            raise SyntheticDemoError("complete specimen failed its 14/14 predicate")
    finally:
        connection.close()
    path.chmod(0o600)
    return {
        "scenario": scenario,
        "tenant_id": tenant_id,
        "generation_id": generation_id,
        "marker_version": MARKER_VERSION,
        "candidate_counts": {
            "total": len(candidates),
            "policy_eligible": int(eligible),
            "human_decisions": int(decisions),
        },
        "readiness": {
            "met": center["pilot_readiness"]["met"],
            "total": center["pilot_readiness"]["total"],
        },
        "run_state": run["lifecycle_state"] if run else None,
    }


def seed_synthetic_demo(
    output_dir: str | Path = DEFAULT_DEMO_DIR,
    *,
    replace_demo: bool = False,
    now: datetime | None = None,
    allowed_demo_dir: str | Path | None = None,
    _failure_phase: str | None = None,
) -> dict[str, Any]:
    """Build and transactionally install one cohesive three-artifact bundle."""
    generated_at = _aware_now(now)
    generation_id = generation.deterministic_generation_id(generated_at, BUILD_RECEIPT_VERSION)
    output = Path(output_dir).expanduser()
    allowed = Path(allowed_demo_dir or DEFAULT_DEMO_DIR).expanduser()
    if _same_path(output, CANONICAL_DB):
        raise SyntheticDemoError("canonical Pilot database path is forbidden")
    if not _same_path(output, allowed):
        raise SyntheticDemoError(f"output directory must be the private demo directory: {allowed}")
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise SyntheticDemoError("private demo output must be a real directory")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    if output.resolve(strict=True) != allowed.resolve(strict=True):
        raise SyntheticDemoError("private demo output resolved outside its allowed path")

    targets = {scenario: output / str(config["filename"]) for scenario, config in SCENARIOS.items()}
    if any(_same_path(target, CANONICAL_DB) for target in targets.values()):
        raise SyntheticDemoError("canonical Pilot database path is forbidden")
    with acquire_demo_lease(output, shared=False):
        recover_synthetic_install(output)
        final_receipt = output / BUILD_RECEIPT_NAME
        artifact_paths = [*targets.values(), final_receipt]
        existing = [path for path in artifact_paths if path.exists()]
        for target in artifact_paths:
            if target.is_symlink():
                raise SyntheticDemoError("synthetic demo artifact path is a symlink")
            if any(sidecar.exists() for sidecar in _sidecars(target)):
                raise SyntheticDemoError("synthetic demo artifact has a stale sidecar")
        if existing and not replace_demo:
            raise SyntheticDemoError("synthetic demo already exists; pass --replace-demo for a marked replacement")
        if replace_demo and existing:
            if len(existing) != len(artifact_paths):
                raise SyntheticDemoError("existing synthetic demo bundle is incomplete")
            validate_synthetic_bundle(output)

        build_dir = Path(tempfile.mkdtemp(prefix=".synthetic-demo-stage-", dir=str(output)))
        build_dir.chmod(0o700)
        if build_dir.stat().st_dev != output.stat().st_dev:
            _remove_transaction_dir(build_dir, ".synthetic-demo-stage-")
            raise SyntheticDemoError("synthetic demo staging must share the output filesystem")
        backup_dir = output / f".synthetic-demo-backup-{generation_id}"
        if backup_dir.exists() or backup_dir.is_symlink():
            _remove_transaction_dir(build_dir, ".synthetic-demo-stage-")
            raise SyntheticDemoError("synthetic demo backup path already exists")
        backup_dir.mkdir(mode=0o700)
        journal = {
            "journal_version": JOURNAL_VERSION,
            "generation_id": generation_id,
            "staging_dir": build_dir.name,
            "backup_dir": backup_dir.name,
        }
        try:
            policy_path = build_dir / "synthetic-policy.yaml"
            _write_policy(policy_path, generated_at)
            receipts: list[dict[str, Any]] = []
            with generation.deterministic_scope(generated_at, generation_id):
                for scenario, target in targets.items():
                    built_path = build_dir / target.name
                    receipt = _build_database(
                        built_path,
                        scenario=scenario,
                        generated_at=generated_at,
                        generation_id=generation_id,
                        policy_path=policy_path,
                    )
                    receipt.update(
                        {
                            "filename": target.name,
                            "sha256": _sha256(built_path),
                            "checksum_bound": scenario == "complete",
                        }
                    )
                    receipts.append(receipt)
            receipt_document = {
                "receipt_version": BUILD_RECEIPT_VERSION,
                "runtime_kind": RUNTIME_KIND,
                "generation_id": generation_id,
                "generated_at": generated_at.isoformat(),
                "builds": receipts,
            }
            receipt_path = build_dir / BUILD_RECEIPT_NAME
            _atomic_json(receipt_path, receipt_document)
            validate_synthetic_bundle(build_dir, enforce_review_checksum=True)

            _write_journal(output, journal, "prepared")
            if _failure_phase == "prepared":
                raise SyntheticDemoError("injected install failure after prepared")
            for target in artifact_paths:
                if target.exists():
                    _require_regular(target, "existing synthetic demo artifact")
                    os.replace(target, backup_dir / target.name)
            _fsync_directory(output)
            _write_journal(output, journal, "backed_up")
            if _failure_phase == "backed_up":
                raise SyntheticDemoError("injected install failure after backed_up")

            os.replace(
                build_dir / targets["review_ready"].name,
                targets["review_ready"],
            )
            targets["review_ready"].chmod(0o600)
            _fsync_directory(output)
            _write_journal(output, journal, "review_installed")
            if _failure_phase == "review_installed":
                raise SyntheticDemoError("injected install failure after review_installed")

            os.replace(build_dir / targets["complete"].name, targets["complete"])
            targets["complete"].chmod(0o600)
            _fsync_directory(output)
            _write_journal(output, journal, "complete_installed")
            if _failure_phase == "complete_installed":
                raise SyntheticDemoError("injected install failure after complete_installed")

            os.replace(receipt_path, final_receipt)
            final_receipt.chmod(0o600)
            _fsync_directory(output)
            _write_journal(output, journal, "receipt_installed")
            if _failure_phase == "receipt_installed":
                raise SyntheticDemoError("injected install failure after receipt_installed")
            validate_synthetic_bundle(output, enforce_review_checksum=True)
            _write_journal(output, journal, "committed")
            if _failure_phase == "committed":
                raise SyntheticDemoError("injected install failure after committed")
            recover_synthetic_install(output)
            return receipt_document
        except Exception:
            if (output / JOURNAL_NAME).exists():
                recover_synthetic_install(output)
            else:
                _remove_transaction_dir(build_dir, ".synthetic-demo-stage-")
                _remove_transaction_dir(backup_dir, ".synthetic-demo-backup-")
            raise


__all__ = [
    "BUILD_RECEIPT_NAME",
    "BUILD_RECEIPT_VERSION",
    "CANONICAL_DB",
    "DEFAULT_DEMO_DIR",
    "INSTALL_PHASES",
    "JOURNAL_NAME",
    "LOCK_NAME",
    "MARKER_KEY",
    "RUNTIME_KIND",
    "SCENARIOS",
    "SyntheticDemoError",
    "acquire_demo_lease",
    "probe_synthetic_marker",
    "read_synthetic_marker",
    "recover_synthetic_install",
    "seed_synthetic_demo",
    "validate_synthetic_bundle",
    "validate_synthetic_database",
]
