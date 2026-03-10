#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/runtime/prod/backups"
RESTORE_ROOT="$ROOT_DIR/runtime/restore"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK_DIR="$RESTORE_ROOT/$STAMP"
LOG_FILE="$ROOT_DIR/runtime/prod/restore-tests.log"

mkdir -p "$WORK_DIR"

LATEST_SQL="$(ls -1t "$BACKUP_DIR"/mim_prod_*.sql 2>/dev/null | head -n1 || true)"
LATEST_ENV="$(ls -1t "$BACKUP_DIR"/mim_prod_env_*.env 2>/dev/null | head -n1 || true)"
LATEST_DATA="$(ls -1t "$BACKUP_DIR"/mim_prod_data_*.tgz 2>/dev/null | head -n1 || true)"

if [[ -z "$LATEST_SQL" || -z "$LATEST_ENV" || -z "$LATEST_DATA" ]]; then
  echo "Missing one or more backup artifacts (sql/env/data)."
  exit 1
fi

cp "$LATEST_ENV" "$WORK_DIR/.env.restore"
cp "$LATEST_SQL" "$WORK_DIR/backup.sql"
mkdir -p "$WORK_DIR/restored_data"
tar -xzf "$LATEST_DATA" -C "$WORK_DIR/restored_data"

POSTGRES_DB="$(grep -E '^POSTGRES_DB=' "$WORK_DIR/.env.restore" | tail -n1 | cut -d= -f2-)"
POSTGRES_USER="$(grep -E '^POSTGRES_USER=' "$WORK_DIR/.env.restore" | tail -n1 | cut -d= -f2-)"
POSTGRES_PASSWORD="$(grep -E '^POSTGRES_PASSWORD=' "$WORK_DIR/.env.restore" | tail -n1 | cut -d= -f2-)"

cat > "$WORK_DIR/compose.yaml" <<EOF
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - ./pgdata:/var/lib/postgresql/data

  app:
    build:
      context: ${ROOT_DIR}
      dockerfile: Dockerfile
    depends_on:
      - db
    environment:
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
      APP_NAME: MIM Core Restore Test
      APP_VERSION: restore-test
      ENVIRONMENT: restore-test
      RELEASE_TAG: restore-test
      CONFIG_PROFILE: restore-test
      BUILD_GIT_SHA: restore-test
      BUILD_TIMESTAMP: ${STAMP}
      ALLOW_OPENAI: "false"
      ALLOW_WEB_ACCESS: "false"
      ALLOW_LOCAL_DEVICES: "true"
    ports:
      - "127.0.0.1:8010:8000"
EOF

cleanup() {
  sudo docker compose -f "$WORK_DIR/compose.yaml" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

sudo docker compose -f "$WORK_DIR/compose.yaml" up -d --build db
sleep 5
cat "$WORK_DIR/backup.sql" | sudo docker compose -f "$WORK_DIR/compose.yaml" exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null
sudo docker compose -f "$WORK_DIR/compose.yaml" up -d --build app
sleep 5

curl -fsS http://127.0.0.1:8010/health >/dev/null
curl -fsS http://127.0.0.1:8010/status >/dev/null
curl -fsS http://127.0.0.1:8010/manifest >/dev/null

TABLE_COUNT="$(sudo docker compose -f "$WORK_DIR/compose.yaml" exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" | tr -d ' ')"

CORE_TABLES_OK="$(sudo docker compose -f "$WORK_DIR/compose.yaml" exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('objectives','tasks','task_results','task_reviews','execution_journal');" | tr -d ' ')"

if [[ "$TABLE_COUNT" -eq 0 || "$CORE_TABLES_OK" -lt 5 ]]; then
  RESULT_LINE="$(date -u +%Y-%m-%dT%H:%M:%SZ) restore_test=fail tables=${TABLE_COUNT} core_tables=${CORE_TABLES_OK} sql=$(basename "$LATEST_SQL") data=$(basename "$LATEST_DATA")"
  echo "$RESULT_LINE" | tee -a "$LOG_FILE"
  exit 1
fi

RESULT_LINE="$(date -u +%Y-%m-%dT%H:%M:%SZ) restore_test=pass tables=${TABLE_COUNT} core_tables=${CORE_TABLES_OK} sql=$(basename "$LATEST_SQL") data=$(basename "$LATEST_DATA")"
echo "$RESULT_LINE" | tee -a "$LOG_FILE"
