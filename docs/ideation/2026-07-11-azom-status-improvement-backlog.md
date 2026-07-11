# AzomOps-Agent — statusanalys och förbättringsbacklog

**Datum:** 2026-07-11 · **Package:** 2.0.0 · **Läge:** V2 shipped / pilot-ready

Interaktiv vy: Cursor Canvas `azom-status-improvement-backlog.canvas.tsx`.

## Produktmål

50% mindre support-tid + hög engagement på 3 mån. Runtime: single-tenant på Hetzner CX22/CPX21 (Ubuntu 24/26).

## Statusöversikt

| Dimension | Bedömning | Motivering |
|-----------|-----------|------------|
| Leveransläge | V2.0 shipped / pilot-ready | V1+V2 acceptance klara |
| Kärnops | Stark | order, mail, SSH, support, cases i mock |
| Cases 2.0 | Stark (MVP) | SQLite, dashboard, Telegram, 5 min poll |
| Dashboard | Stark UX, medel säkerhet | Basic Auth + osaltad SHA-256 |
| AI-kvalitet | Svag vs mål | Keywords + mallar; OpenRouter stub |
| Prod-blindspot | Medel–hög risk | CI bara `AZOM_USE_MOCK=1` |
| Deploy | Stark | Install, systemd, Docker; UFW stänger 8080 |
| SaaS/V3 | Inte påbörjat | Multi-tenant medvetet out of scope |
| Process | Medel | ~116 tester; ingen lint/coverage-gate |

## Subsystem (djupdyk)

### ecom_ops
Action-services → RBAC/security → integrations → telemetry/escalation. Styrkor: mock-transports, lazy Woo, `CASE_REPLY` för Jonatan. Svagheter: legacy shim, stor `mail.py`, OpenRouter-stub, SQLite utan migrate.

### Cases 2.0
poll → ingest → SupportService → order-enrich → draft → approve/send. Gap: generiska mallar, outbound saknar robust threading headers, Telegram hardcodar `actor=jonatan`, poll-fel kan döljas vid partiell success.

### Support
Keyword-klassning + statiska mallar. Bra abuse-eskalering; otillräckligt för 50%-målet.

### Dashboard / Bot / Deploy
Flask Basic Auth, Oscar probes, OpenClaw-bot, one-shot install. Docker config ro + CDN-beroende.

## Kategoriserade förbättringar

### A. Produktvärde
1. LLM-draft för cases/support (OpenRouter + budget-cap)
2. Order-berikade svar som default
3. Snabbare triage (shortcuts, bulk close)
4. Mät support-tid (time-to-draft/approve, edit distance)
5. GA4/Ads (sekundärt)

### B. Tillförlitlighet
6. Mail threading headers vid approve/send
7. Mailbox-fel synliga + eskalering
8. SQLite schema version + migrate
9. Live/staging smoke
10. Konsekventa mock-defaults

### C. Säkerhet
11. Salted password hashes / SSO
12. CSRF på POST
13. Telegram actor mapping
14. Session tokens istället för Basic Auth
15. OAuth callback audit

### D. Arkitektur
16. Legacy shim/wrappers
17. Splitta `integrations/mail.py`
18. En RBAC-sanning
19. Config overlays i `AZOM_DATA_DIR`
20. Eskalering utan race (SQLite/lock)

### E. AI
21. Riktig `_openrouter_generator`
22. Hybrid classify (keywords abuse + LLM)
23. Suggest-approve vid hög confidence (aldrig auto-send)

### F. DX/CI
24. Coverage fail_under
25. Ruff i CI
26. Cov för dashboard-helpers
27. `docs/solutions/`
28. Dependabot / lockfile

### G. Ops
29. Docker data RW
30. Vendor CDN assets
31. Readiness vs liveness
32. Structured logs / rotation

### H. UX
33. Case triage polish (diff, order panel)
34. Jonatan read-only probes
35. Presence vs connectivity

### I. Observability
36. Telemetry schema med cost/case_id
37. Budget alarms (100$ cap)
38. Daily brief med case KPI

### J. V3
39. Tenant isolation
40. Control plane hooks
41. Outlook OAuth

### K. Compliance
42. Append-only audit log
43. PII retention
44. Secret redaction review

## Störst effekt (impact)

1. LLM case-drafts + order-kontext
2. Mail threading + synliga poll-fel
3. Support-loop KPI
4. Auth + CSRF + Telegram actor map
5. Live smoke Woo/IMAP
6. OpenRouter product-desc + budget alarm
7. Docker config/data overlay
8. CI gates + docs/solutions
9. Mail split + SQLite migrate
10. GA4/Ads (efter support-kärnan)

## Utvecklarordning

| Steg | Workstream |
|------|------------|
| P0 | Prod-path regressioner |
| P1 | Mail thread + poll errors |
| P2 | Auth harden |
| P3 | Telegram actor map |
| P4 | OpenRouter drafts + cap |
| P5 | Telemetry KPI |
| P6 | Live smoke + readiness |
| P7 | Docker RW + assets |
| P8 | CI ruff + fail_under |
| P9 | Migrations + mail split |
| P10 | Product-desc LLM / GA4 |

## Implementation i denna iteration

- Publicerad analys (denna fil + Canvas `azom-status-improvement-backlog.canvas.tsx`)
- P0: verifierade prod-path-tester (`tests/test_bugfixes_prod_paths.py`) — gröna
- P1: `In-Reply-To`/`References` vid case approve + eskalering vid mailbox poll-fel
- P2: Auth harden — Werkzeug/salted hashes, CSRF på POST, mock-lösen endast vid `AZOM_USE_MOCK=1`
- P3: `TELEGRAM_ACTOR_MAP` (`ecom_ops.bot.actors`) för approve/close/health/whoami
- P4: OpenRouter support/case-drafts via `ecom_ops.llm` med budget-cap + mall-fallback
- P5: KPI i telemetry — `time_to_approve_sec`, `draft_edit_distance`, `time_to_first_edit_sec`
- Nästa enligt canvas: P6 live smoke + readiness
