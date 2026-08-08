# Google Ads + GA4 — Azom marketing ops

**Purpose:** Living guide for Google Analytics 4, Google Ads, and Merchant feed capacity in AzomOps-Agent.  
**Audience:** Oscar (secrets/live), Jonatan (read + suggest approve), coding agents.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`SOUL.md`](../SOUL.md), [`CLI_REFERENCE.md`](CLI_REFERENCE.md).

## Truth hierarchy (hard)

1. **Woo order revenue** = commerce truth.  
2. **Ads-reported ROAS** = labeled marketing view (Ads attribution).  
3. **GA revenue / purchases** = labeled GA view (may be modeled / consent-affected).  

Never invent metrics. Always cite source + date range. Do not collapse Ads+GA+Woo into one “true ROAS” number.

## Surfaces

| Surface | What |
|---------|------|
| CLI | `python -m ecom_ops marketing …` (digest, health, waste, pacing, consistency, suggests, mutate, mp, mer) |
| Dashboard | `/marketing` (Jonatan read); Oscar secrets + OAuth + probes |
| Bot | Read-only marketing snapshot; mutate only via explicit approve |
| Probes | `ga4`, `google_ads`, `merchant` on Oscar secrets page |

## Auth and allowlists

- OAuth tokens: `$AZOM_DATA_DIR/oauth/google_marketing.json` (auto-refresh via `ensure_fresh_access_token`)
- Env: `GOOGLE_OAUTH_CLIENT_ID/SECRET`, `GOOGLE_ADS_DEVELOPER_TOKEN`, optional `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
- Merchant / MP (live writes): `GOOGLE_MERCHANT_ID`, `GA4_MEASUREMENT_ID`, `GA4_MEASUREMENT_API_SECRET`
- Scopes: Analytics readonly + AdWords + Content API (`content`); edit scope when Oscar starts OAuth with edit
- Fail-closed in live (`AZOM_USE_MOCK=0`): empty `AZOM_GA4_PROPERTY_IDS` or `AZOM_GADS_CUSTOMER_IDS` → deny
- Mock: `AZOM_USE_MOCK=1` uses in-memory fixtures (no network)
- Live transports use Google **REST** (`requests`) — no `google-ads` / Analytics Data SDK package deps

## RBAC

| Actor | Permissions | Typical marketing actions |
|-------|-------------|---------------------------|
| **Jonatan** (`viewer`) | `MARKETING_READ`, `MARKETING_SUGGEST` | Digest/health; suggest build/deny; **approve negatives** |
| **Oscar** (`full_admin`) | + `MARKETING_MUTATE` | Pause/budget/etc. approve; OAuth start; probes |
| **agent** (`operator`) | `MARKETING_READ` only | Read digest/snapshot — not suggest build |

Auth source: `config/rbac.yaml` + `skills/ecom_ops/rbac.py`.

## Mutate / HITL

- Default: `ads_mutate_enabled: false` (and peers) in [`config/marketing.yaml`](../config/marketing.yaml)
- Kill-switches: `AZOM_ADS_MUTATE_KILL`, `AZOM_GA_MUTATE_KILL`, `AZOM_MP_KILL` → always deny when set
- `AZOM_GA_MUTATE_KILL` / `ga_mutate_allowed` is a **reserved rail** until a GA-admin mutate path exists (no invent mutate)
- `config/integrations.yaml` `google_*` flags are **non-gating reserved** — live gating uses marketing.yaml + env allowlists
- Flow: suggest → human approve → execute (never free-text / silent auto-pause)
- Recommendation auto-apply subscriptions: **out of scope**

## Soft mock path

```bash
bash bin/mock-marketing-azom.sh
# or:
python -m ecom_ops --mock marketing digest --days 7
python -m ecom_ops --mock marketing consistency --days 7
python -m ecom_ops --mock marketing suggests list
```

## Phases shipped in code

| Phase | Capability | Live API |
|-------|------------|----------|
| P0 | Mock transports, probes, OAuth (Oscar-only start), status | OAuth exchange + refresh live |
| P1 | Digest, conversion/event health, waste report, pacing alerts | Mock fixtures; Live Ads/GA4 REST wired (needs Oscar creds) |
| P2 | Woo↔GA↔Ads consistency (date-window Woo), landing/shopping read, MER | Woo live when not mock; GA/Ads REST when creds present |
| P3 | Suggest queue (approve/deny, deduped rebuild) | N/A |
| P4 | HITL Ads mutate + kill-switch + result gating | Live mutate REST for pause / negative / budget RN |
| P5 | HITL Measurement Protocol + Merchant write | Live MP + Content API when secrets / merchant id set |

## Do / Do not

**Do:** Use mock locally; label metrics; escalate auth failures to Oscar; approve mutates explicitly.  
**Do not:** Auto-pause campaigns; silent MP `purchase` events; commit OAuth tokens; treat GA revenue as Woo truth.

## Related

- Skill card: [`skills/ecom-ops/SKILL.md`](../skills/ecom-ops/SKILL.md)  
- Config: [`config/marketing.yaml`](../config/marketing.yaml)  
