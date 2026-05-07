#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Compatibility wrapper: launch the MIM desktop text-chat UI.
exec "$ROOT_DIR/goMIM" "$@"
