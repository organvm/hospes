#!/usr/bin/env python3
"""Project the hardening registry onto its GitHub issues.

Sibling of `sync_github_issues.py`, which does the same for the completion
registry. The attribution and severity re-anchoring recorded in
`hardening-registry.yaml` are only useful where the work happens — on the
issues themselves — so this writes a delimited managed block into each one.

Three properties make that safe to run repeatedly:

  - `replace_managed_block` swaps ONLY the delimited region, so the reviewer
    prose and the 109 checkboxes above it are never touched.
  - The block is idempotent: rendering it twice produces the same bytes, so a
    no-op sync writes nothing and reports `verified`.
  - Titles are NOT managed here. `sync_github_issues.py` overwrites the title
    from the completion registry because that registry owns it; this registry
    does not own hardening issue titles and must not clobber them.

`--check` is read-only and exits 1 when any issue's projection is stale, so CI
can hold the projection to the registry. `--write` applies. `--diff` shows what
would change without touching anything.
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hospes.hardening_registry import (  # noqa: E402
    EXPECTED_ISSUES,
    HardeningRegistryError,
    load_registry,
    managed_issue_block,
    replace_managed_block,
)


def _view(repository: str, number: int) -> dict:
    result = subprocess.run(
        ("gh", "issue", "view", str(number), "--repo", repository, "--json", "number,body,state"),
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read-only; exit 1 if any projection is stale")
    mode.add_argument("--write", action="store_true", help="apply the managed block to each issue")
    mode.add_argument("--diff", action="store_true", help="read-only; print the change each issue would receive")
    args = parser.parse_args()

    try:
        registry = load_registry()
    except HardeningRegistryError as exc:
        print(f"hardening registry did not load: {exc}", file=sys.stderr)
        return 1

    stale: list[int] = []
    for number in sorted(EXPECTED_ISSUES):
        try:
            live = _view(registry.repository, number)
        except subprocess.CalledProcessError as exc:
            print(f"cannot read issue #{number}: {exc.stderr.strip()[:160]}", file=sys.stderr)
            return 1

        current = str(live["body"] or "")
        wanted = replace_managed_block(current, managed_issue_block(registry, number))
        if current == wanted:
            continue
        stale.append(number)

        if args.diff:
            print(f"--- issue #{number} (live)")
            print(f"+++ issue #{number} (registry)")
            for line in difflib.unified_diff(current.splitlines(), wanted.splitlines(), lineterm="", n=1):
                if line.startswith(("---", "+++")):
                    continue
                print(line)
            print("")
        elif args.write:
            subprocess.run(
                ("gh", "issue", "edit", str(number), "--repo", registry.repository, "--body", wanted),
                check=True,
                capture_output=True,
            )
            print(f"  updated #{number} ({len(registry.for_issue(number))} findings)")

    if stale and args.check:
        print("hardening GitHub projection is stale: " + ", ".join(f"#{n}" for n in stale))
        return 1
    action = "updated" if (stale and args.write) else ("stale" if stale else "verified")
    print(f"hardening GitHub projection {action}: {len(EXPECTED_ISSUES)} issues, {len(registry.findings)} findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
