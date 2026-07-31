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

**Canonical checklist:** [`docs/solutions/2026-07-16-live-soak-checklist.md`](solutions/2026-07-16-live-soak-checklist.md)  
**FU9 (do not wire yet):** [`docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`](solutions/2026-07-16-fu9-auto-send-preconditions.md)

```text
[ ] TELEGRAM_ALLOWED_CHAT_IDS + TELEGRAM_ACTOR_MAP set (unmapped denied when map set)
[ ] AZOM_USE_MOCK=0; services enabled (dashboard, bot, cases-poll.timer)
[ ] MAIL_PROVIDER + credentials; Gmail OAuth if used
[ ] cases poll creates/updates cases; mark_read OK; partial failures escalated + readiness.partial
[ ] suggest-approve appears only on safe types in live sample (n≥10)
[ ] approve path: dashboard Godkänn&nästa + Telegram once each
[ ] python -m ecom_ops kpis --days 7; /brief shows queue + budget
[ ] AZOM_LIVE_SMOKE / manual smoke; /health readiness not stale / not partial
[ ] AZOM_AUTO_SEND_KILL=1 optional belt; cases_ai auto_send_enabled false
[ ] Backup note: cases.db + secrets.env path known
```

Result → fyll outcome-tabell i soak-checklisten.

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

### Active execution track (2026-07-16+)

**FU1–FU5 effectively shipped on main.** Residual measure/friction + suggest coverage is sequenced in:

**[`docs/superpowers/plans/2026-07-16-001-sprint-a-approve-flow-and-measure-plan.md`](superpowers/plans/2026-07-16-001-sprint-a-approve-flow-and-measure-plan.md)**

| Sprint | Units | Maps to |
|--------|-------|---------|
| **A** friction + measure | SA1–SA5 (order panel, approve&next, ★ count, `/brief` parity, 7d KPI) | FU8 + residual FU3/FU4 |
| **B** (auto-start after A gates) | SB1–SB4 (order extract, email→Woo, richer context, classify fixtures) | FU7 + capacity |
| **C / D later** | ops harden / auto-send | FU6, FU9 |

**Rule:** when Sprint A exit gates G1–G6 are green, agents **start SB1 immediately** without a new replan.

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

### Kod / repo (2026-07-29 Fas 0 verified)

- [x] Path B U1–U6 merged; U7 baseline doc exists (siffra när möjlig — proxy tooling: `ecom_ops kpis`)  
- [x] Human approve required; auto-send default off (FU9 preconditions only)  
- [x] Daily brief / `/brief` shows cases + budget headroom  
- [x] Soak **checklist** + mock soft-soak (`bin/mock-soak-azom.sh`); classify fixtures + `classify-eval`  
- [x] Suggest-approve rails + never-list regression fixtures (live threshold tune still data-gated)  
- [x] Poll partial-fail visibility + Telegram actor fail-closed when map set  
- [x] Docs: README / AGENTS / SYSTEM_OVERVIEW / this finish plan + sprint track  
- [x] **v2.3 Fas 0 (2026-07-29):** Core 3.0 ideation landed; AGENTS drift fixed; gate pytest (`test_auto_send_rails`, `test_probe_mail`, bulk close) — next = Oscar A1  

### Människa / prod (kan inte stängas av agent ensam)

- [ ] Live soak checklist **executed once** on prod host (Oscar + Jonatan) — [`docs/solutions/2026-07-16-live-soak-checklist.md`](solutions/2026-07-16-live-soak-checklist.md) — **blocker documented 2026-07-29** (`blocked_on: Oscar prod access`)  
- [ ] Baseline hours or KPI proxy filled after first live week  
- [ ] Weekly cadence 3× without process chaos  

- [ ] **Not required:** V3, GA4, default auto-send, FAQ/KB  

