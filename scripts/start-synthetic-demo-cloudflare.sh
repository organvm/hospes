#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/synthetic_demo_shell_lifecycle.sh"
CANONICAL_DEMO_DIR="$HOME/Library/Application Support/HOSPES/private_pilot/demo"
if [[ -n "${HOSPES_DEMO_DIR:-}" && "$HOSPES_DEMO_DIR" != "$CANONICAL_DEMO_DIR" ]]; then
  echo "HOSPES_DEMO_DIR must be the canonical private demo runtime" >&2
  exit 2
fi
DEMO_DIR="$CANONICAL_DEMO_DIR"
CLOUDFLARE_DIR="$DEMO_DIR/cloudflare"
REVIEW_DB="$DEMO_DIR/ari-review.synthetic-demo.sqlite3"
COMPLETE_DB="$DEMO_DIR/ari-complete.synthetic-demo.sqlite3"
CONFIG_PATH="$CLOUDFLARE_DIR/config.yml"
ACCESS_RECEIPT="$CLOUDFLARE_DIR/access-ready.json"
LEDGER_PATH="$CLOUDFLARE_DIR/process-ledger.json"
LIFECYCLE_LOCK="$CLOUDFLARE_DIR/.lifecycle.lock.d"

if [[ ! -d "$CLOUDFLARE_DIR" || -L "$CLOUDFLARE_DIR" ]]; then
  echo "canonical Cloudflare private runtime is missing or unsafe" >&2
  exit 2
fi
if ! mkdir "$LIFECYCLE_LOCK" 2>/dev/null; then
  echo "synthetic demo lifecycle is locked by another operator" >&2
  exit 2
fi
cleanup_lifecycle_lock() {
  rmdir "$LIFECYCLE_LOCK" 2>/dev/null || true
}
trap cleanup_lifecycle_lock EXIT

for command_name in python3 cloudflared curl lsof caffeinate; do
  command -v "$command_name" >/dev/null || {
    echo "required command is missing: $command_name" >&2
    exit 2
  }
done
for required_path in "$REVIEW_DB" "$COMPLETE_DB" "$CONFIG_PATH" "$ACCESS_RECEIPT"; do
  if [[ ! -f "$required_path" || -L "$required_path" ]]; then
    echo "required private demo artifact is missing or unsafe" >&2
    exit 2
  fi
done
if [[ -e "$LEDGER_PATH" ]]; then
  echo "a process ledger already exists; run the bounded stop command first" >&2
  exit 2
fi
for port in 8765 8766 8767; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "a required loopback port is already in use" >&2
    exit 2
  fi
done

DEMO_DIR="$DEMO_DIR" PYTHONPATH="$ROOT" python3 -c \
  'import os; from hospes.synthetic_demo import validate_synthetic_bundle; validate_synthetic_bundle(os.environ["DEMO_DIR"])'
"$ROOT/scripts/setup-synthetic-demo-cloudflare.sh" --verify-only

if [[ -z "${HOSPES_OPERATOR_TOKEN:-}" ]]; then
  IFS= read -r -s -p "Session-only HOSPES operator token: " HOSPES_OPERATOR_TOKEN
  printf '\n'
