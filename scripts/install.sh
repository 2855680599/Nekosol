#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root/vendor/hermes"
if ! command -v uv >/dev/null; then echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2;exit 1;fi
uv sync --frozen --extra messaging --extra web
printf 'Installed. Prepare an independent profile and run Hermes setup. See INSTALL.md.\n'
