#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${MIM_ENV_FILE:-$ROOT_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    line="${raw_line%$'\r'}"
    if [[ -z "$line" || "$line" == \#* || "$line" != *=* ]]; then
      continue
    fi
    key="${line%%=*}"
    value="${line#*=}"
    if [[ -n "$key" ]]; then
      export "$key=$value"
    fi
  done < "$ENV_FILE"
fi

if [[ -z "${CLOUDFLARE_API_TOKEN:-}" && -n "${CLOUDFLARE_CUSTOM_KEY:-}" ]]; then
  export CLOUDFLARE_API_TOKEN="$CLOUDFLARE_CUSTOM_KEY"
fi

if [[ -z "${CLOUDFLARE_ZONE_API_TOKEN:-}" ]]; then
  if [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
    export CLOUDFLARE_ZONE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}"
  elif [[ -n "${CLOUDFLARE_GLOBAL_API_TOKEN:-}" ]]; then
    export CLOUDFLARE_ZONE_API_TOKEN="$CLOUDFLARE_GLOBAL_API_TOKEN"
  fi
fi

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "[mim-cloudflared] missing required env var: $name" >&2
    exit 2
  fi
}

require_var CLOUDFLARE_API_TOKEN
require_var CLOUDFLARE_ACCOUNT_ID

TUNNEL_NAME="${MIM_CLOUDFLARED_TUNNEL_NAME:-mim-travel-shell}"
TUNNEL_ID="${MIM_CLOUDFLARED_TUNNEL_ID:-}"
HOSTNAME="${MIM_REMOTE_SHELL_HOSTNAME:-}"
ZONE_NAME="${MIM_REMOTE_SHELL_ZONE:-}"
CREDENTIALS_DIR="${MIM_CLOUDFLARED_CREDENTIALS_DIR:-$HOME/.cloudflared}"
CREDENTIALS_PATH="${MIM_CLOUDFLARED_CREDENTIALS_PATH:-$CREDENTIALS_DIR/${TUNNEL_NAME}.json}"
CONFIG_PATH="${MIM_CLOUDFLARED_CONFIG:-$ROOT_DIR/deploy/cloudflare/mim-shell-tunnel.yml}"
UPSTREAM_URL="${MIM_REMOTE_SHELL_UPSTREAM_URL:-http://127.0.0.1:18001}"
UPSTREAM_HOST_HEADER="${MIM_REMOTE_SHELL_UPSTREAM_HOST_HEADER:-127.0.0.1:18001}"

api_get() {
  local path="$1"
  curl --max-time 30 -fsS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" "$path"
}

api_post() {
  local path="$1"
  local payload="$2"
  curl --max-time 30 -fsS -X POST \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H 'Content-Type: application/json' \
    "$path" \
    --data "$payload"
}

api_put() {
  local path="$1"
  local payload="$2"
  curl --max-time 30 -fsS -X PUT \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H 'Content-Type: application/json' \
    "$path" \
    --data "$payload"
}

zone_api_get() {
  local path="$1"
  curl --max-time 30 -fsS -H "Authorization: Bearer $CLOUDFLARE_ZONE_API_TOKEN" "$path"
}

zone_api_post() {
  local path="$1"
  local payload="$2"
  curl --max-time 30 -fsS -X POST \
    -H "Authorization: Bearer $CLOUDFLARE_ZONE_API_TOKEN" \
    -H 'Content-Type: application/json' \
    "$path" \
    --data "$payload"
}

zone_api_put() {
  local path="$1"
  local payload="$2"
  curl --max-time 30 -fsS -X PUT \
    -H "Authorization: Bearer $CLOUDFLARE_ZONE_API_TOKEN" \
    -H 'Content-Type: application/json' \
    "$path" \
    --data "$payload"
}

VISIBLE_ZONES_JSON="$(api_get "https://api.cloudflare.com/client/v4/zones?page=1&per_page=100")"
VISIBLE_ZONE_COUNT="$(python3 - <<'PY' "$VISIBLE_ZONES_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
print(len(payload.get("result") or []))
PY
)"
VISIBLE_ZONE_NAMES="$(python3 - <<'PY' "$VISIBLE_ZONES_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
names = []
for item in payload.get("result") or []:
  name = str(item.get("name") or "").strip()
  if name:
    names.append(name)
print(",".join(names))
PY
)"

