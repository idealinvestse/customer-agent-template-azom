# customer-agent-template-azom

Template för dedikerad **AzomOps-Agent** + grund för Agent-as-a-Service.

## V1 Pilot (implementerad)

| Capability | Beskrivning | Eskalering |
|------------|-------------|------------|
| **order-status** | Uppdatera WooCommerce orderstatus | Access-fel → Oscar |
| **product-desc** | Generera (och valfritt publicera) produktbeskrivning SE/NO/DK | Access-fel → Oscar |
| **support** | Klassificera ärende + draft-svar | Abuse/legal/critical → Oscar |
| **SSH** | Allowlistad health/ops | Osäker/kodredigering → Oscar |

**RBAC:** Jonatan = viewer (read-only), Oscar = full_admin, agent = operator.

## Quick start

```bash
python -m pip install -r requirements.txt
python -m pip install -e .

# Mock-läge (ingen extern trafik)
set AZOM_USE_MOCK=1   # Windows PowerShell: $env:AZOM_USE_MOCK=1

python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock product-desc --product-id 501 --language sv
python -m ecom_ops support --message "Var är order 1001?"
python -m ecom_ops --mock ssh --command uptime
python -m ecom_ops --mock ssh --command "rm -rf /"   # eskalerar till Oscar
```

Spinup:

```bash
./bin/spinup.sh --customer azom --domains "no,se,dk"
./bin/ecom-automation.sh order-status --order-id 1001 --status completed
```

## Tests

```bash
pytest
bash tests/test_spinup.sh
```

## Config

- `config/sites.yaml` – kund + domäner + LLM-budget
- `config/rbac.yaml` – roller + escalation (Oscar)
- `config/limits.yaml` – OpenRouter cap, Jonatan read-only
- `config/integrations.yaml` – integrationsflaggor
- `.env.example` – secrets (kopiera till `.env`, committas ej)

## Roadmap

1. **V1** – Pilot: order-status, product-desc, support, SSH ✅
2. **V2** – Dashboard + onboarding
3. **V3** – SaaS-skalning

Se `docs/ANALYSIS_AND_DEVELOPMENT_PLAN.md` och `docs/V1_IMPLEMENTATION.md`.
