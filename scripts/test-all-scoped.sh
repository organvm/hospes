#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Repository-scoped full suite. Keeping this bounded wrapper explicit lets the
# host guard distinguish HOSPES verification from an unbounded machine probe.
python3 -m pytest tests --cov=hospes --cov-report=term-missing --cov-fail-under=90 -W error::ResourceWarning
