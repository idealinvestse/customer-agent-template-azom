# AGENTS.md – Azom customer agent

## Budget & roller
- Budget: 100$ OpenRouter (`config/limits.yaml`)
- Jonatan: read-only / viewer
- Oscar: full_admin + escalation target (critical + code_edit)
- Agent automation: operator

## Mål
- 3 mån: 50% mindre support-tid + hög engagement
- Onboarding (V2): dedikerad Telegram-bot + lösenordsskyddad webbdashboard

## V1 (klart)
- order-status, product-desc, support, SSH via `skills/ecom_ops`
- Kör: `python -m ecom_ops --help` eller `./bin/ecom-automation.sh`
- Tester: `pytest`
