#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROD_COMPOSE=(sudo docker compose -f "$ROOT_DIR/docker/prod/compose.yaml" --env-file "$ROOT_DIR/env/.env.prod")
TEST_COMPOSE=(sudo docker compose -f "$ROOT_DIR/docker/test/compose.yaml" --env-file "$ROOT_DIR/env/.env.test")

echo "== MIM STATUS =="
echo "timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

echo "-- Containers (prod) --"
"${PROD_COMPOSE[@]}" ps || true
echo

echo "-- Containers (test) --"
"${TEST_COMPOSE[@]}" ps || true
echo

manifest_field() {
  local url="$1"
  local field="$2"
  curl -fsS "$url" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$field','unknown'))" 2>/dev/null || echo "unavailable"
}

echo "-- Manifest --"
echo "prod git_sha: $(manifest_field http://127.0.0.1:8000/manifest git_sha)"
echo "prod release_tag: $(manifest_field http://127.0.0.1:8000/manifest release_tag)"
echo "prod build_timestamp: $(manifest_field http://127.0.0.1:8000/manifest build_timestamp)"
echo "test git_sha: $(manifest_field http://127.0.0.1:8001/manifest git_sha)"
echo "test release_tag: $(manifest_field http://127.0.0.1:8001/manifest release_tag)"
echo

echo "-- Backup --"
latest_backup="$(ls -1t "$ROOT_DIR/runtime/prod/backups"/mim_prod_*.sql 2>/dev/null | head -n1 || true)"
if [[ -n "$latest_backup" ]]; then
  echo "last_backup_file: $latest_backup"
  echo "last_backup_time: $(date -u -r "$latest_backup" +%Y-%m-%dT%H:%M:%SZ)"
else
  echo "last_backup_file: none"
fi
echo

echo "-- Healthcheck --"
last_health="$(sudo journalctl -u mim-healthcheck.service -n 1 --no-pager -o short-iso 2>/dev/null | head -n1 || true)"
if [[ -n "$last_health" ]]; then
  echo "last_health_log: $last_health"
else
  echo "last_health_log: unavailable"
fi
echo

echo "-- Disk Usage --"
df -h "$ROOT_DIR" | tail -n +1
echo
sudo du -sh "$ROOT_DIR/runtime/prod" "$ROOT_DIR/runtime/test" 2>/dev/null || true
echo

echo "-- Uptime --"
uptime -p || true
