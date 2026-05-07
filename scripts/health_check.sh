#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

check_target() {
  local target="$1"
  local base_url
  if [[ "$target" == "prod" ]]; then
    base_url="http://127.0.0.1:8000"
  else
    base_url="http://127.0.0.1:18001"
  fi

  curl -fsS "$base_url/health" >/dev/null
  curl -fsS "$base_url/status" >/dev/null
  echo "$TS OK $target $base_url"
}

if [[ "${1:-both}" == "prod" ]]; then
  check_target prod
elif [[ "${1:-both}" == "test" ]]; then
  check_target test
else
  check_target prod
  check_target test
fi
