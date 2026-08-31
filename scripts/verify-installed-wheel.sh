#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hospes-wheel.XXXXXX")"
trap 'rm -rf "$TEMP_ROOT"' EXIT

mkdir "$TEMP_ROOT/source"
rsync -a \
  --exclude .git \
  --exclude .venv \
  --exclude build \
  --exclude dist \
  --exclude out \
  "$ROOT/" "$TEMP_ROOT/source/"
python3 -m pip wheel "$TEMP_ROOT/source" --no-deps --wheel-dir "$TEMP_ROOT/wheels" >/dev/null
python3 -m venv "$TEMP_ROOT/venv"
"$TEMP_ROOT/venv/bin/python" -m pip install "$TEMP_ROOT"/wheels/hospes-*.whl >/dev/null
mkdir "$TEMP_ROOT/unrelated"
cd "$TEMP_ROOT/unrelated"

"$TEMP_ROOT/venv/bin/python" - <<'PY'
import json
from importlib.resources import files
from hospes import artifacts, authentication, completion_registry, encryption, jobs

resources = files("hospes.resources")
states = json.loads(resources.joinpath("spec/states.json").read_text(encoding="utf-8"))
assert len(states["main_states"]) == 26
assert len(states["branch_states"]) == 8
for relative in (
    "dashboard/assets/api.js",
    "dashboard/assets/bootstrap.js",
    "dashboard/assets/planner.mjs",
    "config/runtime.yaml",
    "config/outreach_templates.yaml",
    "config/voice.yaml",
    "briefs/guest-packet.html",
    "spec/completion-registry.yaml",
):
    assert resources.joinpath(relative).is_file(), relative
assert len(completion_registry.load_registry().issues) == 17
assert encryption.ALGORITHM == "AES-256-GCM"
assert isinstance(artifacts.LocalArtifactStore, type)
assert authentication.ACCESS_HEADER == "Cf-Access-Jwt-Assertion"
assert jobs.POLICY_VERSION == "jobs-v1"
PY

"$TEMP_ROOT/venv/bin/python" -m hospes validate
"$TEMP_ROOT/venv/bin/python" -m hospes demo
"$TEMP_ROOT/venv/bin/python" -m hospes export-pilot-packet pilot-a --format html
if "$TEMP_ROOT/venv/bin/python" -c 'import weasyprint' >/dev/null 2>&1; then
  "$TEMP_ROOT/venv/bin/python" -m hospes export-pilot-packet pilot-a --format pdf
fi

echo "installed wheel acceptance passed"
