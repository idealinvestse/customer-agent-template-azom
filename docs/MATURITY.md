# Maturity assessment — AzomOps-Agent

**Purpose:** Engineering judgment of product stage and per-surface maturity. This file records how mature the code and the operations evidence are. It does not change what is shipped.  
**Audience:** Coding agents and developers. Oscar and Jonatan may skim the stage summary. Operators run the pilot from [`PILOT_OPS.md`](PILOT_OPS.md).  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`DOC_STYLE.md`](DOC_STYLE.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md).

When this file and [`CURRENT_STATE.md`](CURRENT_STATE.md) disagree, **`CURRENT_STATE.md` wins**. Scores below are a review snapshot. They are not a soak sign-off and not permission to enable a gated feature.

## Glossary

| Term | Meaning |
|------|---------|
| **Code score** | How complete and guarded the repository implementation is (tests, fail-closed defaults, HITL). |
| **Ops score** | How far a real host has proven that surface. Human gates stay low until Oscar and Jonatan record evidence. |
| **HITL** | Human-in-the-loop. Customer mail send needs an explicit approve path. |
| **Fail-closed** | In live mode (`AZOM_USE_MOCK=0`), an empty Messenger or Telegram allowlist or actor map denies access. |
| **Suggest-approve** | Path B badge for allowlisted case categories. A human still sends. |
| **FU9** | Auto-send wired into cases poll. Rails exist. The sender is not wired. |
| **A1 live soak** | Human checklist on a real host in [`PILOT_OPS.md`](PILOT_OPS.md). Agents must not mark it done. |
| **Null-send** | `AZOM_NULL_SEND=1` or `--null-send`. Refuses customer mail and records a shadow decision. |
| **Parked** | Explicit non-goal in [`CURRENT_STATE.md`](CURRENT_STATE.md). Score 0 or 1 means “do not build,” not “half done.” |

## Score scale

| Score | Meaning |
|-------|---------|
| 0 | Not in the tree. Parked or absent. |
| 1 | Skeleton, or a switch that stays off with no live path. |
| 2 | Rails or mock. Safe to exercise locally. Not a production write path. |
| 3 | Working core with a narrow scope. |
| 4 | Production-shaped code: tests, HITL or fail-closed guards, deploy hooks. Not proven on the Azom host. |
| 5 | Proven in live operation with a human outcome log. |

**Good example:** Marketing live **reads** can score 4 in code while Ads mutate stays 2, because `config/marketing.yaml` keeps mutate flags false and live mutate raises `NotImplementedError`.  
**Bad example:** Raising the FU9 ops score because `explain_auto_send` returns `eligible` in a unit test. Eligibility is not a send.

## Do / Do not

**Do:**

- Treat **code 4 / ops 2** as the headline: pilot-candidate software, pre-soak operations.
- Re-check [`config/mailboxes.yaml`](../config/mailboxes.yaml), [`config/cases_ai.yaml`](../config/cases_ai.yaml), and [`config/marketing.yaml`](../config/marketing.yaml) before you quote a flag.
- Update the “Assessed on” line in this file when you re-score.

**Do not:**

- Mark A1 live soak, baseline KPI, or live classify calibration complete from this file.
- Wire FU9 auto-send, enable NO/DK mailboxes, or turn on Ads mutate because a code score is 4.
- Read the root README phrase “Agent-as-a-Service” as a shipped multi-tenant control plane. [`TEMPLATE_GUIDE.md`](TEMPLATE_GUIDE.md) is a manual single-tenant fork checklist.

## Assessed on

| Item | Value |
|------|--------|
| Date | 2026-11-21 |
| Commit | `0449ccc` (`0449cccc8710b839f1b050926710d5d7108b108f`) on `feat/v3-production-maturity` |
| Commit subject | `fix(ops): harden runtime overlays, case state, and CI contract` (2026-08-15) |
| Package | **3.0.0** (`pyproject.toml` and `ecom_ops.__version__`) |
| Remote | Even with `origin/feat/v3-production-maturity` at assessment time (0 ahead, 0 behind) |
| Method | Living docs plus config plus module layout. No production host was contacted. |

