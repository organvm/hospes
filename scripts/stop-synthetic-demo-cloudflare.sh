#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CANONICAL_DEMO_DIR="$HOME/Library/Application Support/HOSPES/private_pilot/demo"
if [[ -n "${HOSPES_DEMO_DIR:-}" && "$HOSPES_DEMO_DIR" != "$CANONICAL_DEMO_DIR" ]]; then
  echo "HOSPES_DEMO_DIR must be the canonical private demo runtime" >&2
  exit 2
fi
CLOUDFLARE_DIR="$CANONICAL_DEMO_DIR/cloudflare"
LEDGER_PATH="$CLOUDFLARE_DIR/process-ledger.json"
LIFECYCLE_LOCK="$CLOUDFLARE_DIR/.lifecycle.lock.d"

if [[ ! -d "$CLOUDFLARE_DIR" || -L "$CLOUDFLARE_DIR" ]]; then
  echo "Synthetic demo is not running."
  exit 0
fi
if ! mkdir "$LIFECYCLE_LOCK" 2>/dev/null; then
  echo "synthetic demo lifecycle is locked by another operator" >&2
  exit 2
fi
cleanup_lifecycle_lock() {
  rmdir "$LIFECYCLE_LOCK" 2>/dev/null || true
}
trap cleanup_lifecycle_lock EXIT

if [[ ! -e "$LEDGER_PATH" ]]; then
  echo "Synthetic demo is not running."
  exit 0
fi
python3 "$ROOT/scripts/synthetic_demo_lifecycle.py" inspect \
  --ledger "$LEDGER_PATH" >/dev/null

terminate_ledger_process() {
  local process_name="$1"
  local process_pid=""
  if ! process_pid="$(
    python3 "$ROOT/scripts/synthetic_demo_lifecycle.py" verify \
      --ledger "$LEDGER_PATH" --name "$process_name" 2>/dev/null
  )"; then
    return 0
  fi
  kill -TERM "$process_pid" 2>/dev/null || true
  for _attempt in {1..40}; do
    if ! python3 "$ROOT/scripts/synthetic_demo_lifecycle.py" verify \
      --ledger "$LEDGER_PATH" --name "$process_name" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  if python3 "$ROOT/scripts/synthetic_demo_lifecycle.py" verify \
    --ledger "$LEDGER_PATH" --name "$process_name" >/dev/null 2>&1; then
    kill -KILL "$process_pid" 2>/dev/null || true
  fi
  for _attempt in {1..20}; do
    if ! python3 "$ROOT/scripts/synthetic_demo_lifecycle.py" verify \
      --ledger "$LEDGER_PATH" --name "$process_name" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "bounded stop could not prove lifecycle process exit: $process_name" >&2
  return 1
}

terminate_ledger_process tunnel
terminate_ledger_process caffeinate
terminate_ledger_process complete_operator
terminate_ledger_process review_operator
rm -f -- "$LEDGER_PATH"

echo "Synthetic demo lifecycle is stopped."
echo "Remote Access, DNS, and tunnel ingress configuration remains private and reusable."
