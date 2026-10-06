#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
if [[ "${1:-}" == "update" ]]; then
  shift
  exec bash "$root/scripts/update.sh" "$@"
fi
exec bash "$root/scripts/hermes.sh" "$@"
