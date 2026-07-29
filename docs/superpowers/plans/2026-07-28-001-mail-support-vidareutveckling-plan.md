# Mail & kundsupport vidareutveckling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sequence ops proof, mail robustness, triage friction, and gated auto-send so Azom’s mail→case→approve loop is measurable, reliable, and only then optionally automated.

**Architecture:** Keep existing `MailTransport` / `CaseService` / human `approve_and_send` stack. Add OAuth token persistence, optional per-mailbox env prefixes, triage UX polish, and a single gated auto-send call site — never change the human-approve production default in repo config.

**Tech Stack:** Python 3, SQLite cases.db, Flask dashboard, Telegram bot, SMTP/IMAP/Graph mail, systemd `azom-cases-poll.timer`, pytest.

**Spec:** [`docs/superpowers/specs/2026-07-28-mail-support-roadmap-design.md`](../specs/2026-07-28-mail-support-roadmap-design.md)

## Global Constraints

- Human approve remains the production send path; `config/cases_ai.yaml` keeps `auto_send_enabled: false` in repo.
- `AZOM_AUTO_SEND_KILL=1` must always force deny.
- Never widen suggest-approve allowlist before H3 live sample review.
- No IMAP IDLE, FAQ/KB, V3, GA4, or silent chat LLM send.
- TDD for all code tasks; mock-first (`AZOM_USE_MOCK` / `InMemoryMailTransport`).
- Fas 3 must not start until Fas 0 H1–H3 exit gates are recorded as done.

## File map

| Path | Responsibility |
|------|----------------|
| `skills/ecom_ops/oauth/gmail.py` | Persist refreshed Gmail tokens |
| `skills/ecom_ops/integrations/mail_providers/smtp_imap.py` | Call persist hook after OAuth refresh |
| `skills/ecom_ops/cases/mailboxes.py` | `env_prefix` on `MailboxConfig` |
| `skills/ecom_ops/integrations/mail.py` | `config_from_env(prefix=…)` / provider override |
| `skills/ecom_ops/cases/service.py` | Poll uses per-mailbox config; Fas 3 single auto-send site |
| `skills/ecom_ops/cases/auto_send.py` | Rails + day counter (already exists) |
| `skills/ecom_ops/actions/mail.py` | Reply header parity |
| `config/mailboxes.yaml` | Document `env_prefix` |
| `infrastructure/dashboard/` | PARTIAL poll UX, draft diff (Fas 2) |
| `tests/test_oauth_gmail.py`, `tests/test_mail.py`, `tests/test_cases.py`, `tests/test_auto_send_rails.py` | Coverage |

---

### Task 1: Fas 0 — Live soak + baseline + cadence (ops)

**Files:**
- Modify (fill): `docs/ideation/baseline-capture.md`
- Reference: `docs/solutions/2026-07-16-live-soak-checklist.md`
- Optional tune: `config/cases_ai.yaml`, `tests/fixtures/support_classify/`

**Interfaces:**
- Consumes: prod host `/opt/azom-agent`, `ecom_ops status`, `cases poll`, dashboard `/cases`
- Produces: signed soak notes; baseline number; H3 sample log

- [ ] **Step 1: Execute live soak (H1)**

On prod (Oscar): run every checkbox in `docs/solutions/2026-07-16-live-soak-checklist.md`. Record date, host, and any PARTIAL poll failures in the checklist’s notes section (or a short `docs/solutions/2026-07-28-live-soak-results.md`).

Expected: `/health` OK; poll creates/skips without silent all-fail; Jonatan completes ≥1 approve on dashboard and Telegram.

- [ ] **Step 2: Fill baseline (H2)**

Edit `docs/ideation/baseline-capture.md` with either:

- weekly support hours before Azom, **or**
- proxy: `python -m ecom_ops kpis` median `time_to_approve_sec` × weekly volume after first live week

- [ ] **Step 3: Classify stick sample (H3)**

Export 20–50 live case subjects/bodies (redact PII). Run `python -m ecom_ops classify-eval`. Add failing never-list cases as fixtures under `tests/fixtures/support_classify/`. Adjust **only** `suggest_approve_min_confidence` if precision fails — do not add categories to `suggest_approve_categories`.

Run: `pytest tests/test_support_classify_fixtures.py tests/test_suggest_approve.py -v`  
Expected: PASS

- [ ] **Step 4: Cadence (H4)**

