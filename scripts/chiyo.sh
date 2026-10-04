#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
exec "$root/vendor/hermes/.venv/bin/python" -m chiyo_bundle "$@"
