#!/usr/bin/env bash
# Test spinup idempotency + V1 CLI smoke.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${ROOT}/config"
export AZOM_DATA_DIR="${ROOT}/.azom-data-test"
export AZOM_USE_MOCK=1

rm -rf "${AZOM_DATA_DIR}"
mkdir -p "${AZOM_DATA_DIR}"

python -m ecom_ops --mock order-status --order-id 1001 --status completed | grep -q '"ok": true'
python -m ecom_ops --mock product-desc --product-id 501 --language sv | grep -q '"ok": true'
python -m ecom_ops support --message "Var är order 1001?" | grep -q '"ok": true'
python -m ecom_ops --mock ssh --command uptime | grep -q '"ok": true'
python -m ecom_ops --mock ssh --command "rm -rf /" | grep -q '"escalated": true'

# Idempotent second run
python -m ecom_ops --mock order-status --order-id 1001 --status completed >/dev/null

echo "Tests passed"
