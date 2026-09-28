#!/usr/bin/env bash
# Start the editor with whatever Python can read LeRobot datasets.
#
# Preference order:
#   1. $LRDE_PYTHON                     - you said which interpreter to use
#   2. the sibling Panthera venv        - also gives us lerobot's own stats code,
#                                         so the numbers written match training
#   3. uv run                           - standalone, deps from the PEP 723 header
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

candidates=()
[[ -n "${LRDE_PYTHON:-}" ]] && candidates+=("$LRDE_PYTHON")
candidates+=("$HERE/../Panthera-HT_lerobot/.venv/bin/python")
candidates+=("$HOME/dev/Panthera-HT_lerobot/.venv/bin/python")

for py in "${candidates[@]}"; do
  if [[ -x "$py" ]] && "$py" -c "import pandas, pyarrow" 2>/dev/null; then
    exec "$py" "$HERE/server.py" "$@"
  fi
done

if command -v uv >/dev/null 2>&1; then
  echo "no venv with pandas found - falling back to 'uv run' (no lerobot: stats use numpy quantiles)" >&2
  exec uv run --no-project --script "$HERE/server.py" "$@"
fi

echo "error: need a Python with pandas+pyarrow. Set LRDE_PYTHON=/path/to/python, or install uv." >&2
exit 1