**Kod-DoD för “systemet i nuvarande form” = grön.** Live-DoD = soak + baseline when humans run. Nästa produktbeslut (engagement / V3 / auto-send trial) tas med data, inte magkänsla.

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
| FU6 live soak | ⬜ | Needs prod access (Oscar) — **blocker listed** in soak checklist 2026-07-29 |
| FU7 fixtures/prompts (Approach A) | ✅ partial 2026-07-29 | `azom-no-support-vnext`: prompts 1.1, nb/da templates, NO classify+draft fixtures, draft-eval fix; NO mailboxes remain `enabled: false` |
| FU7 live threshold tune | ⬜ | Needs live soak samples (after FU6); do not widen suggest allowlist early |
| FU8 friction polish | ✅ partial 2026-07-29 | Mail Task 6: draft diff + 60s regenerate throttle (dashboard) |
| FU9 auto-send wire | ⬜ | Gated — all FU9 preconditions + Oscar written enable; **v2.3 Fas 3 = docs only** |
| v2.3 Fas 0 verify (AGENTS + Core 3.0 + gate pytest) | ✅ 2026-07-29 | Agent: docs sync + deny-by-default / probe_mail / bulk-close green; next = Oscar A1 |

---

*Plan authored 2026-07-13. FU1–FU5 shipped in finish execution; next = FU6 live soak when VPS available.*

---

## 16. Mail & support vidareutveckling (2026-07-28)

Sequenced roadmap after this finish plan’s code DoD:

- Spec: [`docs/superpowers/specs/2026-07-28-mail-support-roadmap-design.md`](superpowers/specs/2026-07-28-mail-support-roadmap-design.md)
- Plan: [`docs/superpowers/plans/2026-07-28-001-mail-support-vidareutveckling-plan.md`](superpowers/plans/2026-07-28-001-mail-support-vidareutveckling-plan.md)
- Azom.no support quality (2026-07-29): [`docs/superpowers/specs/2026-07-29-azom-no-support-vnext-design.md`](superpowers/specs/2026-07-29-azom-no-support-vnext-design.md) — FU7 fixture/prompt expansion; does **not** enable live NO poll or FU9

**Order:** live soak/baseline/classify (Fas 0) → mail OAuth persist + per-mailbox creds (Fas 1) → triage friction (Fas 2) → FU9 wire only if gated (Fas 3) → re-evaluate (Fas 4).

### v2.2 — Ops-proof + narrow automation-ready (2026-07-29)

- Spec: [`docs/superpowers/specs/2026-07-29-mail-kundhantering-v22-design.md`](superpowers/specs/2026-07-29-mail-kundhantering-v22-design.md)
- Plan: [`docs/superpowers/plans/2026-07-29-001-mail-kundhantering-v22-plan.md`](superpowers/plans/2026-07-29-001-mail-kundhantering-v22-plan.md)
- Next code: **landed via v2.3 Fas 0** — B1 + docs + bulk test on main after commit; Fas A soak still human; Fas C FU9 still gated

### v2.3 — Live proof + gated auto-send (2026-07-29)

- Spec: [`docs/superpowers/specs/2026-07-29-mail-kundhantering-v23-design.md`](superpowers/specs/2026-07-29-mail-kundhantering-v23-design.md)
- Plan: [`docs/superpowers/plans/2026-07-29-002-mail-kundhantering-v23-plan.md`](superpowers/plans/2026-07-29-002-mail-kundhantering-v23-plan.md)
- Next: **Oscar A1 live soak**; agent must not wire FU9 until A1–A3 + written enable
- **Fas 0 verified 2026-07-29** (AGENTS + Core 3.0 + gate pytest). Fas 3 remains docs-only until gates.

### Core 3.0 — fördjupa Azom / azom.no (2026-07-29)

- Ideation: [`docs/ideation/2026-07-29-azom-core-30-deepen.md`](ideation/2026-07-29-azom-core-30-deepen.md)
- Scope: **single customer Azom only** — deepen support core + live azom.no; multi-tenant SaaS remains parked
- Sequence: soak → NO enable (Oscar+creds) → FU7 tune / Path B2 → gated FU9 → KPI re-eval
- Path B2 (return/billing draft quality + triage): ✅ code — `docs/plans/2026-07-29-003-path-b2-return-billing-drafts.md`
- Agent now: **no** FU9 wire; **no** repo `enabled: true` for NO/DK without Oscar; FU7 still blocked on soak samples

### Execution log (mail-support roadmap)

