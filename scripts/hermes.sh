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
# Public NYAIRO_* configuration names win over the legacy CHIYO_* spelling, which
# stays supported. Exporting the legacy twin of every NYAIRO_* variable is what
# lets the whole process tree honour a public name -- including the components
# that read os.environ directly instead of a passed mapping.
for _public in ${!NYAIRO_*}; do
  export "CHIYO_${_public#NYAIRO_}=${!_public}"
done
unset _public
exec "$root/vendor/hermes/.venv/bin/hermes" "$@"
