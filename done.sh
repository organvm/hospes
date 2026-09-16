#!/usr/bin/env bash
# done.sh — HOSPES executable goal predicate
# exit 0 ⟺ all predicates green
# Run from repo root: ./done.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "=== HOSPES DONE PREDICATE ==="
echo ""

require_clean_worktree() {
    if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
        echo "done predicate requires a clean worktree" >&2
        git status --short >&2
        exit 1
    fi
}

echo "[0/13] clean starting worktree"
require_clean_worktree
echo ""

# --- Gate 1: operator dependencies ---
echo "[1/13] operator and exporter dependencies"
python3 -c 'import fastapi, httpx, jinja2, uvicorn'
echo ""

# --- Gate 2: full Python suite ---
echo "[2/13] canonical pytest coverage predicate"
bash scripts/test-all-scoped.sh
echo ""

# --- Gate 3: extracted browser policy helpers ---
echo "[3/13] Node capability/planner/payload/clipboard predicates"
node --test tests/synthetic_demo_ui.test.mjs
echo ""

# --- Gate 4: shell lifecycle predicates ---
echo "[4/13] shell lifecycle predicates"
bash tests/test_synthetic_demo_shell.sh
bash tests/test_whitespace_gate.sh
echo ""

# --- Gate 5: compile and syntax checks ---
echo "[5/13] Python compile and shell syntax"
python3 -m compileall -q hospes scripts
bash -n scripts/setup-synthetic-demo-cloudflare.sh
bash -n scripts/start-synthetic-demo-cloudflare.sh
bash -n scripts/stop-synthetic-demo-cloudflare.sh
bash -n scripts/synthetic_demo_shell_lifecycle.sh
bash -n scripts/record-demo.sh
node --check scripts/record-demo.mjs
node --check dashboard/assets/guide.js
python3 scripts/check_guide_registry.py
echo ""

# --- Gate 6: demo clean rerun ---
echo "[6/13] hospes demo twice (second run must be clean)"
status_before="$(git status --porcelain=v1 --untracked-files=all)"
PYTHONPATH=. python3 -m hospes demo
PYTHONPATH=. python3 -m hospes demo
status_after="$(git status --porcelain=v1 --untracked-files=all)"
if [[ "$status_before" != "$status_after" ]]; then
    echo "done predicate changed tracked or visible untracked files" >&2
    exit 1
fi
echo ""

# --- Gate 7: domain, configuration, and ask validation ---
echo "[7/13] hospes validate + configuration + ask ledger"
PYTHONPATH=. python3 -m hospes validate
PYTHONPATH=. python3 -m hospes config validate
PYTHONPATH=. python3 -m hospes capabilities >/dev/null
echo "[7a/13] installed-wheel acceptance"
bash scripts/verify-installed-wheel.sh
python3 scripts/check_asks.py --ledger docs/PUBLIC-ACCEPTANCE.md
python3 scripts/check_completion_registry.py --check --document docs/ROADMAP.md --document docs/public-completion.md
echo ""

# --- Gate 8: deferred review debt ---
# The shipped surface has had a predicate since 1.0; the DEFERRED surface had
# none, so `done.sh` reported green above 109 open review findings. This gate
# gives that backlog an executable owner. Counts are derived from the registry
# rows on every run, never stored, and held to an exact shrink-only ratchet.
# Live issue state (--github) is deliberately NOT requested here: `done.sh` must
# stay runnable offline and must not depend on `gh` auth. CI runs the networked
# form separately.
echo "[8/13] deferred review debt (hardening registry)"
python3 scripts/check_hardening.py --check
echo ""

# --- Gate 9: declared predicates ---
# `completion-registry.yaml` has named a predicate per issue and substrate since
# 1.0, and nothing ever ran them or checked that they exist — so the registry
# could point at a test file that was never written while this script stayed
# green. `--check` is the cheap, offline half: every declared predicate names an
# artifact that exists, or records why it cannot yet. Execution (`--run`) is
# heavier and belongs to CI; it reaches a Postgres bootstrap no other gate here
# invokes.
echo "[9/13] declared predicates exist or are explained"
python3 scripts/run_predicates.py --check
echo ""

# --- Gate 10: permission parity ---
# `spec/permission-matrix.yaml` declared six roles and their capabilities since
# 1.0 and no runtime code read it, while enforcement lived in nineteen
# module-local frozensets behind seven divergent helpers. Nothing compared the
# two, and they disagree at eight sites — every one in the direction of the code
# granting more than the contract. This gate does NOT enforce from the matrix
# (that would tighten eight sites in one flag day); it holds the divergence
# census to an exact baseline so the gap cannot grow while each case is decided.
echo "[10/13] permission parity (matrix vs enforced)"
python3 scripts/check_permissions.py --check
echo ""

# --- Gate 11: staged privacy boundary ---
echo "[11/13] committed-range and cached private-data scans"
python3 scripts/check_private_data.py
python3 scripts/check_private_data.py --cached
echo ""

# --- Gate 12: staged and unstaged whitespace ---
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[12/13] committed, staged, and unstaged whitespace"
    bash scripts/check_whitespace.sh
    echo ""
fi

echo "[13/13] clean ending worktree"
require_clean_worktree
echo ""

echo "HOSPES PUBLIC SOFTWARE DONE — all local predicates green; private pilot outcomes remain external"
