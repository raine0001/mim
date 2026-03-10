#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/env/.env.prod"
OUT_DIR="$ROOT_DIR/runtime/prod/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="$OUT_DIR/mim_prod_$STAMP.sql"
ENV_SNAPSHOT="$OUT_DIR/mim_prod_env_$STAMP.env"
DATA_ARCHIVE="$OUT_DIR/mim_prod_data_$STAMP.tgz"

BACKUP_RETENTION_DAYS="$(grep -E '^BACKUP_RETENTION_DAYS=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
if [[ -z "$BACKUP_RETENTION_DAYS" ]]; then
  BACKUP_RETENTION_DAYS=14
fi

POSTGRES_USER="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
POSTGRES_DB="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"

if [[ -z "$POSTGRES_USER" || -z "$POSTGRES_DB" ]]; then
  echo "Missing POSTGRES_USER or POSTGRES_DB in $ENV_FILE"
  exit 1
fi

mkdir -p "$OUT_DIR"

cp "$ENV_FILE" "$ENV_SNAPSHOT"

tar -czf "$DATA_ARCHIVE" \
  -C "$ROOT_DIR/runtime/prod" \
  reports uploads artifacts

sudo docker compose \
  -f "$ROOT_DIR/docker/prod/compose.yaml" \
  --env-file "$ENV_FILE" \
  exec -T mim_db_prod pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > "$OUT_FILE"

echo "Backup created: $OUT_FILE"
echo "Env snapshot created: $ENV_SNAPSHOT"
echo "Data archive created: $DATA_ARCHIVE"

find "$OUT_DIR" -type f -name 'mim_prod_*.sql' -mtime +"$BACKUP_RETENTION_DAYS" -delete
find "$OUT_DIR" -type f -name 'mim_prod_env_*.env' -mtime +"$BACKUP_RETENTION_DAYS" -delete
find "$OUT_DIR" -type f -name 'mim_prod_data_*.tgz' -mtime +"$BACKUP_RETENTION_DAYS" -delete
echo "Retention applied: deleted backup/env files older than $BACKUP_RETENTION_DAYS days"
