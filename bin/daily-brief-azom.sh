#!/usr/bin/env bash
# Daily brief for azom: KPI hooks + cases queue + readiness + budget (FU4/FU5).
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
from ecom_ops.actions.ssh_ops import SSHOpsService
from ecom_ops.budget import budget_status
from ecom_ops.cases.service import CaseService
from ecom_ops.config import load_app_config
from ecom_ops.ops_status import readiness_from_last_poll
from ecom_ops.telemetry import Telemetry

cfg = load_app_config()
tel = Telemetry()
budget = budget_status(telemetry=tel)
health = SSHOpsService().health(actor="agent")
ready = readiness_from_last_poll()

open_n = escalated_n = suggest_n = 0
stuck = []
try:
    svc = CaseService()
    cases = svc.list_open(limit=50)
    open_n = sum(1 for c in cases if c.status == "open")
    escalated_n = sum(1 for c in cases if c.status == "escalated")
    suggest_n = sum(1 for c in cases if getattr(c, "suggest_approve", False))
    # stuck: escalated first, then oldest open
    ranked = sorted(
        cases,
        key=lambda c: (
            0 if c.status == "escalated" else 1,
            c.created_at or "",
        ),
    )
    for c in ranked[:3]:
        stuck.append(
            {
                "id8": c.id[:8],
                "status": c.status,
                "category": c.category,
                "subject": (c.subject or "")[:60],
                "suggest_approve": bool(getattr(c, "suggest_approve", False)),
                "created_at": c.created_at,
            }
        )
except Exception as exc:
    stuck = [{"error": str(exc)[:120]}]

brief = {
    "customer": cfg.customer.customer,
    "domains": list(cfg.customer.domains),
    "kpis": (cfg.customer_meta or {}).get("kpis", []),
    "llm_cost_usd": budget["used_usd"],
    "openrouter_cap": budget["cap_usd"],
    "budget": budget,
    "budget_cap_llm": cfg.customer.budget_cap_llm,
    "ssh_health_ok": all(h.ok for h in health),
    "readiness": ready,
    "cases": {
        "open": open_n,
        "escalated": escalated_n,
        "suggest_approve": suggest_n,
        "queue_total": open_n + escalated_n,
        "stuck_top": stuck,
    },
    "proposed_actions": [
        "Review open support escalations",
        "Confirm delayed orders still in processing",
        "Refresh missing product descriptions (DK/SE/NO)",
    ],
}
if budget.get("near_cap"):
    brief["proposed_actions"].insert(
        0, f"BUDGET WARN: {budget.get('message')} — reduce LLM use or raise cap"
    )
if ready.get("stale") and not ready.get("ok"):
    brief["proposed_actions"].insert(
        0, "Cases poll stale or missing — check azom-cases-poll.timer"
    )
print(json.dumps(brief, ensure_ascii=False, indent=2))
print("Azom daily KPI brief generated")
PY
