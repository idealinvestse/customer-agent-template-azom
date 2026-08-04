#!/usr/bin/env bash
# Mock soft-soak: null-send shadow poll → list → shadow-report → kpis → classify-eval → brief.
# No live secrets. Does NOT mark A1 live soak complete. FU9 remains unwired.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data-soak}"
export AZOM_USE_MOCK="${AZOM_USE_MOCK:-1}"
export AZOM_NULL_SEND="${AZOM_NULL_SEND:-1}"

mkdir -p "$AZOM_DATA_DIR"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

echo "=== mock soak (AZOM_DATA_DIR=$AZOM_DATA_DIR null_send=$AZOM_NULL_SEND) ==="
"$PY" -m ecom_ops version
"$PY" -m ecom_ops --null-send status
"$PY" -m ecom_ops --mock --null-send cases poll || true
"$PY" -m ecom_ops --mock cases list --status open,escalated --limit 10 || true
"$PY" -m ecom_ops --actor oscar --null-send cases shadow-report --days 7 || true
"$PY" -m ecom_ops kpis --days 7
"$PY" -m ecom_ops classify-eval
if [[ -x "$ROOT/bin/daily-brief-azom.sh" ]]; then
  bash "$ROOT/bin/daily-brief-azom.sh" || true
fi
echo "=== mock soak complete (shadow trail only — LIVE soak remains human-owned; see docs/PILOT_OPS.md) ==="
