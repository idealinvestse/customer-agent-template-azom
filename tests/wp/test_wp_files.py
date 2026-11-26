"""Typed theme and plugin edits. No general shell and no live SSH."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecom_ops.cli import main
from ecom_ops.integrations.wp_files_ssh import SshFileTransport, build_file_transport
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.runtime_env import overlay_key_allowed
from ecom_ops.security import is_critical_ssh_command, is_ssh_allowlisted
from ecom_ops.wp_ops.files import LocalTree, TransportError
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[2]
SECRET = "mock-shop-app-password-aaa"
MARKER = "AZOM-FILE-MARKER"
STYLE = "/var/www/shop/wp-content/themes/twentytwentyfive/style.css"


class MemTree:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.files = {STYLE: b"/* mock theme */\n"}

    def list_dir(self, absolute_dir: str) -> list[str]:
        self.reads.append(absolute_dir)
        prefix = absolute_dir.rstrip("/") + "/"
        names = set()
        for path in self.files:
            if path.startswith(prefix):
                rest = path[len(prefix) :]
                names.add(rest.split("/", 1)[0])
        return sorted(names)

    def read_bytes(self, absolute_file: str) -> bytes:
        self.reads.append(absolute_file)
        if absolute_file not in self.files:
            raise TransportError("FileMissing")
        return self.files[absolute_file]

    def write_bytes(self, absolute_file: str, data: bytes) -> None:
        self.writes.append(absolute_file)
        if absolute_file not in self.files:
            raise TransportError("FileMissing")
        self.files[absolute_file + ".azom-bak"] = self.files[absolute_file]
        self.files[absolute_file] = data


def _row(site_id: str, *, environment: str, host: str = "shop.example.test") -> dict:
    return {
        "id": site_id,
        "display_name": site_id,
        "canonical_url": f"https://{site_id}.example.test/",
        "environment": environment,
        "business_owner": "customer",
        "hosting_provider": "unconfigured",
        "wordpress_version": "6.7",
        "php_version": "8.3",
        "woocommerce": environment == "production",
        "critical_urls": [f"https://{site_id}.example.test/"],
        "maintenance_window": "not agreed",
        "secret_ref": "WP_SECRET_SHOP",
        "alert_owner": "unassigned",
        "alert_confirmations": 2,
        "check_interval_seconds": 60,
        "timeout_seconds": 10,
        "dedupe_window_seconds": 30,
        "capabilities": {"uptime": "available"},
        "files_root": "/var/www/shop",
        "ssh_host": host,
    }


def _service(tmp_path: Path, rows: list[dict], tree: MemTree | None = None) -> WpOpsService:
    registry = tmp_path / "wp_sites.yaml"
    import yaml

    registry.write_text(yaml.safe_dump({"sites": rows}), encoding="utf-8")
    factory = None if tree is None else (lambda site: tree)
    return WpOpsService(registry_path=registry, data_dir=tmp_path, files_factory=factory)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_WP_FILES_SSH", raising=False)
    clear_rbac_cache()


def test_rejected_paths_do_not_touch_transport_and_roles_are_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP", SECRET)
    tree = MemTree()
    staging = _service(tmp_path, [_row("stage", environment="staging")], tree)
    for relative in ("../wp-config.php", "nested/../wp-config.php", "drop.phar", "style.css.azom-bak"):
        refused = staging.propose_file(
            "stage",
            "theme",
            "twentytwentyfive",
            relative,
            "<?php echo 1;\n",
            actor="agent",
        )
        assert refused.data["error_class"] == "PathRejected"
        assert refused.data["executed"] is False
    assert tree.reads == []
    assert tree.writes == []
    missing = staging.propose_file(
        "stage", "theme", "twentytwentyfive", "missing.php", "<?php\n", actor="agent"
    )
    assert missing.data["error_class"] == "FileMissing"
    assert tree.writes == []
    viewer = staging.propose_file(
        "stage", "theme", "twentytwentyfive", "style.css", "x\n", actor="jonatan"
    )
    assert viewer.data["error_class"] == "AccessDenied"
    prod_dir = tmp_path / "prod"
    prod_dir.mkdir()
    production = _service(
        prod_dir,
        [_row("shop", environment="production")],
        MemTree(),
    )
    denied = production.propose_file(
        "shop", "theme", "twentytwentyfive", "style.css", "x\n", actor="agent"
    )
    assert denied.data["error_class"] == "AccessDenied"
    bad_plan = staging.apply_file("stage", "not-a-plan", "x\n", actor="jonatan")
    assert bad_plan.data["error_class"] == "AccessDenied"


def test_propose_and_apply_are_bound_to_the_same_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_SECRET_SHOP", SECRET)
    tree = MemTree()
    tree.files[STYLE] = f"/* secret {SECRET} */\n".encode()
    svc = _service(tmp_path, [_row("stage", environment="staging")], tree)
    loaded = svc.read_file("stage", "theme", "twentytwentyfive", "style.css", actor="jonatan")
    assert loaded.ok is True
    assert SECRET not in loaded.data["content"]
    assert "***" in loaded.data["content"]
    proposed = f"/* secret {SECRET} */\n{MARKER}\n"
    plan = svc.propose_file(
        "stage", "theme", "twentytwentyfive", "style.css", proposed, actor="agent"
    )
    assert plan.ok is True
    assert plan.data["executed"] is False
    assert plan.data["live_calls"] is False
    assert MARKER not in json.dumps(plan.to_dict())
    plan_id = plan.data["plan"]["id"]
    mismatch = svc.apply_file("stage", plan_id, "other\n", actor="agent")
    assert mismatch.data["error_class"] == "DigestMismatch"
    assert tree.files[STYLE] == f"/* secret {SECRET} */\n".encode()
    tree.files[STYLE] = b"/* changed on server */\n"
    stale = svc.apply_file("stage", plan_id, proposed, actor="agent")
    assert stale.data["error_class"] == "StaleFile"
    assert tree.files[STYLE] == b"/* changed on server */\n"
    tree.files[STYLE] = f"/* secret {SECRET} */\n".encode()
    fresh = svc.propose_file(
        "stage", "theme", "twentytwentyfive", "style.css", proposed, actor="agent"
    )
    applied = svc.apply_file("stage", fresh.data["plan"]["id"], proposed, actor="agent")
    assert applied.ok is True
    assert applied.data["executed"] is True
    assert tree.files[STYLE] == proposed.encode()
    assert tree.writes == [STYLE]
    state = json.dumps(svc.store.load())
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert MARKER not in state
    assert MARKER not in audit
    assert SECRET not in state
    assert SECRET not in audit
    assert STYLE in audit
    assert fresh.data["plan"]["after_digest"] in audit
    busy = svc.propose_file(
        "stage", "theme", "twentytwentyfive", "style.css", "again\n", actor="agent"
    )
    assert busy.ok is True
    with svc.hold_site("stage"):
        locked = svc.propose_file(
            "stage", "theme", "twentytwentyfive", "style.css", "later\n", actor="agent"
        )
    assert locked.data["error_class"] == "SiteBusy"
    assert locked.data["executed"] is False


def test_mock_flag_blocks_ssh_and_example_host_is_not_dialed(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    site = parse_site(_row("stage", environment="staging", host="mock-staging.example.test"))
    def _refuse_socket(*_args, **_kwargs):
        raise AssertionError("ssh socket was opened")

    monkeypatch.setattr(
        "ecom_ops.integrations.wp_files_ssh.socket.create_connection", _refuse_socket
    )
    transport = build_file_transport(site, tmp_path)
    assert isinstance(transport, LocalTree)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    live = build_file_transport(site, tmp_path)
    assert isinstance(live, SshFileTransport)
    with pytest.raises(TransportError) as refused:
        live.list_dir("/var/www/shop/wp-content/themes")
    assert refused.value.error_class == "SshHostRefused"
    with pytest.raises(TransportError) as blocked:
        live.write_bytes(STYLE, b"/* next */\n")
    assert blocked.value.error_class == "SshWriteDisabled"
    assert overlay_key_allowed("AZOM_WP_FILES_SSH") is False
    assert is_critical_ssh_command("sed -i 's/a/b/' file.php")
    assert is_critical_ssh_command("vim wp-config.php")
    assert not is_ssh_allowlisted("tee -- '/var/www/shop/a.php'")


def test_broad_root_is_refused_before_the_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    row = _row("stage", environment="staging")
    row["files_root"] = "/var/www"
    tree = MemTree()
    svc = _service(tmp_path, [row], tree)
    read = svc.read_file("stage", "theme", "twentytwentyfive", "style.css", actor="jonatan")
    assert read.ok is False
    assert read.data["error_class"] == "PathRejected"
    assert read.message == "The files root is too broad."
    assert tree.reads == []


def test_cli_proposes_from_a_file_and_keeps_the_body_out_of_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    source = tmp_path / "next.css"
    source.write_text(f"/* mock theme */\n{MARKER}\n", encoding="utf-8")
    assert main(
        [
            "--mock",
            "--actor",
            "agent",
            "wp",
            "file-propose",
            "--id",
            "mock-shop",
            "--kind",
            "theme",
            "--slug",
            "twentytwentyfive",
            "--path",
            "style.css",
            "--from",
            str(source),
        ]
    ) == 1
    capsys.readouterr()
    listed = main(
        ["--mock", "--actor", "jonatan", "wp", "files", "--id", "mock-staging", "--kind", "theme"]
    )
    assert listed == 0
    assert "twentytwentyfive" in capsys.readouterr().out
    assert main(
        [
            "--mock",
            "--actor",
            "agent",
            "wp",
            "file-propose",
            "--id",
            "mock-staging",
            "--kind",
            "theme",
            "--slug",
            "twentytwentyfive",
            "--path",
            "style.css",
            "--from",
            str(source),
        ]
    ) == 0
    proposed = json.loads(capsys.readouterr().out)
    assert MARKER not in json.dumps(proposed)
    plan_id = proposed["data"]["plan"]["id"]
    assert main(
        [
            "--mock",
            "--actor",
            "agent",
            "wp",
            "file-apply",
            "--id",
            "mock-staging",
            "--plan",
            plan_id,
            "--from",
            str(source),
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["data"]["executed"] is True
    assert MARKER not in json.dumps(applied)
    written = (
        tmp_path
        / "wp_files_mock"
        / "mock-staging"
        / "wp-content"
        / "themes"
        / "twentytwentyfive"
        / "style.css"
    )
    assert written.read_text(encoding="utf-8") == f"/* mock theme */\n{MARKER}\n"
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert MARKER not in audit
    with pytest.raises(SystemExit) as exc:
        main(["wp", "file-propose", "--body", "nope"])
    assert exc.value.code == 2
