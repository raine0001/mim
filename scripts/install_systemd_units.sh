#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "Installing MIM systemd units..."
sudo cp "$ROOT_DIR/deploy/systemd/mim-prod.service" /etc/systemd/system/mim-prod.service
sudo cp "$ROOT_DIR/deploy/systemd/mim-test.service" /etc/systemd/system/mim-test.service
sudo cp "$ROOT_DIR/deploy/systemd/mim-backup-prod.service" /etc/systemd/system/mim-backup-prod.service
sudo cp "$ROOT_DIR/deploy/systemd/mim-backup-prod.timer" /etc/systemd/system/mim-backup-prod.timer
sudo cp "$ROOT_DIR/deploy/systemd/mim-healthcheck.service" /etc/systemd/system/mim-healthcheck.service
sudo cp "$ROOT_DIR/deploy/systemd/mim-healthcheck.timer" /etc/systemd/system/mim-healthcheck.timer

sudo systemctl daemon-reload
sudo systemctl enable mim-prod.service mim-test.service mim-backup-prod.timer mim-healthcheck.timer

echo "Units installed and enabled."
echo "Start now with:"
echo "  sudo systemctl start mim-prod"
echo "  sudo systemctl start mim-test"
echo "  sudo systemctl start mim-backup-prod.timer"
echo "  sudo systemctl start mim-healthcheck.timer"
