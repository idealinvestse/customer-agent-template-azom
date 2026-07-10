#!/usr/bin/env bash
# Azom ecom-ops V2 automation entrypoint.
# Usage:
#   ./bin/ecom-automation.sh order-status --order-id 1001 --status completed
#   ./bin/ecom-automation.sh product-desc --product-id 501 --language sv
#   ./bin/ecom-automation.sh support --message "Var är order 1001?"
#   ./bin/ecom-automation.sh ssh --command "uptime"
#   ./bin/ecom-automation.sh critical "summary text"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data}"
export AZOM_USE_MOCK="${AZOM_USE_MOCK:-1}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

if [[ $# -lt 1 ]]; then
  "$PY" -m ecom_ops --help
  exit 2
fi

cmd="$1"
shift || true

if [[ "$cmd" == "critical" ]]; then
  summary="${*:-unspecified critical event}"
  "$PY" - <<PY
from ecom_ops.escalation import EscalationService
t = EscalationService().escalate_critical(${summary@Q} if False else """${summary//\"/\\\"}""")
print(f"Escalated to {t.assignee}: {t.id}")
PY
  exit 0
fi

# Map legacy names
case "$cmd" in
  order-status|order_status|order_status_update)
    exec "$PY" -m ecom_ops order-status "$@"
    ;;
  product-desc|product_desc|product_desc_gen)
    exec "$PY" -m ecom_ops product-desc "$@"
    ;;
  support|support_handler)
    exec "$PY" -m ecom_ops support "$@"
    ;;
  ssh|ssh-ops)
    exec "$PY" -m ecom_ops ssh "$@"
    ;;
  ssh-health|health)
    exec "$PY" -m ecom_ops ssh-health "$@"
    ;;
  mail|email)
    exec "$PY" -m ecom_ops mail "$@"
    ;;
  *)
    exec "$PY" -m ecom_ops "$cmd" "$@"
    ;;
esac
