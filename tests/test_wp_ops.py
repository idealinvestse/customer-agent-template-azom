"""Phase 1 WordPress operations: isolation, health, and no live calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from ecom_ops.cli import main
from ecom_ops.rbac import Actor, Permission, clear_rbac_cache, resolve_actor
from ecom_ops.wp_ops.connectors import ProbeOutcome, ScriptedRest, ScriptedUptime
from ecom_ops.wp_ops.credentials import bind_credential
from ecom_ops.wp_ops.registry import load_sites
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[1]
SHOP_SECRET = "mock-shop-app-password-aaa"
STAGING_SECRET = "mock-staging-app-password-bbb"


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _site(site_id: str, *, environment: str, secret_ref: str, url: str) -> dict:
    return {
        "id": site_id,
        "display_name": site_id,
        "canonical_url": url,
        "environment": environment,
        "business_owner": "customer",
        "hosting_provider": "unconfigured",
        "wordpress_version": "6.7",
        "php_version": "8.3",
        "woocommerce": environment == "production",
        "critical_urls": [url],
        "maintenance_window": "not agreed",
        "secret_ref": secret_ref,
        "alert_owner": "unassigned",
        "alert_confirmations": 2,
        "check_interval_seconds": 60,
        "timeout_seconds": 10,
        "dedupe_window_seconds": 30,
        "capabilities": {"uptime": "available"},
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"sites": rows}), encoding="utf-8")


def _service(tmp_path: Path, rows: list[dict], **kwargs) -> WpOpsService:
    registry = tmp_path / "wp_sites.yaml"
    _write(registry, rows)
    return WpOpsService(registry_path=registry, data_dir=tmp_path, **kwargs)


def test_permissions_follow_roles_not_names():
    viewer = resolve_actor("jonatan")
    operator = Actor(name="agent", role="operator")
    admin = resolve_actor("oscar")
    assert viewer.has(Permission.WP_OPS_READ)
    assert not viewer.has(Permission.WP_OPS_OPERATE)
    assert not viewer.has(Permission.WP_OPS_ADMIN)
    assert not viewer.has(Permission.WP_OPS_RESTORE)
    assert operator.has(Permission.WP_OPS_OPERATE)
    assert not operator.has(Permission.WP_OPS_ADMIN)
    assert admin.has(Permission.WP_OPS_ADMIN)
    assert admin.has(Permission.WP_OPS_RESTORE)


def test_registry_rejects_shared_credentials_and_insecure_urls(tmp_path: Path):
    shared = _site("shop", environment="production", secret_ref="WP_APP_PASSWORD", url="https://shop.example.test/")
    registry = tmp_path / "wp_sites.yaml"
    _write(registry, [shared])
    with pytest.raises(ValueError, match="WP_SECRET_"):
        load_sites(registry)
    insecure = _site(
        "shop",
        environment="production",
        secret_ref="WP_SECRET_SHOP",
        url="http://shop.example.test/",
    )
    _write(registry, [insecure])
    with pytest.raises(ValueError, match="https"):
        load_sites(registry)
    leaked = _site(
        "shop",
        environment="production",
        secret_ref="WP_SECRET_SHOP",
        url="https://user:pw@shop.example.test/",
    )
    _write(registry, [leaked])
    with pytest.raises(ValueError, match="credentials"):
        load_sites(registry)
    once = _site("shop", environment="production", secret_ref="WP_SECRET_SHOP", url="https://shop.example.test/")
    once["alert_confirmations"] = 1
    _write(registry, [once])
    with pytest.raises(ValueError, match="alert_confirmations"):
        load_sites(registry)


def test_sites_do_not_share_credentials_or_mark_unknown_backup_green(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    monkeypatch.setenv("WP_SECRET_SHOP_B", STAGING_SECRET)
    monkeypatch.setenv("WP_APP_PASSWORD", "shared-content-password-999")
    rows = [
        _site("shop-a", environment="production", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/"),
        _site("shop-b", environment="staging", secret_ref="WP_SECRET_SHOP_B", url="https://b.example.test/"),
    ]
    registry = tmp_path / "wp_sites.yaml"
    _write(registry, rows)
    loaded = load_sites(registry)
    with pytest.raises(Exception, match="does not belong"):
        bind_credential(loaded[0], loaded[1].secret_ref)
    bound = bind_credential(loaded[0])
    assert bound.present is True
    assert SHOP_SECRET not in repr(bound)
    assert SHOP_SECRET not in str(bound)
    svc = WpOpsService(registry_path=registry, data_dir=tmp_path)
    listed = svc.list_sites(actor="jonatan")
    blob = json.dumps(listed.to_dict())
    assert "shop-a" in blob and "shop-b" in blob
    assert SHOP_SECRET not in blob
    assert STAGING_SECRET not in blob
    assert "shared-content-password-999" not in blob
    shop_b = next(row for row in listed.data["sites"] if row["id"] == "shop-b")
    assert shop_b["credential_present"] is True
    assert shop_b["backup"]["result"] == "unknown"
    assert shop_b["backup"]["green"] is False
    assert shop_b["healthy"] is False
    assert shop_b["pending_updates"] is None
    assert shop_b["capabilities"]["backup"] == "unknown"


def test_one_failure_does_not_open_a_second_failure_dedupes(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    clock = Clock()
    uptime = ScriptedUptime(ProbeOutcome(result="fail", error_class="ConnectError", dns_ok=True))
    svc = _service(
        tmp_path,
        [_site("shop-a", environment="staging", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/")],
        uptime=uptime,
        clock=clock,
    )
    first = svc.run_checks("shop-a", actor="agent")
    assert first.ok is True
    assert first.data["down"] is False
    assert first.data["executed"] is True
    assert uptime.calls[0][1] == 10
    clock.advance(10)
    duplicate = svc.run_checks("shop-a", actor="agent")
    assert duplicate.data["duplicate"] is True
    assert duplicate.data["down"] is False
    clock.advance(40)
    second = svc.run_checks("shop-a", actor="agent")
    assert second.data["down"] is True
    state = json.loads((tmp_path / "wp_ops_state.json").read_text(encoding="utf-8"))
    open_rows = [row for row in state["incidents"] if row["state"] == "open" and row["check"] == "http_uptime"]
    assert len(open_rows) == 1
    assert open_rows[0]["owner"] == "unassigned"
    assert open_rows[0]["failure_count"] == 2
    clock.advance(40)
    third = svc.run_checks("shop-a", actor="agent")
    assert third.data["down"] is True
    state = json.loads((tmp_path / "wp_ops_state.json").read_text(encoding="utf-8"))
    open_rows = [row for row in state["incidents"] if row["state"] == "open" and row["check"] == "http_uptime"]
    assert len(open_rows) == 1
    assert open_rows[0]["failure_count"] == 3
    blob = json.dumps(state)
    assert SHOP_SECRET not in blob
    http = next(row for row in third.data["checks"] if row["check"] == "http_uptime")
    backup = next(row for row in third.data["checks"] if row["check"] == "backup")
    assert http["timeout_seconds"] == 10
    assert http["duration_ms"] >= 0
    assert backup["result"] == "unknown"
    assert backup["green"] is False
    assert third.data["healthy"] is False


def test_unknown_probe_does_not_clear_or_page(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    clock = Clock()
    uptime = ScriptedUptime(ProbeOutcome(result="fail", error_class="ConnectError"))
    svc = _service(
        tmp_path,
        [_site("shop-a", environment="staging", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/")],
        uptime=uptime,
        clock=clock,
    )
    svc.run_checks("shop-a", actor="agent")
    clock.advance(40)
    uptime.outcome = ProbeOutcome(result="unknown", error_class="UptimeProviderUnconfigured")
    unknown = svc.run_checks("shop-a", actor="agent")
    assert unknown.data["down"] is False
    state = json.loads((tmp_path / "wp_ops_state.json").read_text(encoding="utf-8"))
    assert state["streaks"]["shop-a|http_uptime|https://a.example.test/"] == 1
    assert state["incidents"] == []


def test_operator_cannot_probe_production_and_restore_never_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    monkeypatch.setenv("AZOM_WP_OPS_LIVE", "1")
    uptime = ScriptedUptime(ProbeOutcome(result="ok", tls_days=40, dns_ok=True))
    svc = _service(
        tmp_path,
        [_site("shop-a", environment="production", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/")],
        uptime=uptime,
    )
    denied = svc.run_checks("shop-a", actor="agent")
    assert denied.ok is False
    assert denied.data["error_class"] == "AccessDenied"
    assert denied.data["executed"] is False
    assert uptime.calls == []
    allowed = svc.run_checks("shop-a", actor="oscar")
    assert allowed.ok is True
    assert allowed.data["live_calls"] is False
    assert allowed.data["healthy"] is False
    secret_in_calls = SHOP_SECRET in repr(uptime.calls)
    assert secret_in_calls is False
    refused = svc.restore("shop-a", actor="oscar", change_ref=SHOP_SECRET)
    assert refused.data["error_class"] == "RestoreNotInMvp"
    assert refused.data["executed"] is False
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert SHOP_SECRET not in audit
    assert "RestoreNotInMvp" in audit
    viewer = svc.restore("shop-a", actor="jonatan", change_ref="please")
    assert viewer.data["error_class"] == "AccessDenied"


def test_probe_exception_is_redacted_and_site_lock_is_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    clock = Clock()
    rest = ScriptedRest(exc=RuntimeError(f"body {SHOP_SECRET} token"))
    svc = _service(
        tmp_path,
        [_site("shop-a", environment="staging", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/")],
        rest=rest,
        clock=clock,
    )
    result = svc.run_checks("shop-a", actor="agent")
    assert result.data["partial"] is True
    wp_rest = next(row for row in result.data["checks"] if row["check"] == "wp_rest")
    assert wp_rest["error_class"] == "CheckFailed"
    blob = (tmp_path / "wp_ops_state.json").read_text(encoding="utf-8")
    assert SHOP_SECRET not in blob
    assert SHOP_SECRET not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    clock.advance(40)
    with svc.hold_site("shop-a"):
        busy = svc.run_checks("shop-a", actor="agent")
    assert busy.data["error_class"] == "SiteBusy"
    state = json.loads((tmp_path / "wp_ops_state.json").read_text(encoding="utf-8"))
    assert state["streaks"].get("shop-a|wp_rest|https://a.example.test/", 0) == 1


def test_due_checks_skip_production_for_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    monkeypatch.setenv("WP_SECRET_SHOP_B", STAGING_SECRET)
    clock = Clock()
    svc = _service(
        tmp_path,
        [
            _site("shop-a", environment="production", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/"),
            _site("shop-b", environment="staging", secret_ref="WP_SECRET_SHOP_B", url="https://b.example.test/"),
        ],
        clock=clock,
    )
    viewer = svc.run_due(actor="jonatan")
    assert viewer.ok is False
    due = svc.run_due(actor="agent")
    assert due.ok is True
    assert due.data["ran"] == ["shop-b"]
    assert {"site_id": "shop-a", "error_class": "AccessDenied"} in due.data["skipped"]
    again = svc.run_due(actor="agent")
    assert {"site_id": "shop-b", "error_class": "NotDue"} in again.data["skipped"]
    clock.advance(70)
    repeated = svc.run_due(actor="agent")
    assert "shop-b" in repeated.data["ran"]


def test_show_site_does_not_return_another_site(tmp_path):
    svc = _service(
        tmp_path,
        [
            _site("shop-a", environment="staging", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/"),
            _site("shop-b", environment="staging", secret_ref="WP_SECRET_SHOP_B", url="https://b.example.test/"),
        ],
    )
    missing = svc.show_site("shop-c", actor="jonatan")
    assert missing.data["error_class"] == "UnknownSite"
    assert "shop-a" not in json.dumps(missing.to_dict())
    shown = svc.show_site("shop-a", actor="jonatan")
    assert shown.data["site"]["id"] == "shop-a"
    assert shown.data["site"]["secret_ref"] == "WP_SECRET_SHOP_A"


def test_package_has_no_shell_or_http_client():
    package = ROOT / "skills" / "ecom_ops" / "wp_ops"
    text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    for banned in ("requests", "urlopen", "http.client", "subprocess", "os.system", "integrations.wordpress"):
        assert banned not in text


def test_repo_registry_and_cli_keep_secrets_out(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", STAGING_SECRET)
    monkeypatch.delenv("WP_SECRET_MOCK_SHOP", raising=False)
    clear_rbac_cache()
    sites = load_sites()
    assert {site.id for site in sites} == {"mock-shop", "mock-staging"}
    assert main(["--mock", "--actor", "jonatan", "wp", "sites"]) == 0
    listed = capsys.readouterr().out
    assert "mock-shop" in listed and "mock-staging" in listed
    assert STAGING_SECRET not in listed
    assert main(["--mock", "--actor", "agent", "wp", "check", "--id", "mock-shop"]) == 1
    capsys.readouterr()
    assert main(["--mock", "--actor", "agent", "wp", "check", "--id", "mock-staging"]) == 0
    checked = capsys.readouterr().out
    assert STAGING_SECRET not in checked
    assert "unknown" in checked
    with pytest.raises(SystemExit) as exc:
        main(["wp", "restore"])
    assert exc.value.code == 2


class _FailingBackup:
    def verify(self, record: dict) -> bool:
        del record
        return False


def _two_sites() -> list[dict]:
    return [
        _site("shop-a", environment="production", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/"),
        _site("stage", environment="staging", secret_ref="WP_SECRET_SHOP_B", url="https://stage.example.test/"),
    ]


def test_registry_defaults_retention_and_rejects_out_of_range(tmp_path: Path):
    row = _site("shop", environment="staging", secret_ref="WP_SECRET_SHOP", url="https://shop.example.test/")
    registry = tmp_path / "wp_sites.yaml"
    _write(registry, [row])
    assert load_sites(registry)[0].retention_days == 14
    row["retention_days"] = 0
    _write(registry, [row])
    with pytest.raises(ValueError, match="retention_days"):
        load_sites(registry)
    row["retention_days"] = 91
    _write(registry, [row])
    with pytest.raises(ValueError, match="retention_days"):
        load_sites(registry)


def test_mock_backup_verifies_one_local_manifest_and_stays_not_green(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    clock = Clock()
    svc = _service(
        tmp_path,
        [_site("shop-a", environment="staging", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/")],
        clock=clock,
    )
    created = svc.create_backup("shop-a", actor="agent")
    assert created.ok is True
    assert created.data["live_calls"] is False
    assert created.data["executed"] is True
    backup = created.data["backup"]
    assert backup["provider"] == "mock"
    assert backup["offsite"] is False
    assert backup["encryption"] == "unconfigured"
    assert backup["payload"] == "metadata"
    assert backup["bytes_copied"] is False
    assert backup["parts"] == ["files", "database"]
    assert backup["completion"] == "verified"
    assert backup["integrity"] == "passed"
    assert backup["destination_ref"] == f"mock:shop-a:{backup['id']}"
    assert SHOP_SECRET not in json.dumps(created.to_dict())
    clock.advance(40)
    checked = svc.run_checks("shop-a", actor="agent")
    assert checked.data["healthy"] is False
    listed = svc.list_sites(actor="jonatan")
    row = listed.data["sites"][0]
    assert row["backup"]["result"] == "verified"
    assert row["backup"]["tone"] == "attention"
    assert row["backup"]["green"] is False
    assert row["healthy"] is False
    shown = svc.show_site("shop-a", actor="jonatan")
    assert shown.data["backups"][0]["id"] == backup["id"]
    reloaded = WpOpsService(registry_path=svc.registry_path, data_dir=tmp_path, clock=clock)
    again = reloaded.list_backups("shop-a", actor="jonatan")
    assert again.ok is True
    assert again.data["backups"][0]["id"] == backup["id"]
    assert SHOP_SECRET not in (tmp_path / "wp_ops_state.json").read_text(encoding="utf-8")


def test_failed_integrity_is_stored_and_cannot_be_drilled(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    svc = _service(
        tmp_path,
        [_site("shop-a", environment="staging", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/")],
        backup=_FailingBackup(),
    )
    created = svc.create_backup("shop-a", actor="agent")
    assert created.ok is False
    assert created.data["error_class"] == "IntegrityFailed"
    assert created.data["executed"] is True
    assert created.data["backup"]["completion"] == "failed"
    assert created.data["backup"]["integrity"] == "failed"
    assert created.data["live_calls"] is False
    listed = svc.list_sites(actor="jonatan")
    assert listed.data["sites"][0]["backup"]["result"] == "fail"
    assert listed.data["sites"][0]["backup"]["green"] is False
    drill = svc.restore_drill(created.data["backup"]["id"], target_site_id="shop-a", actor="agent")
    assert drill.ok is False
    assert drill.data["executed"] is False
    assert drill.data["error_class"] == "BackupNotVerified"


def test_expired_backup_stays_on_disk_and_is_not_a_success(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    clock = Clock()
    svc = _service(
        tmp_path,
        [_site("shop-a", environment="staging", secret_ref="WP_SECRET_SHOP_A", url="https://a.example.test/")],
        clock=clock,
    )
    created = svc.create_backup("shop-a", actor="agent")
    backup_id = created.data["backup"]["id"]
    clock.advance(14 * 86400 + 60)
    listed = svc.list_backups("shop-a", actor="jonatan")
    assert listed.ok is True
    assert listed.data["backups"][0]["completion"] == "expired"
    stored = json.loads((tmp_path / "wp_ops_state.json").read_text(encoding="utf-8"))
    assert stored["backups"][0]["id"] == backup_id
    assert stored["backups"][0]["completion"] == "verified"
    overview = svc.list_sites(actor="jonatan")
    assert overview.data["sites"][0]["backup"]["result"] == "unknown"
    assert overview.data["sites"][0]["backup"]["green"] is False
    drill = svc.restore_drill(backup_id, target_site_id="shop-a", actor="agent")
    assert drill.data["error_class"] == "BackupExpired"
    assert drill.data["executed"] is False


def test_staging_drill_records_recovery_and_production_restore_stays_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_A", SHOP_SECRET)
    monkeypatch.setenv("WP_SECRET_SHOP_B", STAGING_SECRET)
    clock = Clock()
    svc = _service(tmp_path, _two_sites(), clock=clock)
    denied = svc.create_backup("shop-a", actor="agent")
    assert denied.data["error_class"] == "AccessDenied"
    assert denied.data["executed"] is False
    created = svc.create_backup("shop-a", actor="oscar")
    assert created.ok is True
    backup_id = created.data["backup"]["id"]
    drill = svc.restore_drill(backup_id, target_site_id="stage", actor="agent")
    assert drill.ok is True
    assert drill.data["duplicate"] is False
    assert drill.data["executed"] is True
    assert drill.data["live_calls"] is False
    body = drill.data["drill"]
    assert body["target_environment"] == "staging"
    assert body["source_backup_id"] == backup_id
    assert body["parts"] == ["files", "database"]
    assert body["recovery_seconds"] >= 0
    assert body["order_reconciliation"] == "required"
    assert body["orders_mutated"] is False
    assert body["bytes_copied"] is False
    blob = json.dumps(drill.to_dict())
    assert SHOP_SECRET not in blob
    assert STAGING_SECRET not in blob
    clock.advance(5)
    again = svc.restore_drill(backup_id, target_site_id="stage", actor="agent")
    assert again.data["duplicate"] is True
    assert again.data["executed"] is False
    assert again.data["drill"]["id"] == body["id"]
    production = svc.restore_drill(backup_id, target_site_id="shop-a", actor="oscar")
    assert production.ok is False
    assert production.data["error_class"] == "TargetNotStaging"
    assert production.data["executed"] is False
    refused = svc.restore("shop-a", actor="oscar", change_ref=SHOP_SECRET)
    assert refused.data["error_class"] == "RestoreNotInMvp"
    assert refused.data["executed"] is False
    state = json.loads((tmp_path / "wp_ops_state.json").read_text(encoding="utf-8"))
    assert len(state["drills"]) == 1
    assert SHOP_SECRET not in json.dumps(state)
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert SHOP_SECRET not in audit
    assert "RestoreNotInMvp" in audit


def test_viewer_lists_backups_and_cannot_create_or_drill(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP_B", STAGING_SECRET)
    clock = Clock()
    svc = _service(
        tmp_path,
        [_site("stage", environment="staging", secret_ref="WP_SECRET_SHOP_B", url="https://stage.example.test/")],
        clock=clock,
    )
    assert svc.create_backup("stage", actor="jonatan").data["error_class"] == "AccessDenied"
    created = svc.create_backup("stage", actor="agent")
    assert svc.list_backups("stage", actor="jonatan").data["backups"][0]["id"] == created.data["backup"]["id"]
    drill = svc.restore_drill(created.data["backup"]["id"], target_site_id="stage", actor="agent")
    assert drill.data["drill"]["order_reconciliation"] == "not_applicable"
    assert svc.restore_drill(created.data["backup"]["id"], target_site_id="stage", actor="jonatan").data[
        "error_class"
    ] == "AccessDenied"
    clock.advance(40)
    with svc.hold_site("stage"):
        busy = svc.create_backup("stage", actor="agent")
    assert busy.data["error_class"] == "SiteBusy"
    assert busy.data["executed"] is False


def test_cli_backup_and_drill_keep_restore_unknown(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", STAGING_SECRET)
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", SHOP_SECRET)
    clear_rbac_cache()
    assert main(["--mock", "--actor", "agent", "wp", "backup", "--id", "mock-shop"]) == 1
    capsys.readouterr()
    assert main(["--mock", "--actor", "oscar", "wp", "backup", "--id", "mock-shop"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["data"]["backup"]["encryption"] == "unconfigured"
    assert created["data"]["live_calls"] is False
    assert SHOP_SECRET not in json.dumps(created)
    assert main(["--mock", "--actor", "jonatan", "wp", "backups", "--id", "mock-shop"]) == 0
    listed = json.loads(capsys.readouterr().out)
    backup_id = listed["data"]["backups"][0]["id"]
    assert main(
        [
            "--mock",
            "--actor",
            "agent",
            "wp",
            "restore-drill",
            "--backup",
            backup_id,
            "--target",
            "mock-staging",
        ]
    ) == 0
    drilled = json.loads(capsys.readouterr().out)
    assert drilled["data"]["drill"]["recovery_seconds"] >= 0
    assert drilled["data"]["drill"]["orders_mutated"] is False
    assert STAGING_SECRET not in json.dumps(drilled)
    with pytest.raises(SystemExit) as exc:
        main(["wp", "restore"])
    assert exc.value.code == 2
