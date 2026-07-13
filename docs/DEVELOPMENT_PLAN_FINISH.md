# Utvecklingsplan — färdigställ systemet mot nuvarande mål

**Datum:** 2026-07-13  
**Package:** 2.0.0  
**Horizon:** ~4–8 veckor till “pilot complete / path-B done”, därefter mätning och ev. auto-send-experiment  
**Supersedes for sequencing:** Path B plan units U1–U7 + thin tracks A/C from locked ideation  

| Primary docs | |
|--------------|--|
| Goals / roles | [`AGENTS.md`](../AGENTS.md), [`SOUL.md`](../SOUL.md) |
| Architecture | [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) |
| Locked strategy | [`ideation/2026-07-11-azom-project-overview-next-steps-scope.md`](ideation/2026-07-11-azom-project-overview-next-steps-scope.md) |
| Path B detail | [`superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`](superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md) |

---

## 1. Vad “färdigt” betyder (nuvarande mål)

| Mål (från AGENTS / beslut) | Definition of done |
|----------------------------|--------------------|
| **Support-tid ↓ (riktning 50% på 3 mån)** | Jonatan kan triaga och approve:a rutinärenden (order_status / shipping med order-id) med **lågt friktion**; suggest-approve används; telemetry mäter time-to-approve / edit-distance; **baseline** finns så % kan beräknas senare |
| **Onboarding** | ✅ Dashboard + Telegram redan levererat |
| **Säker automation** | Human approve default; auto-send endast Oscar-flaggad experiment med rails + kill-switch + audit |
| **Live credibility** | Poll/mail/Woo-fel synliga; readiness OK; budget under cap inte tyst; secrets/probes fungerar |
| **Out of scope tills support rör sig** | V3 multi-tenant, GA4/engagement-program, FAQ/KB, IMAP IDLE, SSO, default-on auto-send |

**“Systemet färdigt i sin nuvarande form”** = Path B DoD stängd + production-grade support loop + mätbar baseline — **inte** SaaS-plattform.

---

## 2. Nuläge (evidence)

### Levererat (closure mot Path B U1–U5 + hybrid vNext)

| Unit / area | Status | Evidence |
|-------------|--------|----------|
| U1 Hybrid classify + confidence + suggest eligibility | ✅ | `support.py`, `llm.classify_support_with_llm`, `cases_ai.yaml`, schema columns |
| U2 Order-context drafts | ✅ | `order_context`, draft path tests |
| U3 Auto-send rails (default off) | ✅ | `auto_send.py` — **not wired into poll sender** |
| U4 Dashboard suggest UX | ✅ | `cases.html` filter `suggest=1`, `case_detail` badge + shorter confirm |
| U5 Telegram suggest + hybrid NL | ✅ | `/cases` ★, chat tool prefetch, NL confirm-only |
| Docs / SOUL | ✅ (docs wave) | SYSTEM_OVERVIEW, CASES, TELEGRAM_OPENCLAW, SOUL |

### Öppet för att stänga nuvarande mål

| ID | Gap | Impact |
|----|-----|--------|
| **F1** | **U6** regenerate draft + tydligare confidence/order-panel | Direkt friktion för Jonatan |
| **F2** | **U7** baseline-capture (process + placeholder + ev. KPI-export) | Krävs för 50%-story |
| **F3** | Daily brief / overview: case-kö + suggest-count + budget headroom | Oscar/Jonatan cadence |
| **F4** | Soft budget alarm (OpenRouter near-cap) i status/brief/dashboard | Stoppar tysta LLM-död |
| **F5** | Live soak checklist (prod smoke, allowlist, actor map, poll timer) | Credibility |
| **F6** | Classify quality iterate (validera LLM vs keyword på live samples) | Färre fel-suggest |
| **F7** | Optional: auto-send **experiment wiring** (Oscar-only, narrow) | Endast om F1–F6 gröna |
| **F8** | Nice: bulk close/skip, draft diff, regenerate on Telegram | After core DoD |

---

## 3. Success metrics (hur vi vet att vi är klara)

### Hard (automatiserbara)

