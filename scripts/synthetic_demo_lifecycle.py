#!/usr/bin/env python3
"""Atomic process-ledger operations for the synthetic demo lifecycle."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "hospes.synthetic-demo-process-ledger.v1"
PROCESS_NAMES = (
    "review_operator",
    "complete_operator",
    "tunnel",
    "caffeinate",
)
LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LedgerError(RuntimeError):
    pass


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _darwin_start_identity(pid: int) -> str:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _ProcBsdInfo()
    size = proc_pidinfo(
        pid,
        3,  # PROC_PIDTBSDINFO
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if size != ctypes.sizeof(info) or info.pbi_start_tvsec == 0:
        raise LedgerError("process start identity is unavailable")
    return f"libproc:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"


def process_start_identity(pid: int) -> str:
    if pid <= 1:
        raise LedgerError("process id is unsafe")
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise LedgerError("process is not running") from exc
    proc_stat = Path(f"/proc/{pid}/stat")
    # Some container runtimes expose the current process through `/proc/self`
    # while hiding its namespace-local numeric PID from `/proc/<pid>`. Preserve
    # start-time binding for the current process instead of falling through to
    # `ps`, which observes the host namespace and cannot resolve that PID.
    if pid == os.getpid() and not proc_stat.is_file():
        self_stat = Path("/proc/self/stat")
        if self_stat.is_file():
            proc_stat = self_stat
    if proc_stat.is_file():
        fields = proc_stat.read_text(encoding="utf-8").split()
        if len(fields) < 22:
            raise LedgerError("process start identity is unavailable")
        raw = f"proc:{fields[21]}"
    elif sys.platform == "darwin":
        raw = _darwin_start_identity(pid)
    else:
        try:
            completed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LedgerError("process start identity is unavailable") from exc
        raw = f"ps:{completed.stdout.strip()}"
        if completed.returncode != 0 or raw == "ps:":
            raise LedgerError("process start identity is unavailable")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise LedgerError("process ledger cannot be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = (
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    path.chmod(0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_ledger(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LedgerError("process ledger is missing or unsafe")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise LedgerError("process ledger permissions are unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError("process ledger is malformed") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA
        or set(value.get("processes", {})) != set(PROCESS_NAMES)
        or value.get("ports")
        != {"review_operator": 8765, "complete_operator": 8766, "metrics": 8767}
    ):
        raise LedgerError("process ledger is malformed")
    for name, process in value["processes"].items():
        if (
            not isinstance(process, dict)
            or not isinstance(process.get("pid"), int)
            or process["pid"] <= 1
            or not isinstance(process.get("start_identity"), str)
            or LOWER_SHA256.fullmatch(process["start_identity"]) is None
        ):
            raise LedgerError(f"process ledger entry is malformed: {name}")
    return value


def create_ledger(path: Path, pids: dict[str, int]) -> None:
    if set(pids) != set(PROCESS_NAMES):
        raise LedgerError("all lifecycle processes are required")
    value = {
        "schema": SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "processes": {
            name: {
                "pid": pid,
                "start_identity": process_start_identity(pid),
            }
            for name, pid in pids.items()
        },
        "ports": {
            "review_operator": 8765,
            "complete_operator": 8766,
            "metrics": 8767,
        },
    }
    _atomic_json(path, value)


def verified_pid(path: Path, name: str) -> int:
    ledger = load_ledger(path)
    process = ledger["processes"].get(name)
    if process is None:
        raise LedgerError("unknown lifecycle process")
    pid = int(process["pid"])
    if process_start_identity(pid) != process["start_identity"]:
        raise LedgerError("process identity no longer matches the ledger")
    return pid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--ledger", type=Path, required=True)
    for name in PROCESS_NAMES:
        create.add_argument(f"--{name.replace('_', '-')}-pid", type=int, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--ledger", type=Path, required=True)
    verify.add_argument("--name", choices=PROCESS_NAMES, required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--ledger", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "create":
            create_ledger(
                args.ledger,
                {
                    name: int(getattr(args, f"{name}_pid"))
                    for name in PROCESS_NAMES
                },
            )
        elif args.command == "verify":
            print(verified_pid(args.ledger, args.name))
        else:
            ledger = load_ledger(args.ledger)
            print(
                json.dumps(
                    {
                        "schema": ledger["schema"],
                        "process_names": sorted(ledger["processes"]),
                    },
                    sort_keys=True,
                )
            )
        return 0
    except LedgerError as exc:
        print(f"[process-ledger] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
