"""Per-site SSH settings. The private key stays out of JSON, audit, and HTML."""

from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ecom_ops.integrations.ssh import is_critical_ssh_command, is_ssh_allowlisted
from ecom_ops.integrations.wp_files_ssh import (
    OpenSshRunner,
    SshFileTransport,
    build_file_transport,
)
from ecom_ops.wp_ops.files import TransportError
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.registry import load_sites
from ecom_ops.wp_ops.service import WpOpsService
from ecom_ops.wp_ops.ssh_settings import identity_ok

ROOT = Path(__file__).resolve().parents[1]
MARKER = "AZOM-SSH-KEY-MARKER"
KEY = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{MARKER}\n-----END OPENSSH PRIVATE KEY-----\n"
IDENTITY = "C:/keys/azom-ssh-test"
HOST_KEY = "ssh-ed25519 " + base64.b64encode(b"azom-host-key-bytes").decode("ascii") + " test"


def _row(site_id: str = "stage") -> dict:
    return {
        "id": site_id,
        "display_name": site_id,
        "canonical_url": f"https://{site_id}.example.test/",
        "environment": "staging",
        "business_owner": "customer",
        "hosting_provider": "unconfigured",
        "wordpress_version": "6.7",
        "php_version": "8.3",
        "woocommerce": False,
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
        "ssh_host": "stage.example.test",
    }


def _service(tmp_path: Path) -> WpOpsService:
    registry = tmp_path / "wp_sites.yaml"
    registry.write_text(yaml.safe_dump({"sites": [_row()]}), encoding="utf-8")
    return WpOpsService(registry_path=registry, data_dir=tmp_path)


def _save(svc: WpOpsService, **overrides: object) -> object:
    fields: dict[str, object] = {
        "files_root": "/var/www/custom",
        "ssh_host": "shop.internal",
        "ssh_user": "deploy",
        "ssh_port": "2222",
        "ssh_identity_file": IDENTITY,
        "private_key": "",
        "clear_key": False,
        "global_user": "",
        "global_port": "",
        "global_identity_file": "",
    }
    fields.update(overrides)
    return svc.save_ssh_settings("stage", actor="oscar", **fields)  # type: ignore[arg-type]


def test_overlay_changes_the_root_the_editor_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    svc = _service(tmp_path)
    saved = _save(svc, private_key=KEY)
    assert saved.ok is True
    loaded = load_sites(svc.registry_path, data_dir=tmp_path)
    assert loaded[0].files_root == "/var/www/custom"
    assert loaded[0].ssh_user == "deploy"
    assert loaded[0].ssh_port == 2222
    read = svc.read_file("stage", "theme", "twentytwentyfive", "style.css", actor="jonatan")
    assert read.ok is True
    assert read.data["path"] == "/var/www/custom/wp-content/themes/twentytwentyfive/style.css"
    raw = (svc.registry_path or Path()).read_text(encoding="utf-8")
    assert "/var/www/custom" not in raw
    assert "/var/www/shop" in raw


def test_bad_root_is_rejected_and_the_key_is_not_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    svc = _service(tmp_path)
    before = svc.ssh_settings("stage", actor="oscar")
    refused = _save(svc, files_root="../etc", private_key=KEY)
    assert refused.ok is False
    assert refused.data["error_class"] == "ConfigInvalid"
    assert MARKER not in refused.message
    assert MARKER not in json.dumps(refused.to_dict())
    after = svc.ssh_settings("stage", actor="oscar")
    assert after.data["ssh"]["files_root"] == before.data["ssh"]["files_root"]
    assert not (tmp_path / "ssh-keys" / "stage").exists()
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert MARKER not in audit