Schedule three weekly 15–30 min syncs (Jonatan + Oscar). After third, mark Fas 0 human-DoD complete in `docs/DEVELOPMENT_PLAN_FINISH.md` execution log.

- [ ] **Step 5: Gate check before Task 2**

Confirm H1 done. If soak found OAuth expiry / shared-mailbox auth failures, prioritize Task 2–3. If soak blocked, do **not** start Fas 3.

---

### Task 2: Persist Gmail OAuth access token after refresh (Fas 1 P0)

**Files:**
- Modify: `skills/ecom_ops/oauth/gmail.py`
- Modify: `skills/ecom_ops/integrations/mail_providers/smtp_imap.py`
- Test: `tests/test_oauth_gmail.py`

**Interfaces:**
- Consumes: `GmailOAuthStore.save_tokens`, `GmailTokenBundle`
- Produces: `persist_refreshed_gmail_token(access_token, expires_in, refresh_token=None) -> None`

- [ ] **Step 1: Write the failing test**

```python
def test_smtp_imap_refresh_persists_gmail_token(tmp_path, monkeypatch):
    from ecom_ops.oauth.gmail import GmailOAuthStore, GmailTokenBundle
    from ecom_ops.integrations.mail_providers.models import MailConfig, MailProvider
    from ecom_ops.integrations.mail_providers.smtp_imap import SmtpImapTransport

    store = GmailOAuthStore(data_dir=tmp_path)
    store.save_tokens(
        GmailTokenBundle(
            access_token="old-access",
            refresh_token="refresh-1",
            expires_at=0,
            token_type="Bearer",
            scope="mail",
        )
    )
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    cfg = MailConfig(
        provider=MailProvider.GMAIL,
        username="a@gmail.com",
        password="",
        from_addr="a@gmail.com",
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_use_ssl=True,
        oauth_refresh_token="refresh-1",
        oauth_client_id="cid",
        oauth_client_secret="sec",
        oauth_access_token="",
    )

    class _Resp:
        status_code = 200
        def json(self):
            return {"access_token": "new-access", "expires_in": 3600}

    monkeypatch.setattr(
        "ecom_ops.integrations.mail_providers.smtp_imap.requests.post",
        lambda *a, **k: _Resp(),
    )

    transport = SmtpImapTransport(cfg)
    assert transport._ensure_oauth_token() == "new-access"
    loaded = GmailOAuthStore(data_dir=tmp_path).load_tokens()
    assert loaded is not None
    assert loaded.access_token == "new-access"
    assert loaded.refresh_token == "refresh-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_oauth_gmail.py::test_smtp_imap_refresh_persists_gmail_token -v`  
Expected: FAIL (token still old or method missing)

- [ ] **Step 3: Implement persist helper + call from refresh**

In `gmail.py` add:

```python
def persist_refreshed_gmail_token(
    *,
    access_token: str,
    expires_in: float | None = None,
    refresh_token: str | None = None,
    data_dir: Path | None = None,
) -> None:
    store = GmailOAuthStore(data_dir=data_dir)
    existing = store.load_tokens()
    if not existing and not refresh_token:
        return
    store.save_tokens(
        GmailTokenBundle(
            access_token=access_token,
            refresh_token=refresh_token
            or (existing.refresh_token if existing else ""),
            expires_at=(time.time() + float(expires_in)) if expires_in else None,
            token_type=(existing.token_type if existing else "Bearer"),
            scope=(existing.scope if existing else GMAIL_SCOPES),
            email=(existing.email if existing else None),
        )
    )
```

In `SmtpImapTransport._ensure_oauth_token`, after setting `self._access_token` from refresh response, if `self.config.provider == MailProvider.GMAIL`:

```python
from ecom_ops.oauth.gmail import persist_refreshed_gmail_token
persist_refreshed_gmail_token(
    access_token=self._access_token,
    expires_in=payload.get("expires_in"),
    refresh_token=payload.get("refresh_token") or None,
)
```

Best-effort: wrap persist in try/except log — never fail the send/fetch solely because disk write failed.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_oauth_gmail.py tests/test_mail.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when user requests)

```bash
git add skills/ecom_ops/oauth/gmail.py skills/ecom_ops/integrations/mail_providers/smtp_imap.py tests/test_oauth_gmail.py
git commit -m "fix: persist Gmail OAuth access token after SMTP/IMAP refresh"
```

---

### Task 3: Per-mailbox env_prefix credentials (Fas 1 P1)

