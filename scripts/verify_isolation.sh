#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "| Resource | Test | Prod |"
echo "|---|---|---|"
echo "| DB host path | $ROOT_DIR/runtime/test/data/postgres | $ROOT_DIR/runtime/prod/data/postgres |"
echo "| App logs | $ROOT_DIR/runtime/test/logs | $ROOT_DIR/runtime/prod/logs |"
echo "| Reports | $ROOT_DIR/runtime/test/reports | $ROOT_DIR/runtime/prod/reports |"
echo "| Artifacts | $ROOT_DIR/runtime/test/artifacts | $ROOT_DIR/runtime/prod/artifacts |"
echo "| Uploads | $ROOT_DIR/runtime/test/uploads | $ROOT_DIR/runtime/prod/uploads |"
echo "| Temp/work | $ROOT_DIR/runtime/test/tmp + $ROOT_DIR/runtime/test/work | $ROOT_DIR/runtime/prod/tmp + $ROOT_DIR/runtime/prod/work |"

test_cfg="$(sudo docker compose -f "$ROOT_DIR/docker/test/compose.yaml" --env-file "$ROOT_DIR/env/.env.test" config)"
prod_cfg="$(sudo docker compose -f "$ROOT_DIR/docker/prod/compose.yaml" --env-file "$ROOT_DIR/env/.env.prod" config)"

echo
echo "Isolation checks:"

if grep -q "runtime/prod" <<<"$test_cfg"; then
  echo "FAIL: test stack references prod runtime paths"
  exit 1
fi
if grep -q "runtime/test" <<<"$prod_cfg"; then
  echo "FAIL: prod stack references test runtime paths"
  exit 1
fi

echo "PASS: compose definitions keep prod/test runtime paths isolated"