def test_private_key_is_written_once_and_left_out_of_records(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    svc = _service(tmp_path)
    saved = _save(svc, private_key=KEY)
    assert saved.ok is True
    assert MARKER not in json.dumps(saved.to_dict())
    key_path = tmp_path / "ssh-keys" / "stage"
    assert MARKER in key_path.read_text(encoding="utf-8")
    overlay = (tmp_path / "wp_ssh.json").read_text(encoding="utf-8")
    assert MARKER not in overlay
    assert "PRIVATE KEY" not in overlay
    assert "ssh-keys" not in overlay
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert MARKER not in audit
    assert IDENTITY not in audit
    assert "ssh_key" in audit
    view = svc.ssh_settings("stage", actor="jonatan")
    body = json.dumps(view.to_dict())
    assert view.data["ssh"]["ssh_key_stored"] is True
    assert view.data["ssh"]["ssh_identity_file"] == ""
    assert MARKER not in body
    assert IDENTITY not in body
    oscar = svc.ssh_settings("stage", actor="oscar")
    assert oscar.data["ssh"]["ssh_identity_file"] == IDENTITY
    again = _save(svc, private_key="   ")
    assert again.ok is True
    assert MARKER in key_path.read_text(encoding="utf-8")
    cleared = _save(svc, private_key="", clear_key=True, ssh_identity_file="")
    assert cleared.ok is True
    assert not key_path.exists()
    assert svc.ssh_settings("stage", actor="jonatan").data["ssh"]["ssh_key_stored"] is False


def test_clear_and_paste_together_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    svc = _service(tmp_path)
    _save(svc, private_key=KEY)
    refused = _save(svc, private_key=KEY, clear_key=True)
    assert refused.ok is False
    assert MARKER not in refused.message
    assert MARKER in (tmp_path / "ssh-keys" / "stage").read_text(encoding="utf-8")


def test_roles_and_corrupt_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")
    svc = _service(tmp_path)
    for actor in ("jonatan", "agent"):
        refused = svc.save_ssh_settings(
            "stage",
            actor=actor,
            files_root="/var/www/custom",
            ssh_host="shop.internal",
            ssh_user="",
            ssh_port="",
            ssh_identity_file="",
            private_key=KEY,
            clear_key=False,
            global_user="",
            global_port="",
            global_identity_file="",
        )
        assert refused.data["error_class"] == "AccessDenied"
    assert not (tmp_path / "ssh-keys").exists()
    (tmp_path / "wp_ssh.json").write_text("{not-json " + MARKER, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_sites(svc.registry_path, data_dir=tmp_path)
    assert str(exc.value) == "wp_ssh.json kan inte läsas."
    assert MARKER not in str(exc.value)
    shown = svc.ssh_settings("stage", actor="oscar")
    assert shown.data["error_class"] == "ConfigInvalid"
    assert MARKER not in shown.message
    assert os.environ["AZOM_WP_FILES_SSH"] == "1"


def test_global_fields_leave_empty_values_and_secrets_win(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SSH_USER", "root")
    runtime = tmp_path / "runtime.env"
    runtime.write_text("SSH_USER=keepme\nOPENAI_MODEL=openai/gpt-4o-mini\n", encoding="utf-8")
    svc = _service(tmp_path)
    kept = _save(svc, global_user="", private_key="")
    assert kept.ok is True
    assert "SSH_USER=keepme" in runtime.read_text(encoding="utf-8")
    (tmp_path / "secrets.env").write_text("SSH_USER=fromsecret\n", encoding="utf-8")
    held = _save(svc, global_user="deploy", private_key="")
    assert held.ok is True
    assert "secrets.env" in held.message
    assert "fromsecret" not in held.message
    assert "deploy" not in held.message
    assert os.environ["SSH_USER"] == "fromsecret"
    text = runtime.read_text(encoding="utf-8")
    assert "SSH_USER=deploy" in text
    assert "OPENAI_MODEL=openai/gpt-4o-mini" in text
    assert MARKER not in text
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "fromsecret" not in audit
    assert "deploy" not in audit


def test_runner_uses_site_login_and_still_refuses_example_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")
    monkeypatch.delenv("SSH_IDENTITY_FILE", raising=False)
    site = replace(
        parse_site(_row()),
        ssh_host="shop.internal",
        ssh_user="deploy",
        ssh_port=2222,
        ssh_identity_file="/keys/id",
    )
    transport = build_file_transport(site, tmp_path)
    assert isinstance(transport, SshFileTransport)
    argv = transport.runner.argv("shop.internal", "cat -- '/var/www/shop/a.css'", stdin=None)
    assert argv is not None
    assert argv[argv.index("-p") + 1] == "2222"
    assert "/keys/id" in argv
    assert argv[-2] == "deploy@shop.internal"
    assert "IdentitiesOnly=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "accept-new" not in argv
    key_path = tmp_path / "ssh-keys" / "stage"
    key_path.parent.mkdir(parents=True)
    key_path.write_text(KEY, encoding="utf-8")
    stored = build_file_transport(site, tmp_path)
    stored_argv = stored.runner.argv("shop.internal", "cat -- '/var/www/shop/a.css'", stdin=None)
    assert stored_argv is not None
    assert str(key_path) in stored_argv
    assert "/keys/id" not in stored_argv
    called: list[list[str]] = []

    class Boom:
        def run(self, host: str, command: str, *, stdin: bytes | None = None, timeout: int = 30):
            del host, command, stdin, timeout
            called.append(["called"])
            raise AssertionError("example.test was dialed")

    refused_site = replace(site, ssh_host="stage.example.test")
    live = build_file_transport(refused_site, tmp_path, runner=Boom())
    with pytest.raises(TransportError) as refused:
        live.list_dir("/var/www/shop/wp-content/themes")
    assert refused.value.error_class == "SshHostRefused"
    assert called == []
    unpinned = build_file_transport(site, tmp_path, runner=Boom())
    with pytest.raises(TransportError) as missing_key:
        unpinned.list_dir("/var/www/shop/wp-content/themes")
    assert missing_key.value.error_class == "SshHostKeyMissing"
    assert called == []
    assert is_critical_ssh_command("sed -i 's/a/b/' file.php")
    assert is_critical_ssh_command("vim wp-config.php")
    assert not is_ssh_allowlisted("tee -- '/var/www/shop/a.php'")
    openssh = OpenSshRunner(user="deploy", port="2222", identity="/keys/id")
    direct = openssh.argv("shop.internal", "cat -- '/var/www/shop/a.css'", stdin=None)
    assert direct is not None and "deploy@shop.internal" in direct
    assert identity_ok("../keys/id") is False
    assert identity_ok("C:/keys/../id") is False
    assert identity_ok("~/.ssh/id_ed25519") is True
    assert identity_ok("keys/id_ed25519") is True
    assert identity_ok("/home/deploy/my keys/id") is True
    assert identity_ok("/keys/id") is True
    general = (ROOT / "skills" / "ecom_ops" / "integrations" / "ssh.py").read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=accept-new" in general


def test_wp_ssh_requires_an_explicit_non_root_user_before_execution(monkeypatch, tmp_path):
    monkeypatch.delenv("SSH_USER", raising=False)
    monkeypatch.delenv("SSH_PORT", raising=False)
    monkeypatch.delenv("SSH_IDENTITY_FILE", raising=False)
    calls: list[list[str]] = []
    runner = OpenSshRunner(
        execute=lambda argv, stdin, timeout: calls.append(argv) or (0, b""),
        host_public_key=HOST_KEY,
        known_hosts=tmp_path / "known_hosts",
    )
    assert runner.argv("shop.internal", "cat -- '/var/www/shop/a.css'", stdin=None) is None
    assert runner.run("shop.internal", "cat -- '/var/www/shop/a.css'")[0] == 126
    assert calls == []

    root_runner = OpenSshRunner(
        execute=lambda argv, stdin, timeout: calls.append(argv) or (0, b""),
        user="root",
        host_public_key=HOST_KEY,
        known_hosts=tmp_path / "known_hosts",
    )
    assert root_runner.argv("shop.internal", "cat -- '/var/www/shop/a.css'", stdin=None) is None
    assert root_runner.run("shop.internal", "cat -- '/var/www/shop/a.css'")[0] == 126
    assert calls == []


def _openssh_blob(cipher: bytes) -> str:
    def ssh_string(data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + data

    blob = b"openssh-key-v1\x00" + ssh_string(cipher) + ssh_string(b"none") + ssh_string(b"")
    body = base64.b64encode(blob).decode("ascii")
    return "-----BEGIN OPENSSH PRIVATE KEY-----\n" + body + "\n-----END OPENSSH PRIVATE KEY-----\n"


def test_registry_values_are_not_pinned_and_the_path_stays_off_show(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    svc = _service(tmp_path)
    saved = _save(
        svc,
        files_root="/var/www/shop",
        ssh_host="stage.example.test",
        ssh_user="deploy",
        private_key=KEY,
        ssh_identity_file="",
        host_public_key=HOST_KEY,
    )
    assert saved.ok is True
    overlay = json.loads((tmp_path / "wp_ssh.json").read_text(encoding="utf-8"))
    row = overlay["sites"]["stage"]
    assert "files_root" not in row
    assert "ssh_host" not in row
    assert row["ssh_user"] == "deploy"
    assert row["host_public_key"] == HOST_KEY
    view = svc.ssh_settings("stage", actor="oscar").data["ssh"]
    assert view["files_root_source"] == "registry"
    assert view["ssh_host_source"] == "registry"
    assert view["ssh_key_stored"] is True
    assert view["identity_present"] is False
    assert view["host_key_fingerprint"].startswith("SHA256:")
    hidden = svc.ssh_settings("stage", actor="jonatan").data["ssh"]
    assert hidden["identity_present"] is False
    assert hidden["host_key_present"] is True
    assert hidden["host_public_key"] == ""
    assert hidden["host_key_fingerprint"] == ""
    shown = svc.show_site("stage", actor="jonatan")
    assert "ssh_identity_file" not in shown.data["site"]
    assert "host_public_key" not in shown.data["site"]
    assert shown.data["site"]["ssh_user"] == "deploy"
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert HOST_KEY.split()[1] not in audit
    assert MARKER not in audit


def test_one_bad_overlay_row_does_not_blank_the_other_site(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    registry = tmp_path / "wp_sites.yaml"
    other = _row("other")
    other["secret_ref"] = "WP_SECRET_OTHER"
    registry.write_text(yaml.safe_dump({"sites": [_row("stage"), other]}), encoding="utf-8")
    svc = WpOpsService(registry_path=registry, data_dir=tmp_path)
    payload = {
        "sites": {
            "stage": {"ssh_user": "deploy"},
            "other": {"files_root": f"/etc/{MARKER}"},
        }
    }
    (tmp_path / "wp_ssh.json").write_text(json.dumps(payload), encoding="utf-8")
    loaded = {site.id: site for site in load_sites(registry, data_dir=tmp_path)}
    assert loaded["stage"].ssh_user == "deploy"
    assert loaded["other"].files_root == "/var/www/shop"
    assert loaded["other"].ssh_user == ""
    view = svc.ssh_settings("other", actor="oscar")
    assert view.ok is True
    assert view.data["ssh"]["overlay_error"] == "SSH-överlagringen för sajten hoppades över."
    assert MARKER not in json.dumps(view.to_dict())
    stage = svc.ssh_settings("stage", actor="jonatan")
    assert stage.ok is True
    assert stage.data["ssh"]["overlay_error"] == ""
    assert stage.data["ssh"]["ssh_user"] == "deploy"


def test_broad_roots_are_rejected_without_storing_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    svc = _service(tmp_path)
    for root in ("/var/www", "/etc", "/var/log/nginx"):
        refused = _save(svc, files_root=root, private_key=KEY)
        assert refused.ok is False
        assert refused.message == "Rotmappen är för bred."
        assert MARKER not in refused.message
        assert not (tmp_path / "ssh-keys" / "stage").exists()
    kept = _save(svc, files_root="/var/www/html", private_key="")
    assert kept.ok is True
    loaded = load_sites(svc.registry_path, data_dir=tmp_path)
    assert loaded[0].files_root == "/var/www/html"


def test_encrypted_keys_are_refused_and_plain_openssh_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    svc = _service(tmp_path)
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "Proc-Type: 4,ENCRYPTED\n"
        "DEK-Info: AES-128-CBC,00000000000000000000000000000000\n\n"
        "AAAA\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    for text in (pem, _openssh_blob(b"aes256-ctr"), "-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n"):
        refused = _save(svc, private_key=text)
        assert refused.ok is False
        assert refused.message == "Den privata nyckeln godtogs inte."
        assert "ENCRYPTED" not in refused.message
        assert not (tmp_path / "ssh-keys" / "stage").exists()
    kept = _save(svc, private_key=_openssh_blob(b"none"))
    assert kept.ok is True
    assert "openssh-key-v1" not in kept.message
    stored = (tmp_path / "ssh-keys" / "stage").read_text(encoding="utf-8")
    assert "BEGIN OPENSSH PRIVATE KEY" in stored


def test_failed_overlay_write_leaves_the_old_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    svc = _service(tmp_path)
    assert _save(svc, private_key=KEY).ok is True
    key_path = tmp_path / "ssh-keys" / "stage"
    before = key_path.read_text(encoding="utf-8")

    def boom(self, *args, **kwargs):
        del self, args, kwargs
        raise OSError("disk")

    monkeypatch.setattr(Path, "write_text", boom)
    refused = _save(svc, private_key=KEY + "next\n", ssh_user="other")
    assert refused.ok is False
    assert refused.message == "SSH-inställningarna kunde inte sparas."
    assert "disk" not in refused.message
    assert key_path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "ssh-keys" / "stage.tmp").exists()


def test_concurrent_saves_keep_both_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    registry = tmp_path / "wp_sites.yaml"
    other = _row("other")
    other["secret_ref"] = "WP_SECRET_OTHER"
    registry.write_text(yaml.safe_dump({"sites": [_row("stage"), other]}), encoding="utf-8")
    svc = WpOpsService(registry_path=registry, data_dir=tmp_path)
    errors: list[str] = []

    def save(site_id: str, user: str) -> None:
        result = svc.save_ssh_settings(
            site_id,
            actor="oscar",
            files_root="",
            ssh_host="",
            ssh_user=user,
            ssh_port="",
            ssh_identity_file="",
            private_key="",
            clear_key=False,
            global_user="",
            global_port="",
            global_identity_file="",
            host_public_key="",
        )
        if not result.ok:
            errors.append(result.message)

    first = threading.Thread(target=save, args=("stage", "deploy"))
    second = threading.Thread(target=save, args=("other", "backup"))
    first.start()
    second.start()
    first.join()
    second.join()
    assert errors == []
    loaded = {site.id: site for site in load_sites(registry, data_dir=tmp_path)}
    assert loaded["stage"].ssh_user == "deploy"
    assert loaded["other"].ssh_user == "backup"


def test_pinned_host_key_is_written_before_ssh_and_a_bad_identity_is_distinct(tmp_path):
    called: list[list[str]] = []
    known = tmp_path / "ssh-known-hosts" / "stage"
    runner = OpenSshRunner(
        execute=lambda argv, stdin, timeout: called.append(argv) or (0, b""),
        user="deploy",
        port="2222",
        identity="/home/deploy/my keys/id",
        host_public_key=HOST_KEY,
        known_hosts=known,
    )
    code, out = runner.run("shop.internal", "cat -- '/var/www/shop/a.css'")
    assert (code, out) == (0, b"")
    assert called
    argv = called[0]
    assert "IdentitiesOnly=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "accept-new" not in argv
    assert f"UserKnownHostsFile={known}" in argv
    assert "/home/deploy/my keys/id" in argv
    text = known.read_text(encoding="utf-8")
    assert text.startswith("shop.internal " + HOST_KEY)
    assert "PRIVATE KEY" not in text
    rejected = OpenSshRunner(
        execute=lambda argv, stdin, timeout: called.append(argv) or (0, b""),
        identity="../keys/id",
        host_public_key=HOST_KEY,
        known_hosts=known,
    )
    with pytest.raises(TransportError) as exc:
        rejected.run("shop.internal", "cat -- '/var/www/shop/a.css'")
    assert exc.value.error_class == "SshIdentityRejected"
    assert len(called) == 1


def test_backup_script_excludes_private_keys_and_model_oauth():
    text = (ROOT / "bin" / "backup-azom-data.sh").read_text(encoding="utf-8")
    assert '--exclude="*/model_oauth"' in text
    assert '--exclude="*/model_oauth/*"' in text
    assert '--exclude="*/ssh-keys"' in text
    assert '--exclude="*/ssh-keys/*"' in text
    assert '--exclude="*/wp_ssh.lock"' in text