1. `pytest` grön; CI ruff + cov ≥ 65%.
2. `auto_send_enabled: false` default; kill-switch testad; **inget poll-auto-send**.
3. Suggest-approve endast på allowlist + threshold + order_id; abuse/return/billing aldrig.
4. `python -m ecom_ops status` readiness speglar poll-age; `/health` 200 i prod.
5. Telemetry innehåller `time_to_approve_sec` / `draft_edit_distance` på minst en live approve-vecka.

### Soft (människa)

6. Jonatan: “rutin order-status tar klart mindre klick/tid” (kvalitativ) efter F1.
7. Veckovis 15–30 min sync (Jonatan + Oscar) körs minst 3 gånger i rad utan processkaos.
8. Baseline siffra eller proxy sparad (timmar/vecka **eller** median time-to-approve × volym).

### Explicit icke-mål denna plan

- Multi-tenant control plane  
- GA4 / Ads “hög engagement”-program  
- Default auto-send  
- Full SSO / CDN vendor-program  

---

## 4. Fasplan

```text
Fas 0   Close Path B residual     (F1, F2)           ~3–7 dagar
Fas 1   Measure & operate         (F3–F5)            ~1–2 veckor
Fas 2   Quality tighten           (F6, F8 lite)      ~1–2 veckor
Fas 3   Optional auto-send trial  (F7)               endast om grön
Fas 4   Re-evaluate goals         (50% / engagement) efter data
```

Allt TDD där kod; live ändringar bakom mock-first + staging/mock smoke.

---

## 5. Fas 0 — Stäng Path B DoD

### F0.1 · U6 Regenerate draft + triage polish (P0)

**Mål:** Jonatan kan regenerera draft utan att lämna detaljvyn; confidence/order är omedelbart läsbart.

| | |
|--|--|
| **API** | `CaseService.regenerate_draft(case_id, actor=…)` → re-run support draft + order context; RBAC `CASE_REPLY` / admin |
| **Telemetry** | `case_draft_regenerated` (+ cost om LLM) |
| **Dashboard** | Knapp “Regenerera utkast” på `case_detail`; behåll confirm på skicka |
| **Telegram (min)** | Valfritt: `/cases regenerate <id8>` eller “regenerera” NL → **inte** skicka; samma service |
| **Tests** | `tests/test_case_regenerate.py` — update draft, deny wrong actor, abuse stays escalated |
| **Files** | `cases/service.py`, dashboard templates/app, optional `openclaw_commands.py` |

**Done when:** mock case regenerate uppdaterar draft; UI + tests gröna.

### F0.2 · U7 Baseline capture scaffolding (P0 process)

**Mål:** Inte blockera kod; gör 50%-målet mätbart när Jonatan är nåbar.

| | |
|--|--|
| **Doc** | `docs/ideation/baseline-capture.md` — fält: start_date, hours_per_week_or_proxy, source, notes |
| **Proxy preference** | Median `time_to_approve_sec` × cases/week från telemetry (när data finns) |
| **Owner** | User + Jonatan (async OK); Oscar fyller inte gissningar |
| **Code (optional light)** | CLI snippet eller brief-sektion som listar last-7d approve KPI om data finns |

**Done when:** fil finns med instruktion; första siffra fylls när kontakt finns (kan vara “TBD” initialt).

### F0.3 · Path B plan status note

Uppdatera Path B-planens unit-status (U1–U5 shipped, U6/U7 this finish plan) så agents inte omimplementerar.

---

## 6. Fas 1 — Operate & measure (support-loop production)

### F1.1 · Daily brief + overview case slice (P1)

**Mål:** Cadence utan extra verktyg.

Utöka `bin/daily-brief-azom.sh` (och ev. `/brief` / dashboard overview) med:

- open + escalated count  
- suggest_approve count  
- last poll age / readiness  
- llm_cost_usd vs openrouter_cap  
- top 3 stuck cases (id8, category, age)

**Done when:** brief JSON innehåller case-fält; timer fortfarande grön.

### F1.2 · Budget near-cap soft alarm (P1)

| Threshold | Action |
|-----------|--------|
| ≥ 80% of cap | Flag in `status`, dashboard overview, `/status` Telegram |
| ≥ 100% | LLM classify/draft already skip — ensure UX says “template/tools only” |

Config optional: `config/limits.yaml` `openrouter_warn_ratio: 0.8`.

