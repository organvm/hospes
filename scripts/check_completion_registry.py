#!/usr/bin/env python3
"""Check or refresh documentation generated from the completion registry."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hospes import completion_registry  # noqa: E402


DOCUMENTS = (
    ROOT / "docs" / "ROADMAP.md",
    ROOT / "docs" / "plans" / "2026-08-10-hospes-1.0-completion.md",
)


def main() -> int:
    """Check or refresh selected in-repository completion projections.

    Reject escaping paths and missing documents. The historical projection homes
    remain the defaults; only explicit --write replaces stale managed blocks.
    """
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--document", action="append", help="repository-relative projection home; repeat for multiple homes")
    args = parser.parse_args()
    documents = tuple((ROOT / value).resolve() for value in args.document) if args.document else DOCUMENTS
    if any(not path.is_relative_to(ROOT) for path in documents):
        parser.error("projection documents must remain inside the repository")
    registry = completion_registry.load_registry()
    block = completion_registry.managed_document_block(registry)
    stale: list[Path] = []
    for path in documents:
        if not path.is_file():
            print(f"MISSING: {path.relative_to(ROOT)}")
            return 1
        current = path.read_text(encoding="utf-8")
        updated = completion_registry.replace_managed_block(current, block)
        if current == updated:
            continue
        stale.append(path)
        if args.write:
            path.write_text(updated, encoding="utf-8")
    if stale and args.check:
        for path in stale:
            print(f"completion registry projection is stale: {path.relative_to(ROOT)}")
        return 1
    action = "updated" if stale else "verified"
    print(f"completion registry {action}: {len(registry.issues)} issues, {len(documents)} documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
