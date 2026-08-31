#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CANONICAL_DEMO_DIR="$HOME/Library/Application Support/HOSPES/private_pilot/demo"
if [[ -n "${HOSPES_DEMO_DIR:-}" && "$HOSPES_DEMO_DIR" != "$CANONICAL_DEMO_DIR" ]]; then
  echo "HOSPES_DEMO_DIR must be the canonical private demo runtime" >&2
  exit 2
fi
DEMO_DIR="$CANONICAL_DEMO_DIR"
CLOUDFLARE_DIR="$DEMO_DIR/cloudflare"
CERT_PATH="${CLOUDFLARED_ORIGIN_CERT:-$HOME/.cloudflared/cert.pem}"
CREDENTIALS_PATH="$CLOUDFLARE_DIR/tunnel-credentials.json"
TUNNEL_NAME="${HOSPES_DEMO_TUNNEL_NAME:-hospes-ari-synthetic-demo}"
VERIFY_ONLY=0

if [[ "${1:-}" == "--verify-only" ]]; then
  VERIFY_ONLY=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "usage: scripts/setup-synthetic-demo-cloudflare.sh [--verify-only]" >&2
  exit 2
fi

for command_name in cloudflared python3; do
  command -v "$command_name" >/dev/null || {
    echo "required command is missing: $command_name" >&2
    exit 2
  }
done
for variable_name in CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID HOSPES_DEMO_ZONE HOSPES_DEMO_OWNER_EMAIL HOSPES_DEMO_ARI_EMAIL; do
  if [[ -z "${!variable_name:-}" ]]; then
    echo "$variable_name is required in the environment" >&2
    exit 2
  fi
done

if [[ "$VERIFY_ONLY" -eq 0 ]]; then
  mkdir -p "$CLOUDFLARE_DIR"
fi
if [[ ! -d "$CLOUDFLARE_DIR" || -L "$CLOUDFLARE_DIR" ]]; then
  echo "canonical Cloudflare private runtime is missing or unsafe" >&2
  exit 2
fi
chmod 700 "$DEMO_DIR" "$CLOUDFLARE_DIR"

if [[ "$VERIFY_ONLY" -eq 0 ]]; then
  if [[ ! -f "$CERT_PATH" || -L "$CERT_PATH" ]]; then
    echo "Cloudflare origin authentication is a human gate." >&2
    echo "Run cloudflared tunnel login, then rerun setup." >&2
    exit 2
  fi
  cert_mode="$(stat -f '%Lp' "$CERT_PATH" 2>/dev/null || stat -c '%a' "$CERT_PATH")"
  if (( (8#$cert_mode & 8#077) != 0 )); then
    echo "Cloudflare origin certificate permissions must be 0600 or stricter" >&2
    exit 2
  fi
fi

if [[ ! -f "$CREDENTIALS_PATH" || -L "$CREDENTIALS_PATH" ]]; then
  if [[ "$VERIFY_ONLY" -eq 1 ]]; then
    echo "private tunnel credentials are missing or unsafe" >&2
    exit 2
  fi
  create_output="$(
    cloudflared tunnel --origincert "$CERT_PATH" create \
      --credentials-file "$CREDENTIALS_PATH" --output json "$TUNNEL_NAME"
  )"
  HOSPES_DEMO_TUNNEL_ID="$(
    CREATE_OUTPUT="$create_output" python3 -c \
      'import json, os; print(json.loads(os.environ["CREATE_OUTPUT"])["id"])'
  )"
  unset create_output
else
  HOSPES_DEMO_TUNNEL_ID="$(
    CREDENTIALS_PATH="$CREDENTIALS_PATH" python3 -c \
      'import json, os; print(json.load(open(os.environ["CREDENTIALS_PATH"], encoding="utf-8"))["TunnelID"])'
  )"
fi
chmod 600 "$CREDENTIALS_PATH"
export HOSPES_DEMO_TUNNEL_ID
export HOSPES_DEMO_TUNNEL_NAME="$TUNNEL_NAME"
export HOSPES_DEMO_CLOUDFLARE_DIR="$CLOUDFLARE_DIR"

if [[ "$VERIFY_ONLY" -eq 1 ]]; then
  python3 "$ROOT/scripts/configure_synthetic_demo_cloudflare.py" --verify-only
else
  python3 "$ROOT/scripts/configure_synthetic_demo_cloudflare.py"
fi
echo "Cloudflare Access configuration is exact; the tunnel has not been started."
