#!/usr/bin/env bash
set -euo pipefail
app_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$app_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$app_dir/../work/venv/bin/python"
fi
if [[ ! -x "$python_bin" ]]; then
  echo 'The local Python environment is missing. See README.md for setup instructions.' >&2
  exit 1
fi
exec "$python_bin" "$app_dir/MediaProber.py" "$@"
