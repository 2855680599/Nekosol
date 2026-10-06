#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
python="$root/vendor/hermes/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  python=$(command -v python3 || true)
fi
if [[ -z "$python" ]]; then
  echo "需要 Python 3。请先按安装教程安装 uv 和 nyairo。" >&2
  exit 1
fi
exec "$python" "$root/scripts/update.py" "$@"
