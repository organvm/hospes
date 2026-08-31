#!/usr/bin/env bash
# record-demo.sh — boot the marked synthetic demo, film a walkthrough, render an mp4.
#
#   HOSPES_DEMO_CUT=proof    scripts/record-demo.sh  # fast, uncaptioned evidence cut
#   HOSPES_DEMO_CUT=pitch    scripts/record-demo.sh  # paced, captioned walkthrough
#   HOSPES_DEMO_CUT=tutorial scripts/record-demo.sh  # films the shipped guided tour
#
# HOSPES_DEMO_PERSONA selects the audience the tour addresses
# (owner|ari|investor|newcomer); HOSPES_DEMO_DEPTH overrides its narration depth.
#
# The video is a build product (artifacts/ is gitignored); the recorder is the
# durable artifact. Re-running reproduces an equivalent film from a clean checkout.
#
# The operator token is generated per run and lives only in this process's
# environment — never in argv, never on disk.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIDEO_DIR="${HOSPES_DEMO_VIDEO_DIR:-$ROOT/artifacts/demo-video}"
PORT="${HOSPES_DEMO_PORT:-8765}"
CUT="${HOSPES_DEMO_CUT:-proof}"
PERSONA="${HOSPES_DEMO_PERSONA:-newcomer}"

case "$CUT" in
    proof | pitch | tutorial) ;;
    *)
        echo "HOSPES_DEMO_CUT must be 'proof', 'pitch', or 'tutorial' (got '$CUT')" >&2
        exit 2
        ;;
esac

mkdir -p "$VIDEO_DIR"

if [[ -z "${HOSPES_OPERATOR_TOKEN:-}" ]]; then
    # allow-secret: ephemeral, single-run loopback credential; never printed or persisted.
    HOSPES_OPERATOR_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    export HOSPES_OPERATOR_TOKEN
fi

# Private-field custody is a real capability gate: without a master key the
# encrypted surfaces (contact rosters, touchpoint receipts) stay hidden. The
# synthetic bundle holds no real data, so the recorder mints a throwaway 32-byte
# key per run rather than faking the gate open with a stubbed response.
if [[ -z "${HOSPES_MASTER_KEY_B64:-}" ]]; then
    # allow-secret: ephemeral synthetic-demo key; discarded when this process exits.
    HOSPES_MASTER_KEY_B64="$(python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
    export HOSPES_MASTER_KEY_B64
fi

(
    trap - TERM
    exec env PYTHONUNBUFFERED=1 PYTHONPATH="$ROOT" \
        python3 -m hospes demo --open --no-browser --port "$PORT" --persona "$PERSONA"
) >"$VIDEO_DIR/server.log" 2>&1 &
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
        echo "synthetic demo server exited before becoming ready:" >&2
        sed -n '1,160p' "$VIDEO_DIR/server.log" >&2
        exit 1
    fi
    sleep 0.25
done

HOSPES_DEMO_URL="http://127.0.0.1:$PORT" \
HOSPES_DEMO_VIDEO_DIR="$VIDEO_DIR" \
HOSPES_DEMO_CUT="$CUT" \
HOSPES_DEMO_PERSONA="$PERSONA" \
    node "$ROOT/scripts/record-demo.mjs"

kill -TERM "$server_pid"
wait "$server_pid" || true
trap - EXIT

webm="$VIDEO_DIR/hospes-demo-$CUT.webm"
mp4="$VIDEO_DIR/hospes-demo-$CUT.mp4"

if [[ ! -f "$webm" ]]; then
    echo "recorder produced no video at $webm" >&2
    exit 1
fi

if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -y -loglevel error -i "$webm" \
        -c:v libx264 -preset slow -crf 23 -pix_fmt yuv420p -movflags +faststart \
        "$mp4"
    echo "recorded: $mp4"
else
    echo "ffmpeg not found; leaving the webm only: $webm" >&2
    echo "recorded: $webm"
fi
