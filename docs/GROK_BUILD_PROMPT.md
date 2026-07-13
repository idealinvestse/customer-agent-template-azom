# Optimal Grok Build prompt — fetch + build Azom ecom-ops v2.0

Copy the block below into **Grok Build** (or any coding agent) to clone, install, verify, and run the project end-to-end.

---

## Prompt (copy everything between the lines)

```
You are Grok Build. Your job is to FETCH and BUILD the Azom customer agent project to a working, verified state.

## Source of truth
- GitHub (public): https://github.com/idealinvestse/customer-agent-template-azom
- Branch: main
- Package version: 2.0.0
- Product: AzomOps-Agent (WooCommerce e-com ops agent + dashboard + mail + SSH + Telegram)

## Goal
1. Clone (or pull) the repository.
2. Create a clean Python environment and install the package editable.
3. Run the full test suite and smoke commands.
4. Prove CLI + mock integrations work without external secrets.
5. Report exact commands run, exit codes, and any failures with fixes applied.

Do NOT invent missing features. Implement only what is needed to make install/build/verify succeed. Prefer reading the repo (README.md, docs/V2_RELEASE.md, pyproject.toml, AGENTS.md) over guessing.

## Environment assumptions
- OS: Linux preferred (Ubuntu 24/26) or Windows with Python 3.11+
- Python: >=3.11 (3.12 recommended)
- Network: yes (pip + git). No WooCommerce/SSH/mail credentials required for mock mode.
- Shell: bash or PowerShell — use OS-appropriate commands.

## Step-by-step build plan (execute in order)

### 1) Fetch
```bash
git clone https://github.com/idealinvestse/customer-agent-template-azom.git
cd customer-agent-template-azom
git checkout main
git pull --ff-only origin main
git log -1 --oneline
```

If the repo is already cloned, only `cd` + `git pull --ff-only origin main`.

### 2) Isolated Python env
```bash
python3 -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1

python -m pip install -U pip wheel setuptools
python -m pip install -r requirements.txt
python -m pip install -e .
```

### 3) Runtime env (mock — no secrets)
```bash
export AZOM_USE_MOCK=1
export AZOM_CONFIG_DIR="$(pwd)/config"
export AZOM_DATA_DIR="$(pwd)/.azom-data-build"
export PYTHONPATH="$(pwd)/skills${PYTHONPATH:+:$PYTHONPATH}"
# Windows PowerShell equivalents:
# $env:AZOM_USE_MOCK="1"
# $env:AZOM_CONFIG_DIR="$PWD\config"
# $env:AZOM_DATA_DIR="$PWD\.azom-data-build"
# $env:PYTHONPATH="$PWD\skills"
```

### 4) Verify package import + version
```bash
python -c "import ecom_ops; print(ecom_ops.__version__)"
python -m ecom_ops version
python -m ecom_ops status
```
Expect version **2.0.0** and status.ok true (with mock).

### 5) Full automated tests
```bash
pytest --tb=short -q
```
All tests must pass. Fix only real breakage (import path, missing dep, syntax). Do not weaken assertions.

### 6) Smoke suite (mock ops — no network integrations)
```bash
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock product-desc --product-id 501 --language sv
python -m ecom_ops support --message "Var är order 1001?"
python -m ecom_ops --mock ssh --command uptime
python -m ecom_ops --mock ssh --command "rm -rf /"   # must escalate, not execute
python -m ecom_ops --mock mail send --to customer@example.com --subject "Build" --body "OK"
python -m ecom_ops --mock mail fetch
python -m ecom_ops --mock cases poll --limit 5
python -m ecom_ops --mock cases list --status open
```

Optional spinup script (bash):
```bash
bash tests/test_spinup.sh
```

### 7) Dashboard import check (no long-running server required)
```bash
python -c "import sys; sys.path.insert(0,'infrastructure/dashboard'); import app; print('dashboard ok', app.app.name)"
```
If you start the dashboard for a live check:
```bash
export AZOM_USE_MOCK=1 DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=8080
# credentials in mock: jonatan/jonatan or oscar/oscar
# run briefly then stop after /health returns 200
python infrastructure/dashboard/app.py
# elsewhere: curl -sf http://127.0.0.1:8080/health
```

### 8) Optional production-style VPS install (ONLY if on Ubuntu 24/26 as root and user asked)
```bash
sudo bash bin/install-ubuntu26.sh --mock --no-start --no-ufw --no-docker
```
Default path for full prod install (starts services, enables UFW):
```bash
sudo bash bin/install.sh
```
Read docs/AUTO_INSTALL.md first.

## Architecture constraints (do not violate)
- Package root: `skills/ecom_ops` (import name `ecom_ops`)
- Config: `config/*.yaml` via `AZOM_CONFIG_DIR`
- Secrets: environment / `.env` only — never commit secrets
- RBAC: Jonatan=viewer, Oscar=full_admin, agent=operator
- SSH: allowlist only; destructive commands escalate to Oscar
- Mail: mock transport when AZOM_USE_MOCK=1
- Cases: human approve required for send; auto-send default off
- Dashboard: Flask under `infrastructure/dashboard/`
- Escalations/telemetry: JSONL under AZOM_DATA_DIR
- Identity docs: `SOUL.md`, `docs/SYSTEM_OVERVIEW.md` (read-only; do not invent product scope)

## Acceptance checklist (must all be true)
- [ ] `git log -1` shows recent main commit
- [ ] `ecom_ops.__version__ == "2.0.0"`
- [ ] `pytest` exit code 0
- [ ] Mock order-status / support / ssh / mail CLI return ok JSON (ssh destructive escalates)
- [ ] No secrets written into the repo
- [ ] Report: Python version, OS, pytest summary line, sample CLI JSON snippets

## Failure recovery
- ImportError / ModuleNotFoundError → ensure PYTHONPATH=skills and pip install -e .
- pytest collection errors → fix syntax; re-run single test file first
- Config FileNotFoundError → set AZOM_CONFIG_DIR to repo config/
- Permission errors on Windows → skip bash spinup; use pytest + python -m ecom_ops only
- Never disable tests to “pass”; fix root cause

## Deliverable format (your final message)
1. Commands executed (ordered)
2. Version + status output
3. Pytest result summary
4. Smoke command outcomes
5. Any code fixes (file paths + why)
6. “Ready for use” statement with how to start dashboard and bot

Begin now. Optimize for correctness and a green build, not extra features.
```

---

## Short variant (minimal)

```
Clone https://github.com/idealinvestse/customer-agent-template-azom (main).
Create venv, pip install -r requirements.txt && pip install -e .
Set AZOM_USE_MOCK=1, AZOM_CONFIG_DIR=./config, PYTHONPATH=./skills.
Verify: python -m ecom_ops version (must be 2.0.0), pytest -q, then mock smoke:
order-status, product-desc, support, ssh uptime, ssh "rm -rf /" (escalate), mail send+fetch, cases poll+list.
Fix only build/install failures. Report results.
```

## Tags

`#azom` `#ecom-ops` `#v2.0` `#grok-build` `#hetzner` `#woocommerce`
