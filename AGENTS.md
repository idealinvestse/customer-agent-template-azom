# AGENTS.md – Azom customer agent (v2.0)

## Budget & roller
- Budget: 100$ OpenRouter (`config/limits.yaml`)
- Jonatan: viewer (+ mail read, SSH read, non-secret settings, **case reply approve/send**)
- Oscar: full_admin + escalation target (critical + code_edit + secrets UI + experiment flags)
- Agent automation: operator (order/product/support/mail send+read/SSH read/case poll)

## Mål
- 3 mån: 50% mindre support-tid + hög engagement
- Onboarding: Telegram-bot + lösenordsskyddad webbdashboard ✅ (v2)

## Runtime target
- **Package version:** 2.0.0
- **OS:** Ubuntu 26.x (primary) / 24.04 LTS
- **Host:** Hetzner Cloud — **CX22 / CPX21** (2 vCPU, 4 GB RAM)
- **Auto-install:** `sudo bash bin/install.sh` eller `bin/install-ubuntu26.sh`
- Docs: `docs/SYSTEM_OVERVIEW.md`, `docs/AUTO_INSTALL.md`, `docs/V2_RELEASE.md`, `docs/DEPLOY_UBUNTU24_HETZNER.md`

## Identitet (OpenClaw)
- **SOUL:** `SOUL.md` — svenska, human-in-the-loop, ingen silent send, order-sanning via tools
- **Skill card:** `skills/ecom-ops/SKILL.md`
- Telegram hybrid: `docs/TELEGRAM_OPENCLAW.md`

## V1 (core — kvar i v2)
- order-status, product-desc, support, SSH, mail via `skills/ecom_ops`
- Mail providers: gmail, outlook, exchange_graph, generic_imap, generic_pop3
- CLI: `python -m ecom_ops` · `./bin/ecom-automation.sh`
- Tester: `pytest` (CI: ruff + cov ≥ 65%)

## V2.0 (klart)
- Dashboard onboarding wizard + HTML views (telemetry/escalations/interact)
- Settings UI: Jonatan (non-secret) · Oscar (secrets + resolve escalations)
- Gmail OAuth browser consent → `AZOM_DATA_DIR/oauth/gmail.json`
- Telegram bot: OpenClaw-style (`/help` `/commands` `/status` `/whoami` `/new` `/reset` `/stop` `/tools` `/tasks` `/usage` `/model` `/verbose` `/think` `/skill` `/context` …) + `/order` `/cases` `/health` `/brief` (`python -m ecom_ops.bot`)
- Hybrid free-text: tool prefetch (order/cases/ops) + LLM phrasing; approve endast explicit path
- Oscar secrets: anslutningstester (`POST /oscar/secrets/test`) för Woo/mail/Telegram/OpenRouter/SSH/Gmail OAuth
- Dashboard ops polish: nav-badges (ärenden/eskaleringar), snabb översikt (presence + `probe_last.json`), live onboarding/Gmail-status via Alpine, case-triage + approve-confirm
- One-shot Ubuntu install + systemd + prod Docker (`azom-agent:2.0`)
- CLI: `python -m ecom_ops version` · `python -m ecom_ops status` · `python -m ecom_ops smoke`
- Spec: `docs/superpowers/specs/2026-07-11-dashboard-ops-polish-design.md`

## Cases 2.0 + Path B
- Status: `open` \| `escalated` \| `replied` \| `closed`
- Trådning (In-Reply-To), mark_read, order-berikad draft, eskaleringslänk
- Suggest-approve badge (`config/cases_ai.yaml`); auto-send rails **default off** + `AZOM_AUTO_SEND_KILL`
- Config: `config/mailboxes.yaml` · DB: `AZOM_DATA_DIR/cases.db`
- Dashboard: `/cases` (filter, spara draft, RBAC close) · översikt med counts
- Telegram: `/cases` · `show` · `approve` · `close` (+ NL suggest/confirm, aldrig silent send)
- systemd: `azom-cases-poll.timer` (5 min)
- CLI:
```bash
python -m ecom_ops --mock cases poll
python -m ecom_ops --mock cases list --status open,escalated
python -m ecom_ops --mock cases draft --id <uuid> --body "..."
python -m ecom_ops --mock cases reply --id <uuid>
python -m ecom_ops --mock cases close --id <uuid>
./bin/cases-poll.sh
```
- Docs: `docs/CASES.md` · Spec: `docs/superpowers/specs/2026-07-11-cases-v2-design.md` · Plan: `docs/superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`

## Mail CLI
```bash
python -m ecom_ops --mock mail send --to a@b.co --subject "Test" --body "Hej"
python -m ecom_ops --mock mail fetch
python -m ecom_ops status
```

## Telegram env (prod)
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=...          # rekommenderas
TELEGRAM_ACTOR_MAP=chat:jonatan,...    # unmapped → jonatan
```

## Prod paths (Ubuntu)
- Code: `/opt/azom-agent`
- Data: `/var/lib/azom`
- Logs: `/var/log/azom`
- Env: `/opt/azom-agent/.env` (`AZOM_USE_MOCK=0`)

## Nästa utveckling (färdigställ mot mål)
- Finish overview: `docs/DEVELOPMENT_PLAN_FINISH.md`
- **Active sprint track:** `docs/superpowers/plans/2026-07-16-001-sprint-a-approve-flow-and-measure-plan.md`
  - Sprint A: approve-flow (order panel, nästa, ★-count, `/brief`, 7d KPI)
  - Sprint B **auto-start** when A exit gates green: order extract, email→Woo, richer context, classify fixtures
- Inte i scope nu: V3 multi-tenant, GA4/engagement-program, default-on auto-send
