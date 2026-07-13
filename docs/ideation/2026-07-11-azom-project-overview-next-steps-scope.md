# Azom — project overview & next-steps scope

**Date:** 2026-07-11 (revised same day — decisions locked)  
**Package:** 2.0.0  
**Purpose:** Decision-ready overview with **locked** next-path choices.  
**Related:** Detailed item backlog → [`2026-07-11-azom-status-improvement-backlog.md`](./2026-07-11-azom-status-improvement-backlog.md)  
**Implementation plan (Path B):** [`../superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`](../superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md)

---

## Decisions locked

| # | Question | Locked answer |
|---|----------|---------------|
| 1 | **Live VPS?** | **Live** — real Woo + mailbox already |
| 2 | **Support-time baseline?** | **Not available yet** — user will provide in days / when they can get in touch with Jonatan. **Do not block capability work** on baseline. Capture baseline as a **parallel** item. |
| 3 | **Primary next outcome?** | **Ship more capability** before measuring (not “prove 50% time cut first”) |
| 4 | **LLM risk appetite?** | **High** — open to going beyond draft-only; **suggest-approve** and later **auto-send experiments** are in play. Safety rails / RBAC / audit remain **required** (not zero-risk). |
| 5 | **“Hög engagement” next 4–6 weeks?** | **Parked** until support-time moves |
| 6 | **Pilot owner day-to-day?** | **Both** — Jonatan (approve) + Oscar (secrets/uptime), with a **shared cadence** |

These six answers supersede the open questions in the earlier draft of this doc. Residual implementation questions are listed in §8.

---

## 1. What Azom is now

**AzomOps-Agent** is a single-tenant customer ops agent for WooCommerce: order/product/support/mail/SSH automation, with a password-protected dashboard (Jonatan + Oscar), Gmail OAuth, Telegram bot, and a Cases 2.0 mail→ärende loop (draft → human approve → send). It runs on a small Hetzner VPS (Ubuntu 24/26, CX22/CPX21) with one-shot install, systemd, and Docker (`azom-agent:2.0`). V3 multi-tenant SaaS is explicitly deferred.

### Capability map

| Layer | Status | What’s in |
|-------|--------|-----------|
| **V1 core** | Done | order-status, product-desc, support classify/draft, SSH allowlist, mail (Gmail/Outlook/Graph/IMAP/POP3/SMTP), RBAC + escalation |
| **V2.0** | Done | Dashboard onboarding/settings/Oscar secrets, Gmail OAuth, OpenClaw-style Telegram bot, auto-install, CLI `version`/`status` |
| **Cases 2.0** | MVP done | SQLite cases, 5‑min poll, threading headers, order-enriched drafts, dashboard `/cases`, Telegram `/cases`, human approve required |
| **Hardening wave** | Shipped on `main` | Prod-path fixes, CSRF + salted passwords, Telegram actor map, OpenRouter drafts + cap, case KPIs, schema migrate, mail split, smoke/readiness, CI ruff + cov≥65%, product-desc LLM |
| **Path B Cases/AI** | Shipped on `main` | suggest-approve + confidence, richer order-context drafts, auto-send rails default-off + kill-switch, Telegram hybrid tool prefetch + NL confirm |
| **Docs map (later)** | Current | `docs/SYSTEM_OVERVIEW.md`, `docs/CASES.md`, `docs/TELEGRAM_OPENCLAW.md`, expanded `SOUL.md` |

Evidence: `AGENTS.md`, `README.md`, `docs/V2_RELEASE.md`, `docs/SYSTEM_OVERVIEW.md`; commits through Path B + hybrid dialog vNext (`9e4be88`, `0c778c6`) and earlier P6–P10.

---

## 2. Who it’s for / success metrics

| Actor | Role |
|-------|------|
| **Jonatan** | Viewer + mail/SSH read, non-secret settings, **case reply approve/send** |
| **Oscar** | full_admin, escalation target, secrets UI, connection probes |
| **Agent automation** | Operator: order/product/support/mail/SSH read/case poll |

**Stated goals** (`AGENTS.md`):

1. **3 months:** 50% less support time + high engagement  
2. **Onboarding:** Telegram + password-protected dashboard — **met (V2)**  
3. **Budget:** $100 OpenRouter cap (`config/limits.yaml`)

**Near-term priority (locked):** Optimize for **capability that reduces approve/send friction** (Cases/AI). Measurement and engagement are **not** the primary 4–6 week program.

**Implied operating model:** Single dedicated instance per customer (Azom), not a shared SaaS control plane yet. Instance is **already live**.

---

## 3. Current state assessment

### Strengths (evidence-based)

- **Full ops surface exists end-to-end** — CLI, dashboard, bot, systemd timers; V1+V2 acceptance tables in `docs/ANALYSIS_AND_DEVELOPMENT_PLAN.md` are checked off.
- **Cases loop is real** — poll → store → draft → approve/send with threading; specs marked implemented (`docs/superpowers/specs/2026-07-11-cases-v2-design.md`).
- **Deploy story is mature** for a pilot — install scripts, Docker overlay docs, `/health` readiness tied to last cases-poll age.
- **Live already** — real Woo + mailbox (locked); not waiting on first production cutover.
- **Quality bar raised recently** — ~22 test modules; CI runs ruff, pytest with coverage fail-under 65, mock smoke; solutions doc for prod-path hardening.
- **LLM is no longer a stub** — `skills/ecom_ops/llm.py` calls OpenRouter with cost estimate + template fallback for support/cases and product-desc.

