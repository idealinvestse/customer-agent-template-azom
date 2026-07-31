# Documentation index

**Purpose:** Canonical entry points and coverage map for living docs.  
**Audience:** Everyone.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md) for what is true now; [`DOC_STYLE.md`](DOC_STYLE.md) before editing docs.

Language split: **Swedish** = ops/pilot/runbooks · **English** = agents/dev/architecture. See [`DOC_STYLE.md`](DOC_STYLE.md).

## Ops (Swedish)

| Doc | Audience | Topic |
|-----|----------|--------|
| [`PILOT_OPS.md`](PILOT_OPS.md) | Oscar / Jonatan | Daily drift, dashboard, **live soak**, runbooks |
| [`CASES.md`](CASES.md) | Support ops | Cases 2.0 + Path B + **FU9 auto-send gates** |
| [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) | Oscar / ops | Gmail/Outlook/Graph/IMAP/POP3 setup |
| [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) | Ops | Meta Messenger daily driver |
| [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) | Ops | Telegram backup bot |
| [`V2_OAUTH_GMAIL.md`](V2_OAUTH_GMAIL.md) | Setup | Gmail browser OAuth |
| [`AUTO_INSTALL.md`](AUTO_INSTALL.md) | Ops | One-shot Ubuntu install |
| [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md) | Ops | Hetzner sizing & deploy |
| [`DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md) | Ops | Config ro vs data rw |
| [`COMPLIANCE.md`](COMPLIANCE.md) | Oscar | GDPR / retention / gaps |
| [`runbooks/`](runbooks/) | Ops | Incident procedures |

## Agents / developers (English)

| Doc | Audience | Topic |
|-----|----------|--------|
| [`../AGENTS.md`](../AGENTS.md) | Coding agents | Always-read operating notes |
| [`../SOUL.md`](../SOUL.md) | OpenClaw / LLM | Voice + hard constraints (Swedish prose) |
| [`CURRENT_STATE.md`](CURRENT_STATE.md) | Agents / eng | Version, shipped, blockers |
| [`DOC_STYLE.md`](DOC_STYLE.md) | Authors / agents | How to write docs |
| [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) | Everyone | Architecture map |
| [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) | Developers | Setup, mock, tests, contrib |
| [`CLI_REFERENCE.md`](CLI_REFERENCE.md) | Developers | Full CLI |
| [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) | Developers | Woo/WP V2.1 capabilities |
| [`../skills/ecom-ops/SKILL.md`](../skills/ecom-ops/SKILL.md) | Skill hosts | Skill card |
| [`../README.md`](../README.md) | Everyone | Project intro + quick start |

## Coverage matrix

Each major surface has one **primary** living doc:

| Surface | Primary doc |
|---------|-------------|
| Current status / blockers | [`CURRENT_STATE.md`](CURRENT_STATE.md) |
| Architecture / RBAC / security | [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) |
| Local dev / CI | [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) |
| CLI | [`CLI_REFERENCE.md`](CLI_REFERENCE.md) |
| Cases + approve + FU9 | [`CASES.md`](CASES.md) |
| Pilot / soak / dashboard day-2 | [`PILOT_OPS.md`](PILOT_OPS.md) |
| Mail providers | [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) |
| Telegram | [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) |
| Messenger | [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) |
| Woo / WordPress | [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| Install / deploy / Docker | [`AUTO_INSTALL.md`](AUTO_INSTALL.md) + deploy/docker docs |
| Secrets / Gmail OAuth | [`V2_OAUTH_GMAIL.md`](V2_OAUTH_GMAIL.md) + `.env.example` |
| Incidents | [`runbooks/`](runbooks/) |
| Compliance | [`COMPLIANCE.md`](COMPLIANCE.md) |
| Agent voice | [`../SOUL.md`](../SOUL.md) |

## Source of truth

When docs disagree: **`CURRENT_STATE.md` > `AGENTS.md` > code/tests > chat memory**.

Historical specs/plans/ideation/solutions were **deleted after absorption** into living docs. Do not recreate them; update the living doc for the surface instead.
