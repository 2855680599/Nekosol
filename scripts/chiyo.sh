#!/usr/bin/env bash
# Legacy entry point. The current one is scripts/nyairo.sh (and the managed
# launcher ~/.local/bin/nyairo); this name is kept so an older guide or script
# keeps working unchanged.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
exec "$root/vendor/hermes/.venv/bin/python" -m chiyo_bundle "$@"