Size snapshot (non-blank Python lines, `__pycache__` excluded):

| Tree | Files | Lines |
|------|-------|-------|
| `skills/ecom_ops/` | 85 | about 16 700 |
| `infrastructure/dashboard/` (Python) | 13 | about 2 300 |
| `tests/` | 62 Python files, 61 named `test_*.py` | about 8 000 |

Largest modules: `skills/ecom_ops/cases/store.py`, `skills/ecom_ops/integrations/woocommerce.py`, `skills/ecom_ops/actions/support.py`, `skills/ecom_ops/bot/chat_agent.py`. CI (`.github/workflows/ci.yml`) runs ruff, shellcheck, pytest with coverage fail-under 70, `tests/test_spinup.sh`, and mock `smoke --live`.

## Stage

| Dimension | Level | Judgment |
|-----------|-------|----------|
| Product stage | Late pilot | One customer (Azom). One mailbox on: `support_default` (SE, `sv`). |
| Code maturity | 4 | HITL, fail-closed bots, kill switches, SQLite WAL, systemd, Docker, runbooks. |
| Operations maturity | 2 | A1 live soak has not been run. Support-time baseline is empty. Live classify calibration is open. |
| Template / more customers | 2 | `config/profile.yaml` renames the shop. It does not isolate tenants. |
| Multi-tenant SaaS | 0 | Parked in [`CURRENT_STATE.md`](CURRENT_STATE.md). |

The repository had no newer commit than `0449ccc` on 2026-11-21. That matches the ops-next rule: the next step is a human soak, not more product surface.

## Per-surface scores

