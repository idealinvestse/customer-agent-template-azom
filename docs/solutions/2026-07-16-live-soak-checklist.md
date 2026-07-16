# Live soak checklist (H2 / FU6) — post Sprint A+B

**Purpose:** Verify suggest-approve coverage, approve&next, poll, and KPIs on a real (or staging) host before auto-send conversation.  
**Owner:** Oscar (ops) + Jonatan (approve sample).  
**Package:** after commits `660f437` / `28c34a7`+.

## Pre-flight

```text
[ ] AZOM_USE_MOCK=0 on prod host
[ ] TELEGRAM_ALLOWED_CHAT_IDS set (required in prod)
[ ] TELEGRAM_ACTOR_MAP set (chat:jonatan / chat:oscar) — unmapped chats denied when map present
[ ] AZOM_AUTO_SEND_KILL=1 optional belt; cases_ai auto_send_enabled: false
[ ] Mailbox credentials / Gmail OAuth OK
[ ] systemd: azom-dashboard, azom-bot, azom-cases-poll.timer active
[ ] Backup path known: AZOM_DATA_DIR/cases.db + secrets.env
```

## Soak script (one session)

```bash
# On host
cd /opt/azom-agent   # or deploy root
source .venv/bin/activate
export AZOM_USE_MOCK=0

# 1) Readiness
python -m ecom_ops status
curl -sS http://127.0.0.1:8080/health | head

# 2) Poll
python -m ecom_ops cases poll
# expect created/skipped/errors; if partial errors → escalation ticket + last_case_poll.errors

# 3) Queue sample
python -m ecom_ops cases list --status open,escalated
python -m ecom_ops kpis --days 7

# 4) Daily brief (cases + budget + readiness)
bash bin/daily-brief-azom.sh
# Telegram: /brief  /cases  “föreslagna”
```

### Operator (Jonatan) — dashboard / Telegram

```text
[ ] Open /cases?suggest=1 (or ★ link) — n★ noted: _____
[ ] Open one ★ case — order panel shows status/total (and payment/frakt if Woo has them)
[ ] Godkänn & nästa once — lands on next open without re-list (filter preserved)
[ ] Telegram: Visa + Godkänn once on a safe routine case
[ ] One regenerate (optional) — does not send
```

### ★ sample quality (n≥10 when volume allows)

| # | id8 | category | has order_id | suggest OK? (Y/N) | notes |
|---|-----|----------|--------------|-------------------|-------|
| 1 | | | | | |
| … | | | | | |

False-positive suggest on return/billing/abuse must be **0**. If any, file ticket and **do not** lower thresholds.

## Post-soak

```text
[ ] python -m ecom_ops kpis --days 7 → note median TTA / n_approved
[ ] Append proxy numbers or “blocked_on Jonatan hours” to docs/ideation/baseline-capture.md
[ ] Outcome one-liner below
```

### Outcome log

| date | host | n poll create | n★ sample | FP suggest | TTA median | notes |
|------|------|---------------|-----------|------------|------------|-------|
| _TBD_ | | | | | | |

## Not in soak

- Enabling `auto_send_enabled` (see FU9 preconditions doc)
- Lowering suggest confidence without fixture update