**Files:**
- Modify: `skills/ecom_ops/cases/mailboxes.py`
- Modify: `skills/ecom_ops/integrations/mail.py` (`config_from_env`)
- Modify: `skills/ecom_ops/cases/service.py` (`poll` client construction)
- Modify: `config/mailboxes.yaml` (document example)
- Modify: `.env.example` (document `MAIL_SE_*` pattern)
- Test: `tests/test_cases.py` (or new `tests/test_mailbox_env_prefix.py`)

**Interfaces:**
- Consumes: `MailboxConfig` fields from YAML
- Produces: `MailboxConfig.env_prefix: str | None`; `config_from_env(provider=..., env_prefix=...) -> MailConfig`

- [ ] **Step 1: Write the failing test**

```python
def test_config_from_env_uses_mailbox_prefix(monkeypatch):
    from ecom_ops.integrations.mail import config_from_env, MailProvider

    monkeypatch.setenv("MAIL_PROVIDER", "generic_imap")
    monkeypatch.setenv("MAIL_USERNAME", "shared@azom.se")
    monkeypatch.setenv("MAIL_SE_USERNAME", "se@azom.se")
    monkeypatch.setenv("MAIL_SE_PASSWORD", "se-secret")
    monkeypatch.setenv("MAIL_SE_FROM", "se@azom.se")

    cfg = config_from_env(env_prefix="MAIL_SE_")
    assert cfg.username == "se@azom.se"
    assert cfg.password == "se-secret"
    assert cfg.from_addr == "se@azom.se"
```

```python
def test_poll_passes_mailbox_env_prefix(tmp_path, monkeypatch):
    # Arrange mailbox YAML with env_prefix MAIL_SE_
    # Patch client_from_env to capture kwargs
    # Call CaseService.poll
    # Assert client_from_env called with env_prefix="MAIL_SE_"
    ...
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_mailbox_env_prefix.py -v`

- [ ] **Step 3: Implement**

1. Add `env_prefix: str | None = None` to `MailboxConfig`; parse from YAML.  
2. In `config_from_env`, if `env_prefix` set, read `f"{prefix}USERNAME"` etc., falling back to unprefixed `MAIL_*` only for hosts/ports if prefix-specific missing (document the fallback rule in `.env.example`).  
3. In `CaseService.poll`, call `client_from_env(provider=mb.provider, env_prefix=mb.env_prefix, use_mock=...)`.

Uncomment/document in `config/mailboxes.yaml`:

```yaml
# env_prefix: MAIL_SE_   # resolves MAIL_SE_USERNAME, MAIL_SE_PASSWORD, ...
```

- [ ] **Step 4: Run related tests**

