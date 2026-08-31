#!/usr/bin/env python3
"""Execute the predicates `completion-registry.yaml` has always declared.

The registry has named a `predicate` for every issue and substrate since 1.0.
Nothing had ever run them, and nothing had ever checked that the artifacts they
name exist — so the registry could point at `tests/issue_predicates/
test_issue_09.py`, which does not exist, while `done.sh` stayed green. A
declared predicate nobody executes is a claim, not a check.

Two modes, because they answer different questions:

  --check  Does every declared predicate's artifact EXIST (or is its absence
           explained)? Cheap, offline, no test execution. This is the form
           `done.sh` runs. It catches the failure the registry actually had:
           declared-but-missing.

  --run    Execute each predicate and report pass/fail. Heavier, and it reaches
           coverage `done.sh` does not otherwise run — `verify-storage-substrate.sh`
           includes a Postgres bootstrap that no other gate invokes.

A missing artifact is a failure UNLESS the registry records a
`predicate_absent_reason`. That escape hatch exists for exactly one shape: an
issue that turns on a human-gated real-world event, where writing a passing test
today would fabricate the receipt the close condition demands. It is checked in
both directions — a reason that outlives the artifact's arrival is drift, and
reds, so the explanation cannot quietly become permanent.

Exit 0 ⟺ every declared predicate is accounted for (and, under --run, passes).
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hospes.completion_registry import (  # noqa: E402
    CompletionRegistryError,
    load_registry,
)

# Artifacts a predicate command can name. Anything else is reported as
# unrecognised rather than silently skipped — an unparsed command is an
# unchecked one, and that is the failure this script exists to end.
ARTIFACT = re.compile(r"^[\w./-]+\.(py|sh)$")


@dataclass(frozen=True)
class Declared:
    key: str
    command: str
    absent_reason: str | None


def declared_predicates(registry) -> list[Declared]:
    out = [
        Declared(key=name, command=body["predicate"], absent_reason=None)
        for name, body in sorted(registry.substrates.items())
    ]
    out.extend(
        Declared(
            key=f"issue:{issue.number}",
            command=issue.predicate,
            absent_reason=issue.predicate_absent_reason,
        )
        for issue in registry.issues
    )
    return out


def artifacts_for(command: str) -> list[str]:
    return [token for token in shlex.split(command) if ARTIFACT.match(token)]


def resolve(command: str) -> list[str]:
    """Rewrite `python` to the running interpreter.

    The registry stores `python -m pytest ...`, but a bare `python` does not
    exist on a stock macOS toolchain, so executing the string verbatim would
    fail for a reason that has nothing to do with the predicate.
    """
    argv = shlex.split(command)
    if argv and argv[0] == "python":
        argv[0] = sys.executable
    return argv


def check(registry) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    for item in declared_predicates(registry):
        refs = artifacts_for(item.command)
        if not refs:
            problems.append(f"{item.key}: no artifact recognised in predicate {item.command!r}")
            continue
        missing = [ref for ref in refs if not (ROOT / ref).exists()]
        if missing and item.absent_reason:
            notes.append(f"{item.key}: {', '.join(missing)} absent — explained")
        elif missing:
            problems.append(
                f"{item.key}: declares {', '.join(missing)}, which does not exist "
                "(record a predicate_absent_reason, or write the predicate)"
            )
        elif item.absent_reason:
            problems.append(
                f"{item.key}: carries a predicate_absent_reason but {', '.join(refs)} now exists — "
                "delete the reason in the change that added the artifact"
            )
    return problems, notes


def run(registry, timeout: int) -> list[str]:
    failures: list[str] = []
    for item in declared_predicates(registry):
        refs = artifacts_for(item.command)
        if refs and any(not (ROOT / ref).exists() for ref in refs):
            print(f"  SKIP  {item.key} — artifact absent" + (" (explained)" if item.absent_reason else ""))
            continue
        try:
            result = subprocess.run(
                resolve(item.command), cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{item.key}: {type(exc).__name__} while executing")
            print(f"  ERROR {item.key} — {type(exc).__name__}")
            continue
        if result.returncode == 0:
            print(f"  PASS  {item.key}")
        else:
            tail = (result.stdout or result.stderr).strip().splitlines()[-3:]
            failures.append(f"{item.key}: exit {result.returncode}")
            print(f"  FAIL  {item.key} — exit {result.returncode}")
            for line in tail:
                print(f"          {line}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify artifacts exist; exit non-zero on any gap")
    parser.add_argument("--run", action="store_true", help="execute every declared predicate")
    parser.add_argument("--timeout", type=int, default=900, help="per-predicate timeout in seconds")
    parser.add_argument("--registry", default=None, help="registry path override")
    args = parser.parse_args()

    print("=== HOSPES DECLARED PREDICATES ===")
    try:
        registry = load_registry(args.registry)
    except CompletionRegistryError as exc:
        print(f"  FAIL  registry did not load: {exc}")
        return 1

    declared = declared_predicates(registry)
    print(
        f"declared     {len(declared)} predicates ({len(registry.substrates)} substrate, {len(registry.issues)} issue)"
    )

    problems, notes = check(registry)
    for note in notes:
        print(f"  NOTE  {note}")
    print(f"[A] artifacts     {'OK' if not problems else 'FAIL'} — every declared predicate is accounted for")

    failures: list[str] = []
    if args.run:
        print("")
        print("executing:")
        failures = run(registry, args.timeout)
        print("")
        print(f"[B] execution     {'OK' if not failures else 'FAIL'} — every runnable predicate passes")

    if problems or failures:
        print("")
        for line in problems + failures:
            print(f"  FAIL  {line}")
        print("")
        print(f"declared-predicate gate FAILED — {len(problems) + len(failures)} problem(s)")
        return 1 if (args.check or args.run) else 0

    print("")
    print("declared-predicate gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
