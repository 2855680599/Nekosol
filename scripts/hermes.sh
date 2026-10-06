#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
state="${NYAIRO_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/nyairo}"
if [[ -f "$state/install.json" && "$(uname -s)" == Linux ]]; then
  exec 9>>"$state/update.lock"
  if ! flock --shared --nonblock 9; then
    echo 'nyairo 正在更新，请等更新完成后再启动。' >&2
    exit 1
  fi
  if [[ -e "$state/pending.json" ]]; then
    echo '上次更新中断，请先运行 ~/.local/bin/nyairo update --recover' >&2
    exit 1
  fi
fi
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
exec "$root/vendor/hermes/.venv/bin/hermes" "$@"