| Task | Status | Notes |
|------|--------|-------|
| Task 2 Gmail OAuth persist | ✅ | `32c0012` — `persist_refreshed_gmail_token` |
| Task 3 `env_prefix` | ✅ | `48a7db6` — `MailboxConfig.env_prefix` + poll |
| Task 4 Poll PARTIAL + runbook | ✅ | `81f0e43` — brief/dashboard PARTIAL hints |
| Task 5 Reply thread headers | ✅ | `94f4e5b` / `29db73a` — `mail_threading` |
| Task 6 Draft diff + regen throttle | ✅ 2026-07-29 | schema v4 `draft_before_regen` / `draft_regenerated_at`; 60s cooldown; case_detail side-by-side |
| Task 1 / FU6 live soak H1 | ⬜ | Human-owned; still required before Fas 3 / Task 7 |
| Task 7 FU9 auto-send wire | ⬜ | Gated — do not start |
| v2.2 B1 live probe_mail + env matrix | ✅ 2026-07-29 | `secret_probes.probe_mail` forces `use_mock` bool; `.env.example` provider matrix; `tests/test_probe_mail.py` |
| v2.2 B2 bulk close | ✅ pre-existing + test 2026-07-29 | `CaseService.bulk_close` + `/cases/bulk-close` + cases.html; `test_bulk_close_closes_open_cases_without_send` |
| v2.2 Fas C FU9 | ⬜ | Same gate as Task 7 — do not start |
| v2.2 Fas A H1–H4 | ⬜ | Human-owned (Oscar + Jonatan) |
| v2.3 Fas 0 land v2.2 | ✅ 2026-07-29 | Commit/push B1 + docs + bulk test + v2.2/v2.3 artifacts |
| v2.3 Fas 0 verify (docs+gates) | ✅ 2026-07-29 | Core 3.0 ideation + AGENTS sync + gate pytest; **Fas 0 exit** |
| v2.3 robustness harden | ✅ 2026-07-29 | Reopen replied threads; persist outbound Message-ID; Gmail OAuth expiry refresh; Telegram/Gmail probe posture; empty-mailbox poll fail in prod |
| v2.3 Fas A soak | ⬜ | Same as Task 1 / FU6 — Oscar — blocker documented in soak checklist |
| v2.3 Fas C FU9 | ⬜ | Gated — do not start (preconditions doc updated; no wire) |
| v2.3 Fas D re-evaluate | ⬜ deferred 2026-07-29 | **Decision log:** park product expansion (V3/GA4/FAQ/NO enable/FU9 wire) until A1 soak + baseline KPI exist; continue SE pilot human-approve loop only |
| Core 3.0 ideation (Azom/azom.no deepen) | ✅ 2026-07-29 | `docs/ideation/2026-07-29-azom-core-30-deepen.md` — no SaaS |
| Core 3.0 Fas 1 soak/baseline | ⬜ | Same as FU6 / v2.3 Fas A — Oscar + Jonatan |
| Core 3.0 Fas 2 azom.no enable | ⬜ | Credentials + Oscar OK first |
| Core 3.0 Fas 3 Path B2 drafts (Q2) | ✅ 2026-07-29 | Richer return/billing templates, SB6 soft-ask, draft prompt 1.2, priority+UI triage; never ★ |
| Core 3.0 Fas 3 FU7 threshold (Q1) | ⬜ | After soak samples — **Q1 blocked_on A3** (no threshold tune without live stick) |
| Core 3.0 Fas 4 FU9 | ⬜ | Hard-gated |

### v2.3 Live-proof agent closeout (2026-07-29)

| Item | Agent status | Notes |
|------|--------------|-------|
| Fas 0 V0.1–V0.4 | ✅ | Core 3.0 docs on branch; AGENTS sync; gate pytest green |
| A1 soak | ⬜ human | Blocker documented — not executed |
| A2 baseline numbers | ⬜ human | Tooling ready; numbers not invented |
| A3 classify stick | ⬜ human | No live samples → no fixture PR |
| A4 cadence ×3 | ⬜ human | Placeholder row in baseline cadence log |
| Q1 FU7 tune | ⬜ blocked_on A3 | No `cases_ai.yaml` threshold change |
| G1 FU9 docs | ✅ | Rollback drill + gate table; still Not wired |
| R1 Fas D | ✅ interim | Park V3/GA4/NO/FU9 wire until KPI |
