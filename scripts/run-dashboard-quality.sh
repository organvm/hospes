#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARTIFACT_DIR="${HOSPES_DASHBOARD_ARTIFACT_DIR:-$ROOT/artifacts/dashboard-quality}"
PORT="${HOSPES_DASHBOARD_PORT:-8765}"
mkdir -p "$ARTIFACT_DIR"

(
    trap - TERM
    exec env PYTHONUNBUFFERED=1 PYTHONPATH="$ROOT" \
        python3 -m hospes demo --open --no-browser --port "$PORT"
) >"$ARTIFACT_DIR/server.log" 2>&1 &
server_pid=$!

cleanup() {
    if kill -0 "$server_pid" 2>/dev/null; then
        kill -TERM "$server_pid" 2>/dev/null || true
        wait "$server_pid" || true
    fi
}
trap cleanup EXIT

for _attempt in $(seq 1 80); do
    if curl --fail --silent --show-error "http://127.0.0.1:$PORT/operator/login" >/dev/null; then
        break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
        sed -n '1,160p' "$ARTIFACT_DIR/server.log" >&2
        exit 1
    fi
    sleep 0.25
done

HOSPES_DASHBOARD_URL="http://127.0.0.1:$PORT" \
HOSPES_DASHBOARD_ARTIFACT_DIR="$ARTIFACT_DIR" \
node "$ROOT/scripts/dashboard-quality.mjs"

kill -TERM "$server_pid"
wait "$server_pid"
trap - EXIT
