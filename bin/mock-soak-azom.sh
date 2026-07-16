#!/usr/bin/env bash
# Mock soft-soak: poll → list → kpis → classify-eval → brief (no live secrets).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data-soak}"
export AZOM_USE_MOCK="${AZOM_USE_MOCK:-1}"

mkdir -p "$AZOM_DATA_DIR"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

echo "=== mock soak (AZOM_DATA_DIR=$AZOM_DATA_DIR) ==="
"$PY" -m ecom_ops version
"$PY" -m ecom_ops status
"$PY" -m ecom_ops --mock cases poll || true
"$PY" -m ecom_ops --mock cases list --status open,escalated --limit 10 || true
"$PY" -m ecom_ops kpis --days 7
"$PY" -m ecom_ops classify-eval
if [[ -x "$ROOT/bin/daily-brief-azom.sh" ]]; then
  bash "$ROOT/bin/daily-brief-azom.sh" || true
fi
echo "=== mock soak complete (see docs/solutions/2026-07-16-live-soak-checklist.md for LIVE) ==="