**Done when:** tests med high telemetry sum set flag; no secret leak.

### F1.3 · Production soak checklist (P1 ops)

Kör en gång live (Oscar):

```text
[ ] TELEGRAM_ALLOWED_CHAT_IDS + TELEGRAM_ACTOR_MAP set
[ ] AZOM_USE_MOCK=0; services enabled (dashboard, bot, cases-poll.timer)
[ ] MAIL_PROVIDER + credentials; Gmail OAuth if used
[ ] cases poll creates/updates cases; mark_read OK
[ ] suggest-approve appears only on safe types in live sample (n≥10)
[ ] approve path: dashboard + Telegram once each
[ ] AZOM_LIVE_SMOKE / manual smoke; /health readiness not stale
[ ] AZOM_AUTO_SEND_KILL=1 optional belt; cases_ai auto_send_enabled false
[ ] Backup note: cases.db + secrets.env path known
```

Result → kort logg i `docs/solutions/` eller ideation note (datum + outcome).

---

## 7. Fas 2 — Quality tighten (mot 50%)

### F2.1 · Classify quality loop (P1)

1. Export 20–50 live (anonymiserade) inbound subject/body labels (Jonatan: true category).  
2. Score keyword vs LLM hybrid confusion.  
3. Justera:  
   - abuse keyword list  
   - category prompts  
   - suggest thresholds i `cases_ai.yaml` (data-driven, not vibes)  
4. Regression tests med representative fixtures (inga råa PII i repo).

**Done when:** false-positive suggest på return/billing = 0 i sample; order_status recall “good enough” per Jonatan.

### F2.2 · Friction polish lite (P2)

Prioritera efter Jonatan feedback — max 1–2:

| Item | Notes |
|------|--------|
| Draft diff (old vs new after regenerate) | Dashboard only |
| Order panel always visible when order_id | May already be partial |
| Keyboard shortcut / one-click filter “★ only” | Dashboard mostly done |
| Telegram regenerate | If F0.1 min path was CLI-only |

**Stop rule:** om det inte sparar klick i approve-path — skippa.

### F2.3 · Cadence lock-in (process)

Weekly checklist (från locked ideation §7) — spara 3 veckor anteckningar i baseline-doc appendix.

---

## 8. Fas 3 — Optional auto-send experiment (endast om grön)

**Preconditions (all must be true):**

1. F0–F2 done; suggest-approve precision high on `order_status`.  
2. ≥ 2 weeks human approve without serious bad send.  
3. Oscar explicit written enable for experiment window.  
4. `auto_send_enabled: true` **only** in data overlay / carefully reviewed config — kill-switch armed.  
5. Wire **one** call site (post-ingest eligible only), not broad poll blind send.  
6. Telemetry `case_auto_sent` + daily cap + allowlist `order_status` only, conf ≥ 0.92, order_id required.  
7. Rollback plan: set false + `AZOM_AUTO_SEND_KILL=1` within 1 minute of incident.

**Not done in this plan unless preconditions met.** Rails already exist — do not redesign.

---

## 9. Fas 4 — Re-evaluate product goals

Efter att baseline + 2–4 veckor KPI finns:

| Om… | Då… |
|-----|-----|
| time-to-approve / hours drop materially | Öppna engagement (D) eller lätt multi-site polish — fortfarande inte V3 |
| Support still bottleneck on returns/billing | Path B2: drafts for returns (never auto-send) + clearer escalate |
| Ops fragile | Companion C slice only (logging, alerts) |
| Cap constant burn | Raise cap **or** reduce LLM classify; keep drafts |

V3 (multi-tenant) förblir **senare produkt**, inte “färdigställ nuvarande Azom-pilot”.

---

## 10. Implementation units (executable backlog)

Ordning rekommenderad; en unit i taget, TDD.

