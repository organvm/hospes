#!/usr/bin/env python3
"""Check or sync managed GitHub issue blocks without replacing human requirements."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hospes import completion_registry  # noqa: E402


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    registry = completion_registry.load_registry()
    stale: list[int] = []
    for issue in registry.issues:
        result = _run(
            "gh",
            "issue",
            "view",
            str(issue.number),
            "--repo",
            registry.repository,
            "--json",
            "title,body",
        )
        live = json.loads(result.stdout)
        wanted_body = completion_registry.replace_managed_block(
            str(live["body"]), completion_registry.managed_issue_block(registry, issue.number)
        )
        if live["title"] == issue.title and live["body"] == wanted_body:
            continue
        stale.append(issue.number)
        if args.write:
            subprocess.run(
                (
                    "gh",
                    "issue",
                    "edit",
                    str(issue.number),
                    "--repo",
                    registry.repository,
                    "--title",
                    issue.title,
                    "--body",
                    wanted_body,
                ),
                check=True,
            )
    if stale and args.check:
        print("completion registry GitHub projection is stale: " + ", ".join(f"#{number}" for number in stale))
        return 1
    action = "updated" if stale else "verified"
    print(f"completion registry GitHub projection {action}: {len(registry.issues)} issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