if [[ -n "$HOSTNAME" && -z "$ZONE_NAME" ]]; then
  ZONE_NAME="$(python3 - <<'PY' "$HOSTNAME" "$VISIBLE_ZONES_JSON"
import json
import sys

hostname = str(sys.argv[1] or "").strip().lower().rstrip('.')
payload = json.loads(sys.argv[2])
best = ""
for item in payload.get("result") or []:
  zone_name = str(item.get("name") or "").strip().lower().rstrip('.')
  if not zone_name:
    continue
  if hostname == zone_name or hostname.endswith("." + zone_name):
    if len(zone_name) > len(best):
      best = zone_name
print(best)
PY
)"
fi

TUNNELS_JSON="$(api_get "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/tunnels?page=1&per_page=100")"

if [[ -z "$TUNNEL_ID" ]]; then
  TUNNEL_ID="$(python3 - <<'PY' "$TUNNELS_JSON" "$TUNNEL_NAME"
import json
import sys

payload = json.loads(sys.argv[1])
name = sys.argv[2]
for item in payload.get("result", []):
    if str(item.get("name") or "").strip() == name:
        print(str(item.get("id") or "").strip())
        break
PY
)"
fi

if [[ -z "$TUNNEL_ID" ]]; then
  TUNNEL_SECRET="$(python3 - <<'PY'
import base64
import secrets

print(base64.b64encode(secrets.token_bytes(32)).decode())
PY
)"
  CREATED_JSON="$(api_post "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/tunnels" "{\"name\":\"${TUNNEL_NAME}\",\"tunnel_secret\":\"${TUNNEL_SECRET}\",\"config_src\":\"local\"}")"
  TUNNEL_ID="$(python3 - <<'PY' "$CREATED_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
print(str((payload.get("result") or {}).get("id") or "").strip())
PY
)"
fi

if [[ -z "$TUNNEL_ID" ]]; then
  echo "[mim-cloudflared] could not create or resolve tunnel ID for ${TUNNEL_NAME}" >&2
  exit 2
fi

TUNNEL_JSON="$(api_get "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/tunnels/${TUNNEL_ID}")"

mkdir -p "$CREDENTIALS_DIR"
python3 - <<'PY' "$TUNNEL_JSON" "$TUNNEL_NAME" "$CREDENTIALS_PATH" "$CLOUDFLARE_API_TOKEN" "$CLOUDFLARE_ACCOUNT_ID" "$TUNNEL_ID"
import base64
import json
import sys
import urllib.request
from pathlib import Path

payload = json.loads(sys.argv[1])
result = payload.get("result") or {}
credentials = dict(result.get("credentials_file") or {})
if not credentials:
  token_request = urllib.request.Request(
    f"https://api.cloudflare.com/client/v4/accounts/{sys.argv[5]}/cfd_tunnel/{sys.argv[6]}/token",
    headers={"Authorization": f"Bearer {sys.argv[4]}"},
    method="GET",
  )
  with urllib.request.urlopen(token_request, timeout=30) as response:
    token_payload = json.loads(response.read().decode("utf-8"))
  encoded_token = str((token_payload.get("result") or "")).strip()
  if not encoded_token:
    raise SystemExit("missing token result in tunnel token response")
  decoded = json.loads(base64.b64decode(encoded_token).decode("utf-8"))
  credentials = {
    "AccountTag": str(decoded.get("a") or "").strip(),
    "TunnelID": str(decoded.get("t") or "").strip(),
    "TunnelName": sys.argv[2],
    "TunnelSecret": str(decoded.get("s") or "").strip(),
  }
if not credentials.get("AccountTag") or not credentials.get("TunnelID") or not credentials.get("TunnelSecret"):
  raise SystemExit("tunnel credentials are incomplete")
path = Path(sys.argv[3])
path.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
PY

mkdir -p "$(dirname "$CONFIG_PATH")"
if [[ -n "$HOSTNAME" ]]; then
  cat > "$CONFIG_PATH" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDENTIALS_PATH

ingress:
  - hostname: $HOSTNAME
    service: $UPSTREAM_URL
    originRequest:
      httpHostHeader: $UPSTREAM_HOST_HEADER
      noTLSVerify: true
  - service: http_status:404
EOF
else
  cat > "$CONFIG_PATH" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDENTIALS_PATH

ingress:
  - service: $UPSTREAM_URL
    originRequest:
      httpHostHeader: $UPSTREAM_HOST_HEADER
      noTLSVerify: true