| Unit | Fas | Est. | Dependencies | Primary files |
|------|-----|------|--------------|---------------|
| **FU1** regenerate_draft + tests | 0 | S | — | `cases/service.py`, dashboard, tests |
| **FU2** regenerate UI + optional Telegram | 0 | S | FU1 | templates, `openclaw_commands.py` |
| **FU3** baseline-capture.md + KPI dump helper | 0 | XS | — | `docs/ideation/…`, optional brief |
| **FU4** daily brief case counts + readiness | 1 | S | — | `bin/daily-brief-azom.sh`, maybe `ops_status` |
| **FU5** budget warn flag | 1 | S | — | `limits.yaml`, `status` CLI, dashboard overview, tests |
| **FU6** live soak checklist + write-up | 1 | XS | prod access | docs/solutions or ideation |
| **FU7** classify fixture suite + threshold tune | 2 | M | live samples | `support`/`llm` tests, `cases_ai.yaml` |
| **FU8** friction polish (pick 1) | 2 | S | Jonatan input | dashboard |
| **FU9** auto-send wire (gated) | 3 | M | FU1–7 green + Oscar | `cases/service.py`, `auto_send.py`, tests |

S ≈ 0.5–1.5 d, M ≈ 2–4 d (enkel dev; live access kan elongera FU6/FU7).

---

## 11. Risker

| Risk | Mitigation |
|------|------------|
| Regenerera bränner budget | Cap check; template fallback; throttle button (1/min) optional |
| Suggest precision låg live | FU7 innan mer automation; never widen allowlist early |
| Auto-send oavsiktlig | Fas 3 preconditions; kill-switch; no silent default |
| Baseline aldrig fylls | Proxy från telemetry; notera “blocked on Jonatan contact” |
| Scope creep (V3/GA4) | Denna plan är gate — säg nej om det inte minskar support-tid |

---

## 12. Första exekverbara steg (börja här)

1. **FU1** — `CaseService.regenerate_draft` + `tests/test_case_regenerate.py`  
2. **FU2** — dashboard-knapp  
3. **FU3** — `docs/ideation/baseline-capture.md`  
4. **FU4–FU5** — brief + budget warn  
5. **FU6** — live soak med Oscar  

Därefter: Jonatan-vecka med suggest-filter + regenerate; justera trösklar (FU7).

---

## 13. Definition of Done — “systemet färdigt i nuvarande mål”

- [ ] Path B U1–U6 merged; U7 baseline doc exists (siffra när möjlig)  
- [ ] Human approve required; auto-send default off  
- [ ] Daily brief shows cases + budget headroom  
- [ ] Live soak checklist completed once  
- [ ] Suggest-approve calibrated on real mailbox sample  
- [ ] Weekly cadence running; no open P0 prod bugs in poll/approve  
- [ ] Docs point here: README/AGENTS/SYSTEM_OVERVIEW “finish plan” link  
- [ ] **Not required:** V3, GA4, default auto-send, FAQ/KB  

När checklistan är avbockad är AzomOps **färdig som single-tenant support-ops agent** under nuvarande målbild. Nästa produktbeslut (engagement / V3 / auto-send trial) tas med data, inte magkänsla.

---

## 14. Mapping till tidigare artefakter

| Earlier | Status in this plan |
|---------|---------------------|
| Path B U1–U5 | Done — do not rebuild |
| Path B U6–U7 | Fas 0 (FU1–FU3) |
| Option A measure | Fas 1 thin (baseline, brief, soak) |
| Option C harden | Only blockers in soak (FU6) |
| Option D engagement | Fas 4 decision only |
| Backlog P0–P10 | Shipped; residual ops via Fas 1 |

## 15. Execution log

| Unit | Status | Notes |
|------|--------|-------|
| FU1 regenerate_draft + tests | ✅ 2026-07-13 | `CaseService.regenerate_draft`, `tests/test_case_regenerate.py` |
| FU2 dashboard + Telegram regenerate | ✅ 2026-07-13 | case_detail button, `/cases regenerate`, CLI `cases regenerate` |
| FU3 baseline-capture.md | ✅ | `docs/ideation/baseline-capture.md` (siffra TBD med Jonatan) |
| FU4 daily brief cases + readiness | ✅ | `bin/daily-brief-azom.sh` |
| FU5 budget near-cap | ✅ | `ecom_ops.budget`, status CLI, `/status`, overview warn, limits.yaml |
| FU6 live soak | ⬜ | Needs prod access (Oscar) |
| FU7–FU9 | ⬜ | Classify tune / friction / auto-send later |

---

*Plan authored 2026-07-13. FU1–FU5 shipped in finish execution; next = FU6 live soak when VPS available.*