### Maturity snapshot

| Dimension | Level | Notes |
|-----------|-------|-------|
| Feature completeness (pilot) | High | Enough to run a dedicated customer agent |
| Production credibility | Medium | Live in use; CI remains mock-only (`AZOM_USE_MOCK=1`) |
| AI quality vs 50% goal | Medium | LLM drafts + order context + **suggest-approve shipped**; classify still largely keyword/heuristic hybrid; auto-send default off |
| Measurement of support-time goal | Early | Telemetry records KPIs — **baseline not yet captured**; parallel track only |
| Security posture | Improved, not “done” | Werkzeug hashes + CSRF + sessions; still Basic Auth (no SSO); CDN assets remote |
| Platform / V3 | Not started | Correctly out of scope until core support loop improves further |

### Known gaps (post Path B ship)

Decision-relevant residuals — not a full backlog:

1. **Classify depth** — abuse gate + confidence path exists; room to deepen LLM classify reliability vs pure keywords.
2. **Human-in-the-loop remains default** — suggest-approve reduces friction; **auto-send live sender not wired** (rails only, Oscar experiment later).
3. **Triage UX polish** — more regenerate/bulk/edit-distance dashboards still optional.
4. **Baseline not captured** — do not block capability work; capture when Jonatan is reachable.
5. **Ops leftovers** — budget near-cap alerts, poll/mail failure visibility hardening as live needs.
6. **Engagement / GA4** — researched (`ga-ads-api-feasibility.json`); **parked**.
7. **Docs** — prefer `docs/SYSTEM_OVERVIEW.md` + this file over older backlog rows that still call OpenRouter a stub.

---

## 4. Decision surface (historical frame)

Earlier draft framed four options. With decisions locked, the primary axis for the next 4–6 weeks is **Deepen cases/AI** (B), not prove-value-first (A).

```text
                    Prove value in production   ← thin parallel only
                              ▲
                              │
         Deepen AI quality ◄──┼──► Ops credibility / harden  ← minimal live hygiene
                              │
                              ▼
                    Expand surface (GA4 / V3)   ← parked
```

| Strategic option | Role under locked decisions |
|------------------|-----------------------------|
| **A. Pilot + measure** | **Demoted** — thin parallel track (baseline when Jonatan available; light ops credibility for live safety) |
| **B. Deepen cases/AI** | **Primary** — ship capability; high LLM appetite (suggest-approve → scoped auto-send experiments) |
| **C. Platform harden** | **Companion slice only** — Oscar’s minimum live-ops hygiene, not a full harden program |
| **D. Expand (engagement / V3)** | **Parked** until support-time moves |

**Still deferred unless forced:** Multi-tenant control plane, IMAP IDLE, FAQ/KB, Outlook browser OAuth, **unscoped** auto-send without rails.

---

## 5. Revised recommended path

### Primary recommendation: **Option B — Cases & AI quality (high LLM appetite)**

Ship more capability that makes Jonatan’s approve/send path faster and safer, before investing in a measurement-first program. The instance is already live; drafts exist; the bottleneck is quality + friction + cautious automation, not “get to production.”

**Why this over A**

- Locked primary outcome is **capability**, not prove-50%-first.
- Live Woo + mailbox removes the classic A precondition (“unblock pilot”).
- High LLM appetite explicitly opens **suggest-approve** and later **auto-send experiments** — those live in B’s product surface, with mandatory safety rails.
- Engagement is parked; heavy measurement without baseline contact would stall work the user does not want blocked.
- A thin measurement/ops track still preserves AGENTS.md 3‑month honesty without owning the sprint.

### Parallel thin track (do not expand into a program)

| Track | What | Owner cue |
|-------|------|-----------|
| **Baseline capture** | When Jonatan is contactable: hours/week or agreed proxy; note start date | User + Jonatan |
| **Live-ops hygiene** | Only what Oscar needs so live mail/Woo/poll don’t silently fail (smoke/alert if broken, budget near $100) | Oscar |

### Explicitly out / parked (next 4–6 weeks)

- GA4 / Ads / “hög engagement” work
- Heavy measurement-first KPI program (daily brief redesign as a *primary* project, multi-week baseline rituals before features)
- V3 / multi-tenant
- Full Option C harden sweep (CDN pin, Dependabot, SSO, log rotation as a *program*)

---

## 6. Option scopes under locked constraints (4–6 weeks)

### Option B — Cases & AI quality *(primary — do this)*