| Surface | Code | Ops | What the score means |
|---------|------|-----|----------------------|
| Cases loop (poll, thread, classify, draft, approve, send) | 4 | 2 | Main product path. Statuses include `open`, `escalated`, `sending`, `replied`, `closed`. Send stays on the human approve path. |
| Suggest-approve (Path B) | 4 | 2 | On for `order_status` and `shipping` at confidence ≥ 0.8 and with an order id (`config/cases_ai.yaml`). `abuse`, `return`, and `billing` are in `never_suggest_categories`. |
| Path B2 drafts | 4 | 2 | Richer return and billing drafts. Those categories still never get the suggest-approve badge. |
| FU9 auto-send | 2 | 1 | `skills/ecom_ops/cases/auto_send.py` explains eligibility, daily cap, and `AZOM_AUTO_SEND_KILL`. `auto_send_enabled: false`. Poll does not send. |
| Shadow Live Ledger / null-send | 4 | 1 | Refuses customer mail and records the would-have FU9 decision. Default off. Oscar runs `cases shadow-report`. `bin/mock-soak-azom.sh` is not A1. |
| Mail providers | 4 | 2 | Gmail, Outlook, Exchange Graph, generic IMAP, generic POP3, SMTP. Gmail browser OAuth is shipped. Outlook browser OAuth is parked (env + probe). |
| NO/DK mailboxes | 3 | 1 | Rows exist in `config/mailboxes.yaml` with `enabled: false`. Draft fixtures exist for `nb` and `da`. Do not flip `enabled` without Oscar and credentials. |
| WooCommerce and WordPress | 4 | 2 | Orders, shipment trackings, pagination, WordPress REST, HMAC webhook, probes. Order facts come from tools. |
| Support classify and draft | 4 | 2 | Hybrid classify, template fallback, OpenRouter drafts under the USD 100 cap in `config/limits.yaml`. `classify-eval` and `draft-eval` exist. Live calibration (20–50 redacted samples) is a human gate. |
| order-status and product-desc | 3 | 2 | V1 actions remain. `order_status_update.py`, `product_desc_gen.py`, and `support_handler.py` are deprecated CLI wrappers and are omitted from coverage in `pyproject.toml`. |
| SSH | 3 | 2 | Allowlisted read. Unsafe commands and code edits escalate to Oscar. |
| Dashboard | 4 | 2 | Onboarding, cases, marketing, Oscar secrets, GDPR export/delete, CSRF, `/health`, `/ready`, token-only `/metrics`. Prod server is Waitress. |
| Messenger | 4 | 2 | Daily-driver design. Webhook on the dashboard. Empty live allowlist denies everyone. |
| Telegram | 4 | 2 | Same `BotHandler` brain. Own `azom-bot.service`. Backup channel. Same fail-closed rule. |
| Marketing reads (Ads + GA4) | 4 | 2 | Mock digest, suggest queue, pacing, waste, Woo vs GA vs Ads. Live GA4 `runReport` and Ads GAQL when allowlists are set and mock is off. |
| Marketing writes | 2 | 1 | HITL queue in mock. Live Ads mutate, Measurement Protocol, and Merchant writes raise `NotImplementedError`. Flags in `config/marketing.yaml` are false. |
| RBAC and safety rails | 4 | 3 | Jonatan `viewer`, Oscar `full_admin`, agent `operator`. Guards are code (`skills/ecom_ops/rbac.py`), not only prose. Ops score is 3 because the rails are the default even before soak; the host setup is still unproven. |
| Observability and ops tools | 4 | 2 | `status`, `smoke`, `soak-preflight` (always leaves soak incomplete), KPIs, calibration report, WAL backup, retention purge, six runbooks under `docs/runbooks/`. |
| Install and runtime packaging | 4 | 2 | `bin/install.sh`, `bin/install-ubuntu26.sh`, systemd timers (poll, backup, daily brief, retention), Docker tag `azom-agent:3.0` with `2.0` kept for rollback. |
| Living documentation | 5 | — | [`CURRENT_STATE.md`](CURRENT_STATE.md) is the status contract. Operator docs are Swedish. Agent docs are English. Non-goals are named. |
| FAQ / knowledge base | 0 | — | Parked. No FAQ package is part of this tree. |
| Multi-tenant SaaS | 0 | — | Parked. |

## Human gates that cap the ops score

These stay open until Oscar and Jonatan write the outcome. This assessment does not fill them in.

1. **A1 live soak** on a real host. Checklist and the blocked 2026-07-29 outcome row live in [`PILOT_OPS.md`](PILOT_OPS.md). `python -m ecom_ops soak-preflight` must keep `soak_complete` false.
2. **Baseline** for support hours, so the “about 50% less support time” goal can be measured.
3. **Live classify calibration** before anyone lowers suggest-approve thresholds.

## Parked on purpose

Do not treat these as unfinished accidents:

- FU9 sender inside poll, and default-on auto-send.
- NO and DK mailboxes (`enabled: false`).
- Live Ads mutate, GA4 Measurement Protocol, and Merchant writes.
- Meta and TikTok ads. GA4 BigQuery. IMAP IDLE.
- Outlook browser OAuth UI.
- Multi-tenant SaaS.
- FAQ / knowledge base.

## How to revise this file

1. Read [`CURRENT_STATE.md`](CURRENT_STATE.md) and the surface doc you are re-scoring.
2. Confirm flags in `config/cases_ai.yaml`, `config/mailboxes.yaml`, and `config/marketing.yaml`.
3. Change a score only when code or a human outcome log changed.
4. Bump **Assessed on** (date, commit, package version).
5. Leave soak, FU9 wire, and NO/DK enablement as human gates unless [`CURRENT_STATE.md`](CURRENT_STATE.md) says they are done.

```bash
# cwd: repository root
# env: none required
git rev-parse --short HEAD
python -c "import ecom_ops; print(ecom_ops.__version__)"
# expect: short commit printed; version matches pyproject.toml (3.0.0 at the 2026-11-21 snapshot)
```