EOF
fi

if [[ -z "$HOSTNAME" ]]; then
  cat <<EOF
[mim-cloudflared] named tunnel is ready without custom DNS.
[mim-cloudflared] tunnel_name=$TUNNEL_NAME
[mim-cloudflared] tunnel_id=$TUNNEL_ID
[mim-cloudflared] credentials_file=$CREDENTIALS_PATH
[mim-cloudflared] config_file=$CONFIG_PATH
[mim-cloudflared] worker_origin=https://${TUNNEL_ID}.cfargotunnel.com
[mim-cloudflared] visible_zone_count=$VISIBLE_ZONE_COUNT
[mim-cloudflared] visible_zone_names=${VISIBLE_ZONE_NAMES:-none}
[mim-cloudflared] token_source=${CLOUDFLARE_CUSTOM_KEY:+CLOUDFLARE_CUSTOM_KEY}${CLOUDFLARE_CUSTOM_KEY:+ -> }CLOUDFLARE_API_TOKEN
[mim-cloudflared] next_step=optional set MIM_REMOTE_SHELL_HOSTNAME (and MIM_REMOTE_SHELL_ZONE) for a custom public hostname
EOF
  exit 0
fi

DNS_STATUS="hostname_configured_dns_unverified"
RESOLVED_ZONE_NAME="$ZONE_NAME"

if [[ -n "$ZONE_NAME" ]]; then
  ZONE_JSON="$(zone_api_get "https://api.cloudflare.com/client/v4/zones?name=${ZONE_NAME}")"
  ZONE_ID="$(python3 - <<'PY' "$ZONE_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
items = payload.get("result") or []
print(str(items[0].get("id") or "").strip() if items else "")
PY
)"

  if [[ -z "$ZONE_ID" ]]; then
    echo "[mim-cloudflared] zone ${ZONE_NAME} was not found or is not visible to this API token" >&2
    echo "[mim-cloudflared] visible_zone_count=${VISIBLE_ZONE_COUNT}" >&2
    echo "[mim-cloudflared] visible_zone_names=${VISIBLE_ZONE_NAMES:-none}" >&2
    exit 2
  fi

  RECORD_NAME="${HOSTNAME%.${ZONE_NAME}}"
  RECORD_NAME="${RECORD_NAME%.}"
  if [[ "$HOSTNAME" == "$ZONE_NAME" ]]; then
    RECORD_NAME="@"
  fi
  RECORD_CONTENT="${TUNNEL_ID}.cfargotunnel.com"
  RECORDS_JSON="$(zone_api_get "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records?type=CNAME&name=${HOSTNAME}")"
  RECORD_ID="$(python3 - <<'PY' "$RECORDS_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
items = payload.get("result") or []
print(str(items[0].get("id") or "").strip() if items else "")
PY
)"
  DNS_PAYLOAD="{\"type\":\"CNAME\",\"name\":\"${HOSTNAME}\",\"content\":\"${RECORD_CONTENT}\",\"proxied\":true,\"ttl\":1}"
  if [[ -n "$RECORD_ID" ]]; then
    zone_api_put "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records/${RECORD_ID}" "$DNS_PAYLOAD" >/dev/null
  else
    zone_api_post "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records" "$DNS_PAYLOAD" >/dev/null
  fi
  DNS_STATUS="hostname_dns_ready"
else
  DNS_STATUS="hostname_configured_zone_missing"
fi

cat <<EOF
[mim-cloudflared] tunnel_name=$TUNNEL_NAME
[mim-cloudflared] tunnel_id=$TUNNEL_ID
[mim-cloudflared] credentials_file=$CREDENTIALS_PATH
[mim-cloudflared] config_file=$CONFIG_PATH
[mim-cloudflared] hostname=$HOSTNAME
[mim-cloudflared] zone_name=${RESOLVED_ZONE_NAME:-unset}
[mim-cloudflared] dns_status=$DNS_STATUS
[mim-cloudflared] visible_zone_count=$VISIBLE_ZONE_COUNT
[mim-cloudflared] visible_zone_names=${VISIBLE_ZONE_NAMES:-none}
[mim-cloudflared] stable_origin=https://$HOSTNAME
[mim-cloudflared] worker_deploy_hint=./scripts/deploy_mim_cloudflare_worker.sh
EOF