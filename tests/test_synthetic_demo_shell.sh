#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for script_path in \
  "$ROOT/scripts/setup-synthetic-demo-cloudflare.sh" \
  "$ROOT/scripts/start-synthetic-demo-cloudflare.sh" \
  "$ROOT/scripts/stop-synthetic-demo-cloudflare.sh" \
  "$ROOT/scripts/synthetic_demo_shell_lifecycle.sh"; do
  bash -n "$script_path"
done

grep -q 'env -i' "$ROOT/scripts/synthetic_demo_shell_lifecycle.sh"
grep -q 'synthetic_demo_failed_start' "$ROOT/scripts/start-synthetic-demo-cloudflare.sh"
grep -q 'unset CLOUDFLARE_API_TOKEN' "$ROOT/scripts/start-synthetic-demo-cloudflare.sh"
grep -q 'process-ledger.json' "$ROOT/scripts/start-synthetic-demo-cloudflare.sh"
grep -q 'process-start identity' "$ROOT/config/domain_kernel.yaml"
grep -q -- '--verify-only' "$ROOT/scripts/setup-synthetic-demo-cloudflare.sh"
grep -q -- '--secure-session-cookie' "$ROOT/scripts/start-synthetic-demo-cloudflare.sh"
if grep -Eq 'rm .*config\.yml|delete.*tunnel|tunnel delete' \
  "$ROOT/scripts/stop-synthetic-demo-cloudflare.sh"; then
  echo "stop script must retain remote and local tunnel configuration" >&2
  exit 1
fi

test_root="$(mktemp -d "${TMPDIR:-/tmp}/hospes-shell-test.XXXXXX")"
test_pids=()
cleanup() {
  local test_pid
  # Bash 3.2 treats an initialized empty array as unset under `set -u`.
  for test_pid in "${test_pids[@]-}"; do
    if [[ "$test_pid" =~ ^[0-9]+$ ]] && (( test_pid > 1 )); then
      kill -KILL "$test_pid" 2>/dev/null || true
    fi
  done
  rm -rf -- "$test_root"
}
trap cleanup EXIT
private_cloudflare="$test_root/Library/Application Support/HOSPES/private_pilot/demo/cloudflare"
mkdir -p "$private_cloudflare"

source "$ROOT/scripts/synthetic_demo_shell_lifecycle.sh"
export CLOUDFLARE_API_TOKEN="fixture-secret-must-not-inherit"
export HOSPES_DEMO_OWNER_EMAIL="owner-secret-must-not-inherit"
clean_environment="$(
  synthetic_demo_run_clean_env "$ROOT" \
    "HOSPES_OPERATOR_TOKEN=fixture-operator-token" \
    python3 -c \
      'import os; print(",".join(("operator" if os.environ.get("HOSPES_OPERATOR_TOKEN") else "missing", "leak" if os.environ.get("CLOUDFLARE_API_TOKEN") else "clean", "leak" if os.environ.get("HOSPES_DEMO_OWNER_EMAIL") else "clean")))'
)"
unset CLOUDFLARE_API_TOKEN HOSPES_DEMO_OWNER_EMAIL
if [[ "$clean_environment" != "operator,clean,clean" ]]; then
  echo "whitelisted child environment inherited a setup secret" >&2
  exit 1
fi

continued_path="$test_root/continued-after-failure"
failed_ledger="$test_root/failed-ledger.json"
printf '{}\n' >"$failed_ledger"
printf '{}\n' >"$test_root/.failed-ledger.json.tmp"
sleep 60 &
failed_pid=$!
test_pids+=("$failed_pid")
set +e
(
  set -Eeuo pipefail
  started_pids=("$failed_pid")
  trap 'synthetic_demo_failed_start "$?" "$failed_ledger" "${started_pids[@]}"' ERR
  false
  touch "$continued_path"
)
failed_status=$?
set -e
if [[ "$failed_status" -eq 0 || -e "$continued_path" || -e "$failed_ledger" \
  || -e "$test_root/.failed-ledger.json.tmp" ]]; then
  echo "failed-start cleanup was not terminal" >&2
  exit 1
fi
if kill -0 "$failed_pid" 2>/dev/null; then
  echo "failed-start cleanup left its child running" >&2
  exit 1
fi
wait "$failed_pid" 2>/dev/null || true
test_pids=()

touch "$private_cloudflare/config.yml"
lifecycle_pids=()
for _process_name in review complete tunnel caffeinate; do
  sleep 60 &
  lifecycle_pid=$!
  lifecycle_pids+=("$lifecycle_pid")
  test_pids+=("$lifecycle_pid")
done
python3 "$ROOT/scripts/synthetic_demo_lifecycle.py" create \
  --ledger "$private_cloudflare/process-ledger.json" \
  --review-operator-pid "${lifecycle_pids[0]}" \
  --complete-operator-pid "${lifecycle_pids[1]}" \
  --tunnel-pid "${lifecycle_pids[2]}" \
  --caffeinate-pid "${lifecycle_pids[3]}"
HOME="$test_root" "$ROOT/scripts/stop-synthetic-demo-cloudflare.sh" >/dev/null
for lifecycle_pid in "${lifecycle_pids[@]}"; do
  if kill -0 "$lifecycle_pid" 2>/dev/null; then
    echo "bounded stop left a ledger-owned child running" >&2
    exit 1
  fi
  wait "$lifecycle_pid" 2>/dev/null || true
done
test_pids=()
if [[ -e "$private_cloudflare/process-ledger.json" ]]; then
  echo "bounded stop retained the process ledger" >&2
  exit 1
fi
if [[ ! -f "$private_cloudflare/config.yml" ]]; then
  echo "bounded stop removed retained tunnel configuration" >&2
  exit 1
fi
HOME="$test_root" "$ROOT/scripts/stop-synthetic-demo-cloudflare.sh" >/dev/null
if [[ -e "$private_cloudflare/.lifecycle.lock.d" ]]; then
  echo "idempotent stop left the lifecycle lock behind" >&2
  exit 1
fi

echo "synthetic demo shell predicates: ok"
