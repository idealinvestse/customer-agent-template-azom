#!/usr/bin/env bash
# Spin up Azom customer-agent template (V1 pilot).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CUSTOMER="azom"
DOMAINS="no,se,dk"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --customer) CUSTOMER="$2"; shift 2 ;;
    --domains) DOMAINS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--customer azom] [--domains no,se,dk]"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Spinup customer=${CUSTOMER} domains=${DOMAINS}"
mkdir -p "${ROOT}/.azom-data" "${ROOT}/logs"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python not found" >&2
  exit 1
fi

if [[ -f "${ROOT}/requirements.txt" ]]; then
  echo "==> Ensuring Python deps (user site ok)"
  "$PY" -m pip install -q -r "${ROOT}/requirements.txt" || true
fi

export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${ROOT}/config"
export AZOM_DATA_DIR="${ROOT}/.azom-data"
export AZOM_USE_MOCK="${AZOM_USE_MOCK:-1}"

echo "==> Validating config + V1 modules"
"$PY" - <<'PY'
from ecom_ops.config import load_app_config
from ecom_ops import __version__
cfg = load_app_config()
print(f"ecom_ops {__version__} customer={cfg.customer.customer} domains={cfg.customer.domains}")
print(f"escalation critical -> {cfg.rbac.escalation_critical}")
PY

echo "==> Smoke: order-status + support + ssh (mock)"
"$PY" -m ecom_ops --mock order-status --order-id 1001 --status completed >/dev/null
"$PY" -m ecom_ops support --message "Order 1001 status?" >/dev/null
"$PY" -m ecom_ops --mock ssh --command uptime >/dev/null

echo "==> Spinup complete (idempotent). Data dir: ${ROOT}/.azom-data"
