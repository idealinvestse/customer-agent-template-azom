#!/usr/bin/env bash
# Daily brief for azom: KPI hooks + health (V1 mock-friendly).
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

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

"$PY" - <<'PY'
import json
from pathlib import Path
from ecom_ops.config import load_app_config
from ecom_ops.telemetry import Telemetry
from ecom_ops.actions.ssh_ops import SSHOpsService

cfg = load_app_config()
tel = Telemetry()
cost = tel.sum_cost_usd()
health = SSHOpsService().health(actor="agent")
brief = {
    "customer": cfg.customer.customer,
    "domains": cfg.customer.domains,
    "kpis": (cfg.customer_meta or {}).get("kpis", []),
    "llm_cost_usd": cost,
    "budget_cap_llm": cfg.customer.budget_cap_llm,
    "openrouter_cap": cfg.limits.openrouter_cap,
    "ssh_health_ok": all(h.ok for h in health),
    "proposed_actions": [
        "Review open support escalations",
        "Confirm delayed orders still in processing",
        "Refresh missing product descriptions (DK/SE/NO)",
    ],
}
print(json.dumps(brief, ensure_ascii=False, indent=2))
print("Azom daily KPI brief generated")
PY
