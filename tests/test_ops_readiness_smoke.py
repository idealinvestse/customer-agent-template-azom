"""P6: /health readiness (last poll age) + opt-in live smoke."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    for name in ("azom_dashboard", "settings_store", "secret_probes", "status"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod


def test_poll_writes_last_case_poll_marker(tmp_path, monkeypatch):
    from ecom_ops.cases.mailboxes import MailboxConfig
    from ecom_ops.cases.service import CaseService
    from ecom_ops.cases.store import CaseStore
    from ecom_ops.integrations.mail import (
        InMemoryMailTransport,
        MailClient,
        MailConfig,
        MailProvider,
    )
    from ecom_ops.ops_status import last_case_poll_path, read_last_case_poll

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    client = MailClient(
        config=MailConfig(provider=MailProvider.GENERIC_IMAP, from_addr="s@azom.se"),
        transport=InMemoryMailTransport(),
    )
    monkeypatch.setattr(
        "ecom_ops.cases.service.client_from_env", lambda **kw: client
    )
    monkeypatch.setattr(
        "ecom_ops.cases.service.enabled_mailboxes",
        lambda: [
            MailboxConfig(
                id="support_default", label="Support", address="support@azom.se"
            )
        ],
    )
    svc = CaseService(store=store, mail_client=client)
    result = svc.poll(actor="agent", use_mock=True)
    assert result.ok
    assert last_case_poll_path().is_file()
    marker = read_last_case_poll()
    assert marker is not None
    assert marker.get("ok") is True
    assert "polled_at" in marker


def test_health_includes_readiness_last_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret")
    monkeypatch.chdir(DASH_DIR)

    from ecom_ops.ops_status import write_last_case_poll

    write_last_case_poll(ok=True, errors=0, created=1)
    mod = _load_dashboard()
    mod.app.config["TESTING"] = True
    client = mod.app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data.get("liveness") is True
    assert "readiness" in data
    assert data["readiness"]["last_poll_ok"] is True
    assert data["readiness"]["last_poll_age_sec"] is not None
    assert data["readiness"]["last_poll_age_sec"] < 60


def test_health_marks_stale_poll_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret")
    monkeypatch.setenv("AZOM_POLL_STALE_SEC", "120")
    monkeypatch.chdir(DASH_DIR)

    from ecom_ops.ops_status import write_last_case_poll

    # Backdated poll
    old = datetime.now(timezone.utc).timestamp() - 600
    write_last_case_poll(
        ok=True,
        errors=0,
        created=0,
        polled_at=datetime.fromtimestamp(old, tz=timezone.utc).isoformat(),
    )
    mod = _load_dashboard()
    mod.app.config["TESTING"] = True
    client = mod.app.test_client()
    data = client.get("/health").get_json()
    assert data["liveness"] is True
    assert data["readiness"]["stale"] is True
    assert data["readiness"]["ok"] is False


def test_smoke_skipped_without_live_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_LIVE_SMOKE", raising=False)
    from ecom_ops.smoke import run_live_smoke

    result = run_live_smoke()
    assert result["ok"] is True
    assert result.get("skipped") is True


def test_smoke_mock_mode_runs_local_checks(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_LIVE_SMOKE", "1")
    from ecom_ops.smoke import run_live_smoke

    result = run_live_smoke()
    assert result["ok"] is True
    assert result.get("skipped") is not True
    checks = {c["name"]: c for c in result["checks"]}
    assert "woocommerce" in checks
    assert checks["woocommerce"]["ok"] is True