| | |
|--|--|
| **Goal** | Raise draft/triage quality and reduce approve/send friction; carefully introduce higher-automation modes under RBAC + audit. |
| **In scope** | Hybrid classify (keywords for abuse/legal + LLM for the rest); stronger order-context drafts; **suggest-approve** UX; triage UX that cuts Jonatan friction (diff, order panel, regenerate, confidence); carefully scoped **auto-send experiments** only with explicit guardrails (case-type allowlist, confidence thresholds, audit log, easy kill-switch, RBAC). |
| **Out of scope** | Engagement/GA4; measurement-first program; V3; FAQ/KB as a large build; unscoped “send everything.” |
| **Risks** | Budget burn; bad auto-send; false confidence on abuse/legal. Mitigate with rails, allowlists, and human default for high-risk types. |
| **Success signal** | Jonatan spends less effort per routine case (qualitative + edit-distance/time-to-approve when available); suggest-approve used on safe types; auto-send experiment (if run) has zero silent bad sends and clear audit trail. |

### Option A — Pilot & measurement *(thin parallel only)*

| | |
|--|--|
| **Goal** | Capture baseline when possible; keep light credibility so live ops stay trustworthy. |
| **In scope** | Baseline conversation with Jonatan (async OK); optional light KPI glance in brief/overview **without** blocking B; fix live blockers Oscar hits. |
| **Out of scope** | Making “prove 50%” the gate for shipping B features. |
| **Success signal** | Baseline number (or proxy) recorded when contactable; no silent poll/mail failures. |

### Option C — Ops credibility *(minimal companion)*

| | |
|--|--|
| **Goal** | Oscar can keep the live instance upright without a full harden program. |
| **In scope** | Broken-smoke/poll visibility; budget near-cap awareness; secrets/probe fixes as needed. |
| **Out of scope** | Broad CDN/SSO/Dependabot/log-rotation program. |

### Option D — Engagement *(parked)*

No work in the next 4–6 weeks unless support-time trajectory clearly moves and stakeholders reopen this.

---

## 7. Shared Jonatan + Oscar cadence

Lightweight, practical — avoid process theater.

**Default: weekly 15–30 min sync** (or async checklist the same day if a call is hard).

| Agenda item | Owner |
|-------------|--------|
| Cases stuck / bad drafts / near-misses this week | Jonatan |
| Approve friction (what slowed send) | Jonatan |
| Secrets, poll/health, budget, uptime surprises | Oscar |
| Which case types feel safe for suggest-approve / auto-send trial | Both |
| Kill-switch or roll back if an experiment misfires | Oscar + Jonatan |

**Async fallback checklist** (Slack/mail/Telegram — same topics, yes/no + one example each).

---

## 8. Open residual questions (implementation scoping)

Decisions above are locked. These still matter before / during `/ce-plan` for B:

1. **Which mailbox / Woo flows hurt most today?** (order status? returns? shipping? payment?) — drives classify + draft priority.
2. **Which case types are first-safe for suggest-approve?** (e.g. routine order-status with high order-match confidence vs never abuse/legal/refund disputes).
3. **Auto-send experiment guardrails:** confidence threshold, allowlist types, max sends/day, mandatory audit fields, who can enable (Oscar-only?), kill-switch location.
4. **Where does Jonatan approve most?** Dashboard vs Telegram — prioritize UX there first.
5. **OpenRouter budget headroom** under higher LLM use (classify + regenerate + suggest) vs $100 cap — soft alarm threshold?
6. **Baseline proxy** when Jonatan is reachable: hours/week vs “cases/week × median approve time” from telemetry?

---

## 9. Next artifact

**Implementation plan (Path B):** [`docs/superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`](../superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md) — hybrid classify, suggest-approve, auto-send rails (default off), triage UX; first slice **U1**.

Do **not** open a parallel full plan for A, C, or D unless a live incident forces a C slice.

---

## Appendix — evidence sources

| Source | Used for |
|--------|----------|
| `AGENTS.md`, `README.md`, `pyproject.toml` 2.0.0 | Product identity, roles, goals, version |
| `docs/V2_RELEASE.md`, `docs/ANALYSIS_AND_DEVELOPMENT_PLAN.md` | Acceptance / out-of-scope V3 |
| `docs/superpowers/specs/*cases*`, `*dashboard-ops-polish*` | Cases & dashboard locked decisions |
| `docs/ideation/2026-07-11-azom-status-improvement-backlog.md` | Prioritized improvement inventory (partially executed) |
| `docs/solutions/2026-07-11-mail-thread-poll-llm-drafts.md` | What hardening already shipped |
| `docs/DOCKER_CONFIG_OVERLAY.md` | Deploy/ops maturity |
| Git log `be03684`→`2e62309` | Delivery arc V1 → V2 → Cases → polish → P0–P10 |
| `skills/ecom_ops/{llm,smoke,ops_status,cases,actions/support}.py` | LLM real, classify keywords, smoke/readiness |
| `.github/workflows/ci.yml` | Mock CI + ruff + cov≥65 + mock smoke |
| `bin/daily-brief-azom.sh` | Brief lacks case KPI loop today |
| `ga-ads-api-feasibility.json` | Engagement track is research-only (parked) |

---

*Decisions locked 2026-07-11. Path B plan: `docs/superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`.*