fi
if [[ ${#HOSPES_OPERATOR_TOKEN} -lt 16 || "$HOSPES_OPERATOR_TOKEN" =~ [[:space:]] ]]; then
  echo "operator token must be at least 16 non-whitespace characters" >&2
  unset HOSPES_OPERATOR_TOKEN
  exit 2
fi
operator_token="$HOSPES_OPERATOR_TOKEN"
unset HOSPES_OPERATOR_TOKEN
unset CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID
unset HOSPES_DEMO_ZONE HOSPES_DEMO_OWNER_EMAIL HOSPES_DEMO_ARI_EMAIL
unset HOSPES_DEMO_TUNNEL_ID HOSPES_DEMO_TUNNEL_NAME CLOUDFLARED_ORIGIN_CERT

chmod 700 "$DEMO_DIR" "$CLOUDFLARE_DIR"
started_pids=()
trap 'synthetic_demo_failed_start "$?" "$LEDGER_PATH" "${started_pids[@]}"' ERR

synthetic_demo_run_clean_env "$ROOT" "HOSPES_OPERATOR_TOKEN=$operator_token" \
  python3 -m hospes operator --db "$REVIEW_DB" --port 8765 \
  --actor ari_demo_owner --role relationship_owner --tenant ari_demo_review \
  --synthetic-demo --secure-session-cookie \
  >"$CLOUDFLARE_DIR/review-operator.log" 2>&1 &
review_pid=$!
started_pids+=("$review_pid")
synthetic_demo_run_clean_env "$ROOT" "HOSPES_OPERATOR_TOKEN=$operator_token" \
  python3 -m hospes operator --db "$COMPLETE_DB" --port 8766 \
  --actor ari_demo_owner --role relationship_owner --tenant ari_demo_complete \
  --synthetic-demo --secure-session-cookie \
  >"$CLOUDFLARE_DIR/complete-operator.log" 2>&1 &
complete_pid=$!
started_pids+=("$complete_pid")
unset operator_token

operators_ready=0
for _attempt in {1..80}; do
  if ! kill -0 "$review_pid" 2>/dev/null || ! kill -0 "$complete_pid" 2>/dev/null; then
    break
  fi
  if curl --silent --fail --max-time 1 http://127.0.0.1:8765/health >/dev/null \
    && curl --silent --fail --max-time 1 http://127.0.0.1:8766/health >/dev/null \
    && lsof -nP -a -p "$review_pid" -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 \
    && lsof -nP -a -p "$complete_pid" -iTCP:8766 -sTCP:LISTEN >/dev/null 2>&1; then
    operators_ready=1
    break
  fi
  sleep 0.25
done
if [[ "$operators_ready" -ne 1 ]]; then
  echo "loopback operators did not prove bounded readiness and port ownership" >&2
  synthetic_demo_failed_start 1 "$LEDGER_PATH" "${started_pids[@]}"
fi

synthetic_demo_run_clean_env "$ROOT" cloudflared tunnel --config "$CONFIG_PATH" run \
  >"$CLOUDFLARE_DIR/tunnel.log" 2>&1 &
tunnel_pid=$!
started_pids+=("$tunnel_pid")
connector_ready=0
for _attempt in {1..120}; do
  if ! kill -0 "$tunnel_pid" 2>/dev/null; then
    break
  fi
  if curl --silent --fail --max-time 1 http://127.0.0.1:8767/ready >/dev/null \
    && lsof -nP -a -p "$tunnel_pid" -iTCP:8767 -sTCP:LISTEN >/dev/null 2>&1; then
    connector_ready=1
    break
  fi
  sleep 0.25
done
if [[ "$connector_ready" -ne 1 ]]; then
  echo "tunnel connector did not prove bounded readiness" >&2
  synthetic_demo_failed_start 1 "$LEDGER_PATH" "${started_pids[@]}"
fi

synthetic_demo_run_clean_env "$ROOT" caffeinate -w "$tunnel_pid" \
  >"$CLOUDFLARE_DIR/caffeinate.log" 2>&1 &
caffeinate_pid=$!
started_pids+=("$caffeinate_pid")
kill -0 "$caffeinate_pid"

python3 "$ROOT/scripts/synthetic_demo_lifecycle.py" create \
  --ledger "$LEDGER_PATH" \
  --review-operator-pid "$review_pid" \
  --complete-operator-pid "$complete_pid" \
  --tunnel-pid "$tunnel_pid" \
  --caffeinate-pid "$caffeinate_pid"
chmod 600 "$CLOUDFLARE_DIR"/*.log "$LEDGER_PATH"
started_pids=()
trap - ERR

echo "Synthetic demo operators and exact Access tunnel are running."
echo "Stop with scripts/stop-synthetic-demo-cloudflare.sh; remote configuration is retained."