Run: `pytest tests/test_mailbox_env_prefix.py tests/test_cases.py tests/test_cases_v2.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when user requests)

```bash
git commit -m "feat: per-mailbox mail credentials via env_prefix"
```

---

### Task 4: Poll PARTIAL visibility + runbook links (Fas 1 P0)

**Files:**
- Modify: `infrastructure/dashboard/templates/` (overview / cases flash)
- Modify: `bin/daily-brief-azom.sh` or brief builder used by `/brief`
- Reference: `docs/runbooks/mail-poll-stuck.md`, `docs/runbooks/gmail-oauth-revoked.md`
- Test: existing dashboard/ops tests extended

**Interfaces:**
- Consumes: `last_case_poll.json` via `ops_status`
- Produces: brief/dashboard line distinguishing PARTIAL vs ALL-fail with runbook URL

- [ ] **Step 1: Characterization test**

Assert status/brief payload includes `poll_partial: true` (or equivalent) when `last_case_poll` has errors but `ok=True`.

- [ ] **Step 2: Implement UI/brief copy**

Swedish short copy: “Poll delvis misslyckad — se mail-poll-stuck runbook” linking to `docs/runbooks/mail-poll-stuck.md` (or dashboard help path already used for runbooks).

- [ ] **Step 3: pytest for status JSON / template fragment**

Run: `pytest tests/test_ops_readiness_smoke.py tests/test_dashboard.py -v`

---

### Task 5: MailService.reply thread-header parity (Fas 1 P2)

**Files:**
- Modify: `skills/ecom_ops/actions/mail.py`
- Test: `tests/test_mail.py`

- [ ] **Step 1: Failing test** — `MailService.reply` with mock transport sets `In-Reply-To` and `References` on outbound message when `original_uid` resolves a fetched message with those headers (or when caller passes `in_reply_to` / `references_header`).

- [ ] **Step 2: Implement** — reuse the same header assembly pattern as `CaseService._outbound_thread_headers` or call shared helper extracted to `skills/ecom_ops/integrations/mail_providers/models.py` / small `mail_threading.py` if duplication is painful.

- [ ] **Step 3:** `pytest tests/test_mail.py -v` → PASS

---

### Task 6: Triage friction — draft diff + regenerate throttle (Fas 2)

**Files:**
- Modify: `infrastructure/dashboard/templates/case_detail.html`
- Modify: `infrastructure/dashboard/app.py` (pass previous draft if stored)
- Modify: `skills/ecom_ops/cases/service.py` or bot handlers for throttle
- Test: `tests/test_case_regenerate.py`, dashboard test

**Gate:** Fas 0 H1 complete.

- [ ] **Step 1:** Store `draft_before_regen` (or compare last saved vs current) and show a simple side-by-side or unified diff on `case_detail` after regenerate — text only, no new JS framework.

- [ ] **Step 2:** Enforce regenerate cooldown (e.g. 60s per case) returning clear Swedish error; unit-test deny within window.

- [ ] **Step 3:** `pytest tests/test_case_regenerate.py -v` → PASS

---

### Task 7: FU9 auto-send wire — gated (Fas 3)

**Files:**
- Modify: `skills/ecom_ops/cases/service.py` (one call site post-ingest)
- Modify: `skills/ecom_ops/cases/auto_send.py` (day counter integration)
- Test: `tests/test_auto_send_rails.py` (replace/extend “poll does not call” with “poll calls eligibility + send only when enabled”)
- Config overlay: data-dir copy of `cases_ai.yaml` — **not** repo default flip

**Gate checklist (all required):**

1. Task 1 H1–H3 done  
2. ≥2 weeks human approve without serious bad send  
3. Oscar written enable for experiment window  
4. Kill-switch drill practiced  

- [ ] **Step 1: Keep deny-by-default test**

```python
def test_poll_does_not_auto_send_when_disabled(monkeypatch, ...):
    # auto_send_enabled false → approve_and_send never called from poll
    ...
```

- [ ] **Step 2: Enable-path test with mock**

```python
def test_poll_auto_sends_eligible_order_status_when_enabled(monkeypatch, ...):
    # Overlay config auto_send_enabled=True, kill switch off
    # Eligible case → one MailService.send / approve path
    # Telemetry action == "case_auto_sent"
    # Ineligible (billing/abuse/no order_id) → no send
    ...
```

- [ ] **Step 3: Implement single call site**

After successful ingest + draft for a **new** case only, call:

```python
if self.evaluate_auto_send_eligibility(case, auto_sends_today=counter.count):
    result = self.approve_and_send(case.id, actor="agent")  # or dedicated auto actor with CASE_REPLY
    if result.ok:
        counter.increment()
        # telemetry case_auto_sent
```

Use actor that has `CASE_REPLY` (operator/`agent`). Never send from viewer path implicitly.

- [ ] **Step 4: Rollback drill doc**

Add 5-line section to `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`: disable overlay + `AZOM_AUTO_SEND_KILL=1` + restart poll timer.

- [ ] **Step 5:** `pytest tests/test_auto_send_rails.py -v` → PASS with both disabled and enabled scenarios.

---

### Task 8: Fas 4 — Re-evaluate (decision, no code)

**Files:**
- Update: `docs/DEVELOPMENT_PLAN_FINISH.md` § Fas 4 notes or new short decision log

- [ ] **Step 1:** After ≥2 weeks KPI data, Oscar + Jonatan decide: stop / continue narrow auto-send / park engagement-GA4 / park V3.

- [ ] **Step 2:** Record decision in finish plan execution log. Do not start V3/GA4 from this plan.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Fas 0 H1–H4 | Task 1 |
| OAuth token persist | Task 2 |
| Per-mailbox credentials | Task 3 |
| Poll PARTIAL clarity | Task 4 |
| Reply header parity | Task 5 |
| Draft diff + regen throttle | Task 6 |
| FU9 gated wire | Task 7 |
| Fas 4 re-evaluate | Task 8 |
| Non-goals (IDLE/FAQ/V3/GA4/default-on) | Global Constraints |

No TBD placeholders in task steps. Fas 3 remains hard-gated.

## Execution order

```text
Task 1 (ops) → Task 2 → Task 3 → Task 4 → Task 5
                ↘ Task 6 (after H1)
Task 7 only after Task 1 H1–H3 + Oscar written enable
Task 8 last
```
