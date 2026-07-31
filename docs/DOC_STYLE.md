# Documentation style (for humans and weaker coding models)

**Purpose:** Rules for writing and maintaining living docs in this repository.  
**Audience:** Authors of documentation and coding agents that edit docs.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md) (what is true now), then the doc you are changing.

## Language split (mandatory)

| Kind of doc | Language | Examples |
|-------------|----------|----------|
| Operator / pilot / runbooks | **Swedish** | `PILOT_OPS.md`, `CASES.md`, `TELEGRAM_OPENCLAW.md`, `MAIL_PROVIDERS.md`, `runbooks/*` |
| Agent / developer / architecture | **English** | `AGENTS.md`, `SYSTEM_OVERVIEW.md`, `DEVELOPER_GUIDE.md`, `CLI_REFERENCE.md`, `WOO_WORDPRESS.md`, this file, `CURRENT_STATE.md` |
| Customer-facing agent voice | **Swedish** | `SOUL.md` (ops chat and draft language rules) |

Keep technical identifiers in English exactly as in code: env names, CLI flags, file paths, status enums (`open`, `escalated`).

## Required sections on every living doc

Every living markdown file under `docs/` (and root `AGENTS.md` / `SOUL.md` / skill card) must open with:

1. **Purpose** — one short paragraph: what this file is for.
2. **Audience** — who should read it (operator, Oscar, coding agent, developer).
3. **Read this first** — links to prerequisite living docs only (never to deleted history).

Add a **Glossary** when the doc introduces domain terms (suggest-approve, fail-closed, Path B, `AZOM_DATA_DIR`, …). Define each term the first time a weak model might misread it.

## How to write for weaker models

Do:

- Use numbered steps for procedures. State working directory, required env (`AZOM_USE_MOCK=1` vs `0`), and **expected result** after each command block.
- Use **Do / Do not** lists for safety-critical behavior (mail send, auto-send, secrets, allowlists).
- Give one **good example** and one **bad example** when a rule is easy to get wrong.
- Cite paths from **repository root** (example: `skills/ecom_ops/cases/service.py`, `config/cases_ai.yaml`).
- Name env vars exactly as in `.env.example`.
- State ownership: who may change what (Jonatan / Oscar / agent).
- Mark status explicitly: shipped code vs human ops gate vs deferred.

Do not:

- Link to deleted history (`docs/superpowers/`, `docs/solutions/`, `docs/ideation/`, finish plans).
- Say “see backlog” or “Active sprint” without a living-doc target.
- Assume the reader knows WooCommerce, Microsoft Graph, systemd, or RBAC — define briefly on first use.
- Claim live soak is done, or that auto-send is wired, unless `CURRENT_STATE.md` says so.
- Invent CLI flags or dashboard routes; verify against `skills/ecom_ops/cli.py` and `infrastructure/dashboard/`.

## Command blocks

Prefer this shape:

```bash
# cwd: repository root (dev) or /opt/azom-agent (prod)
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock cases list --status open,escalated
# expect: JSON with ok true and a list of cases (may be empty)
```

## Source of truth order

When documents disagree:

1. [`CURRENT_STATE.md`](CURRENT_STATE.md)
2. [`AGENTS.md`](../AGENTS.md)
3. Code + tests
4. Older memories or chat history (ignore if they conflict)

## Index maintenance

When you add, rename, or delete a living doc:

1. Update [`docs/README.md`](README.md) coverage matrix and tables.
2. Update root [`README.md`](../README.md) documentation index if the file is an entry point.
3. Update [`AGENTS.md`](../AGENTS.md) “Read order” if agents must load the new file.
4. Grep the repo for the old path and fix broken links.

## What we do not maintain here

- No MkDocs / Sphinx site in this repository wave.
- No `docs/archive/` folder — obsolete history is deleted after facts are absorbed into living docs.
- Python docstrings are optional extras; living docs are the operator/agent contract.
