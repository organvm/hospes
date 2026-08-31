"""Process-ledger atomicity and reused-PID denial tests."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "synthetic_demo_lifecycle.py"
)
SPEC = importlib.util.spec_from_file_location("synthetic_lifecycle", SCRIPT)
assert SPEC and SPEC.loader
lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lifecycle)


def test_process_ledger_v1_is_atomic_private_and_identity_bound(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "process-ledger.json"
    pids = {name: os.getpid() for name in lifecycle.PROCESS_NAMES}
    lifecycle.create_ledger(ledger, pids)
    value = lifecycle.load_ledger(ledger)
    assert value["schema"] == lifecycle.SCHEMA
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert lifecycle.verified_pid(ledger, "tunnel") == os.getpid()
    assert not ledger.with_name(".process-ledger.json.tmp").exists()


def test_reused_or_stale_pid_identity_is_never_accepted(tmp_path: Path) -> None:
    ledger = tmp_path / "process-ledger.json"
    pids = {name: os.getpid() for name in lifecycle.PROCESS_NAMES}
    lifecycle.create_ledger(ledger, pids)
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value["processes"]["tunnel"]["start_identity"] = "0" * 64
    lifecycle._atomic_json(ledger, value)
    with pytest.raises(lifecycle.LedgerError, match="no longer matches"):
        lifecycle.verified_pid(ledger, "tunnel")


def test_process_ledger_rejects_symlink_and_schema_drift(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    linked = tmp_path / "process-ledger.json"
    linked.symlink_to(target)
    with pytest.raises(lifecycle.LedgerError, match="unsafe"):
        lifecycle.load_ledger(linked)

    linked.unlink()
    linked.write_text(
        json.dumps({"schema": "hospes.synthetic-demo-process-ledger.v0"}),
        encoding="utf-8",
    )
    linked.chmod(0o600)
    with pytest.raises(lifecycle.LedgerError, match="malformed"):
        lifecycle.load_ledger(linked)


def test_process_ledger_rejects_unsafe_pid_and_noncanonical_identity(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "process-ledger.json"
    pids = {name: os.getpid() for name in lifecycle.PROCESS_NAMES}
    lifecycle.create_ledger(ledger, pids)
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value["processes"]["tunnel"]["pid"] = 1
    value["processes"]["tunnel"]["start_identity"] = "A" * 64
    lifecycle._atomic_json(ledger, value)
    with pytest.raises(lifecycle.LedgerError, match="malformed"):
        lifecycle.load_ledger(ledger)
