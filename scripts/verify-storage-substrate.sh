#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m pytest tests/test_migrations.py tests/test_database_backends.py -q
bash scripts/verify-postgres-bootstrap.sh
python3 scripts/check_completion_registry.py --check

echo "storage and migration substrate acceptance passed"
