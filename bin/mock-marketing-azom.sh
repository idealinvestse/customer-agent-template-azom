#!/usr/bin/env bash
# Mock marketing soft path: digest → health → consistency → suggests (no live secrets).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data-marketing}"
export AZOM_USE_MOCK="${AZOM_USE_MOCK:-1}"

mkdir -p "$AZOM_DATA_DIR"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

echo "=== mock marketing (AZOM_DATA_DIR=$AZOM_DATA_DIR) ==="
"$PY" -m ecom_ops --mock status
"$PY" -m ecom_ops --mock marketing digest --days 7
"$PY" -m ecom_ops --mock marketing health
"$PY" -m ecom_ops --mock marketing waste --days 7
"$PY" -m ecom_ops --mock marketing pacing
"$PY" -m ecom_ops --mock marketing consistency --days 7
"$PY" -m ecom_ops --mock marketing mer --days 7
"$PY" -m ecom_ops --mock --actor jonatan marketing suggests build
"$PY" -m ecom_ops --mock marketing suggests list
"$PY" -m ecom_ops --mock marketing snapshot
echo "=== mock marketing complete (see docs/MARKETING_GOOGLE.md) ==="
