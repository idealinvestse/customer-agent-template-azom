"""Tests for Wave 3 SshDeployTransport (*-wp-deployer) & DeployOrchestrator."""

from __future__ import annotations

import io
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _helpers import passing_catalog, seed_mock_child_theme

from ecom_ops.integrations.wp.sftp_deploy_transport import (
    SshDeployTransport,
    deploy_ssh_enabled,
)
from ecom_ops.integrations.wp.sftp_transport import SshFileTransport
from ecom_ops.wp_ops.deploy_orchestrator import DeployOrchestrator
from ecom_ops.wp_ops.files import TransportError
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme
from ecom_ops.wp_ops.ssh_settings import load_overlay, save_deploy_ssh


def _site(site_id: str = "mock-staging", environment: str = "staging") -> Site:
    return Site(
        id=site_id,
        display_name=site_id,
        canonical_url="https://staging.example.test/",
        environment=environment,
        business_owner="customer",
        hosting_provider="unconfigured",
        wordpress_version="6.7",
        php_version="8.3",
        woocommerce=True,
        critical_urls=("https://staging.example.test/",),
        maintenance_window="not agreed",
        secret_ref="WP_SECRET_MOCK_STAGING",
        alert_owner="oscar",
        alert_confirmations=2,
        check_interval_seconds=60,
        timeout_seconds=10,
        dedupe_window_seconds=30,
        retention_days=7,
        capabilities={},
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
    )


class _FakeSftpHandle(io.BytesIO):
    def __init__(self, store: dict[str, bytes], path: str, mode: str) -> None:
        self._store = store
        self._path = path
        self._mode = mode
        initial = store.get(path, b"") if "r" in mode else b""
        super().__init__(initial)

    def close(self) -> None:
        if "w" in self._mode:
            self._store[self._path] = self.getvalue()
        super().close()

    def __exit__(self, *args: Any) -> None:
        self.close()


class _FakeSftpClient:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.dirs: set[str] = {"/var", "/var/www", "/var/www/staging", "/var/www/staging/wp-content"}

    def lstat(self, path: str) -> SimpleNamespace:
        if path in self.files:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)
        if path in self.dirs or any(k.startswith(path.rstrip("/") + "/") for k in self.files):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
        raise FileNotFoundError(path)

    def mkdir(self, path: str, mode: int = 0o755) -> None:
        del mode
        self.dirs.add(path)

    def open(self, path: str, mode: str) -> _FakeSftpHandle:
        if "r" in mode and path not in self.files:
            raise FileNotFoundError(path)
        return _FakeSftpHandle(self.files, path, mode)

    def posix_rename(self, src: str, dst: str) -> None:
        self.files[dst] = self.files.pop(src)

    def remove(self, path: str) -> None:
        self.files.pop(path, None)


def test_read_only_ssh_transport_still_raises_ssh_write_disabled(tmp_path: Path) -> None:
    site = _site()
    reader = SshFileTransport(site, tmp_path)
    with pytest.raises(TransportError, match="SshWriteDisabled"):
        reader.write_bytes("/var/www/staging/wp-content/themes/azom-child/style.css", b"x")
    with pytest.raises(TransportError, match="SshWriteDisabled"):
        reader.create_bytes("/var/www/staging/wp-content/themes/azom-child/new.css", b"x")


def test_deploy_ssh_settings_isolated_and_fail_closed(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    site = _site("azom-no", environment="production")

    # Cannot reuse *-wp-reader account name
    with pytest.raises(Exception, match="wp-deployer"):
        save_deploy_ssh(
            tmp_path,
            site,
            deploy_ssh_user="azom-wp-reader",
            files_root="/var/www/shop",
            ssh_host="shop.example.test",
            ssh_port="22",
        )

    saved = save_deploy_ssh(
        tmp_path,
        site,
        deploy_ssh_user="azom-wp-deployer",
        files_root="/var/www/shop",
        ssh_host="shop.example.test",
        ssh_port="22",
        host_public_key="ssh-ed25519 bW9jay1lZDI1NTE5LWhvc3Qta2V5LWJ5dGVzLTMy",
        private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nMOCK\n-----END OPENSSH PRIVATE KEY-----\n",
    )
    assert saved["deploy_key_stored"] is True
    assert (tmp_path / "ssh-deploy-keys" / "azom-no").is_file()
    # wp_ssh.json (read-only settings) remains untouched
    assert load_overlay(tmp_path) == {}

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_DEPLOY_SSH", raising=False)
    assert deploy_ssh_enabled() is False

    tr = SshDeployTransport(site, tmp_path)
    with pytest.raises(TransportError, match="SshDeployDisabled"):
        tr.atomic_apply_bundle(
            [("write", "/var/www/shop/wp-content/themes/azom-child/style.css", b"body{}")]
        )


def test_sftp_deploy_atomic_bundle_and_auto_rollback_on_health_failure(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    site = _site("mock-staging")
    save_deploy_ssh(
        tmp_path,
        site,
        deploy_ssh_user="azom-wp-deployer",
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
        ssh_port="22",
    )
    target = "/var/www/staging/wp-content/themes/azom-child/style.css"
    fake_sftp = _FakeSftpClient({target: b"/* old */"})

    tr = SshDeployTransport(site, tmp_path)

    @contextmanager
    def _fake_conn():
        yield fake_sftp

    monkeypatch.setattr(tr, "_session", _fake_conn)

    # Failing health check triggers automatic SFTP rollback to original bytes
    with pytest.raises(TransportError, match="PostDeployHealthFailed"):
        tr.atomic_apply_bundle(
            [("write", target, b"/* broken */")],
            health_check_fn=lambda: False,
        )
    assert fake_sftp.files[target] == b"/* old */"

    # Healthy deploy persists new bytes
    res = tr.atomic_apply_bundle(
        [("write", target, b"/* good */")],
        health_check_fn=lambda: True,
    )
    assert res["ok"] is True
    assert fake_sftp.files[target] == b"/* good */"


def test_deploy_orchestrator_snapshots_and_live_gate(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    seed_mock_child_theme(tmp_path, site_id="mock-staging", slug="azom-child")
    monkeypatch.setattr(
        "ecom_ops.wp_ops.review_plan.run_catalog",
        passing_catalog,
    )

    plans = ReviewPlans(data_dir=tmp_path)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Uppdatera style.css via orchestrator",
        verified_theme=VerifiedTheme("azom-child", "parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    patched = plans.patch_file(
        "mock-staging",
        plan_id,
        "style.css",
        ".btn { background: #111; color: #fff; }",
        ".btn { background: #222; color: #fff; }",
        actor="oscar",
    )
    assert patched.ok is True
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok is True
    row = plans.get(plan_id, site_id="mock-staging")
    assert row is not None
    base_sha = row["base_sha256"]
    work_sha = row["work_sha256"]

    orch = DeployOrchestrator(data_dir=tmp_path)
    # Bridge deploy succeeds cleanly in mock mode
    ok_res = orch.deploy_workspace(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=base_sha,
        work_sha256=work_sha,
        transport_mode="bridge",
        health_check_fn=lambda: True,
    )
    assert ok_res.ok is True
    assert ok_res.data["change_id"].startswith("chg-")

    # Live mode requires AZOM_WP_LIVE_DEPLOY_ENABLED=1
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_LIVE_DEPLOY_ENABLED", raising=False)
    blocked = orch.deploy_workspace(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=base_sha,
        work_sha256=work_sha,
    )
    assert blocked.ok is False and blocked.data["error_class"] == "LiveDeployDisabled"


def test_deploy_orchestrator_state_guard_zero_byte_snapshot_and_health_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
    from ecom_ops.wp_ops.review_workspace import tree_hash, workspace_root, write_diff

    root_dir = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "secret-staging")
    seed_mock_child_theme(tmp_path, site_id="mock-staging")

    plans = ReviewPlans(data_dir=tmp_path, registry_path=root_dir / "config" / "wp_sites.mock.yaml")
    plan_file = tmp_path / "wp_review_plans.json"

    # V-03: draft / checks_incomplete / deployed plans must be rejected
    for bad_status in ("draft", "checks_incomplete", "deployed"):
        opened = plans.open(
            "mock-staging",
            "theme",
            "azom-child",
            actor="oscar",
            verified_theme=VerifiedTheme("azom-child", "twentytwentyfive"),
        ).data
        bad_id = opened["plan_id"]
        bad_base = ""
        bad_work = ""
        stored = _json.loads(plan_file.read_text(encoding="utf-8"))
        for row in stored["plans"]:
            if row["plan_id"] == bad_id:
                row["status"] = bad_status
                bad_base = str(row.get("base_sha256") or "")
                bad_work = str(row.get("work_sha256") or "")
        plan_file.write_text(_json.dumps(stored), encoding="utf-8")

        orch_bad = DeployOrchestrator(data_dir=tmp_path, registry_path=root_dir / "config" / "wp_sites.mock.yaml")
        bad_res = orch_bad.deploy_workspace(
            "mock-staging",
            bad_id,
            actor="oscar",
            base_sha256=bad_base,
            work_sha256=bad_work,
            transport_mode="bridge",
        )
        assert bad_res.ok is False
        assert bad_res.data["error_class"] in {"PlanNotReady", "BackupNotVerified", "AlreadyDeployed", "TestsNotPassed"}

    # V-16 + V-32: 0-byte live file is captured cleanly & health_check_fn failure rolls back
    opened_ok = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        verified_theme=VerifiedTheme("azom-child", "twentytwentyfive"),
    ).data
    plan_id = opened_ok["plan_id"]
    ws = workspace_root(tmp_path, "mock-staging", plan_id)
    (ws / "base" / "empty.css").write_bytes(b"")
    (ws / "work" / "empty.css").write_bytes(b"/* non-empty */")
    new_base_sha = tree_hash(ws / "base")
    new_work_sha = tree_hash(ws / "work")
    write_diff(ws, "--- a/empty.css\n+++ b/empty.css\n+ /* non-empty */\n")

    from ecom_ops.wp_ops.review import checks as review_checks

    all_work = [
        (
            str(fp.relative_to(ws / "work")).replace("\\", "/"),
            fp.read_text(encoding="utf-8", errors="replace"),
        )
        for fp in sorted((ws / "work").rglob("*"))
        if fp.is_file()
    ]
    needed_pol = review_checks.requirements("theme", all_work, "low")
    chk_rows = passing_catalog("theme", all_work, "low")
    run_meta = {
        "run_id": "chk-empty001",
        "site_id": "mock-staging",
        "plan_id": plan_id,
        "base_sha256": new_base_sha,
        "work_sha256": new_work_sha,
        "profile": review_checks.PROFILE,
        "profile_version": review_checks.PROFILE_VERSION,
        "policy": needed_pol,
        "published": True,
    }
    for chk in chk_rows:
        job_name = str(chk.get("job") or "")
        if needed_pol.get(job_name) == "required":
            chk.update(
                {
                    "run_id": "chk-empty001",
                    "policy": "required",
                    "site_id": "mock-staging",
                    "plan_id": plan_id,
                    "base_sha256": new_base_sha,
                    "work_sha256": new_work_sha,
                    "profile": review_checks.PROFILE,
                    "profile_version": review_checks.PROFILE_VERSION,
                }
            )
    stored = _json.loads(plan_file.read_text(encoding="utf-8"))
    for row in stored["plans"]:
        if row["plan_id"] == plan_id:
            row["status"] = "tests_passed"
            row["base_sha256"] = new_base_sha
            row["work_sha256"] = new_work_sha
            row["files"] = ["empty.css"]
            row["checks"] = chk_rows
            row["check_run"] = run_meta
    plan_file.write_text(_json.dumps(stored), encoding="utf-8")

    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    tr.files["theme/azom-child/empty.css"] = b""
    bridge = WpBridgeClient(site_id="mock-staging", transport=tr)

    orch = DeployOrchestrator(data_dir=tmp_path, registry_path=root_dir / "config" / "wp_sites.mock.yaml")
    failed_health = orch.deploy_workspace(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=new_base_sha,
        work_sha256=new_work_sha,
        transport_mode="bridge",
        bridge_client=bridge,
        health_check_fn=lambda: False,
    )
    assert failed_health.ok is False
    assert failed_health.data["error_class"] == "PostDeployHealthFailed"
    assert tr.files["theme/azom-child/empty.css"] == b""

    res = orch.deploy_workspace(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=new_base_sha,
        work_sha256=new_work_sha,
        transport_mode="bridge",
        bridge_client=bridge,
        health_check_fn=lambda: True,
    )
    assert res.ok is True
    assert tr.files["theme/azom-child/empty.css"] == b"/* non-empty */"
    # Second deploy on same already-deployed plan is rejected
    redeploy = orch.deploy_workspace(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=new_base_sha,
        work_sha256=new_work_sha,
        transport_mode="bridge",
        bridge_client=bridge,
    )
    assert redeploy.ok is False


def test_sftp_deploy_transport_rejects_symlinks_existing_create_and_oversized(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)
    site = _site("mock-staging", "production")
    save_deploy_ssh(
        tmp_path,
        site,
        deploy_ssh_user="azom-wp-deployer",
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
        ssh_port="22",
    )

    class _SymlinkAndChmodSftp(_FakeSftpClient):
        def __init__(self, files: dict[str, bytes] | None = None) -> None:
            super().__init__(files)
            self.chmod_calls: list[tuple[str, int]] = []
            self.symlink_paths: set[str] = set()

        def lstat(self, path: str) -> SimpleNamespace:
            if path in self.symlink_paths:
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
            return super().lstat(path)

        def chmod(self, path: str, mode: int) -> None:
            self.chmod_calls.append((path, mode))

    style_path = "/var/www/staging/wp-content/themes/azom-child/style.css"
    existing_path = "/var/www/staging/wp-content/themes/azom-child/existing.css"
    link_path = "/var/www/staging/wp-content/themes/azom-child/link.css"
    fake_sftp = _SymlinkAndChmodSftp({style_path: b"/* orig */", existing_path: b"/* exists */"})
    fake_sftp.symlink_paths.add(link_path)

    tr = SshDeployTransport(site, tmp_path)

    @contextmanager
    def _fake_conn():
        yield fake_sftp

    monkeypatch.setattr(tr, "_session", _fake_conn)

    # V-18: symlink blocked
    from ecom_ops.wp_ops.file_paths import PathRejected

    with pytest.raises(PathRejected, match="symlink"):
        tr.atomic_apply_bundle([("write", link_path, b"x")])

    # V-16: op="create" fails closed if file already exists
    with pytest.raises(TransportError) as exists_exc:
        tr.atomic_apply_bundle([("create", existing_path, b"overwrite")])
    assert exists_exc.value.error_class == "FileExistsConflict"

    # V-18: oversized payload rejected before write
    with pytest.raises(TransportError) as big_exc:
        tr.atomic_apply_bundle([("write", style_path, b"x" * (600 * 1024))])
    assert big_exc.value.error_class == "FileTooLarge"

    # Mode preserved via chmod on successful write
    ok = tr.atomic_apply_bundle([("write", style_path, b"/* new */")])
    assert ok["ok"] is True
    assert fake_sftp.chmod_calls


def test_b1_04_b1_10_b1_11_u1_08_stale_file_kex_pinning_key_reuse_symlinks_and_overlay(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import json

    import paramiko

    from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
    from ecom_ops.integrations.wp.sftp_transport import _pinned_host_key_types
    from ecom_ops.wp_ops.file_paths import PathRejected, sha256_digest
    from ecom_ops.wp_ops.ssh_settings import (
        deploy_overlay_file,
        load_deploy_overlay,
        stored_key_path,
    )

    wp_env(mock="1", files_ssh="0")

    # B1-10: RSA host key pins rsa-sha2-512 / rsa-sha2-256 / ssh-rsa; Ed25519 pins [ssh-ed25519]
    rsa_fake = SimpleNamespace(get_name=lambda: "ssh-rsa")
    ed_fake = SimpleNamespace(get_name=lambda: "ssh-ed25519")
    assert _pinned_host_key_types(rsa_fake) == ["rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"]
    assert _pinned_host_key_types(ed_fake) == ["ssh-ed25519"]

    # B1-04: Bridge DeployOrchestrator fails closed with StaleFile if live bytes diverged from workspace base/
    seed_mock_child_theme(tmp_path, site_id="mock-staging", slug="azom-child")
    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", passing_catalog)
    plans = ReviewPlans(data_dir=tmp_path)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Stale check",
        verified_theme=VerifiedTheme("azom-child", "parent-base"),
    )
    pid = opened.data["plan_id"]
    assert plans.patch_file(
        "mock-staging",
        pid,
        "style.css",
        ".btn { background: #111; color: #fff; }",
        ".btn { background: #333; color: #fff; }",
        actor="oscar",
    ).ok
    assert plans.run_checks("mock-staging", pid, actor="oscar").ok
    row = plans.get(pid, site_id="mock-staging")
    assert row is not None

    drifted_tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    drifted_tr.files["theme/azom-child/style.css"] = b"/* concurrent live edit in wp-admin */\n"
    orch = DeployOrchestrator(data_dir=tmp_path)
    stale_res = orch.deploy_workspace(
        "mock-staging",
        pid,
        actor="oscar",
        base_sha256=row["base_sha256"],
        work_sha256=row["work_sha256"],
        transport_mode="bridge",
        bridge_client=WpBridgeClient(site_id="mock-staging", transport=drifted_tr),
    )
    assert stale_res.ok is False
    assert stale_res.data["error_class"] == "StaleFile"
    assert drifted_tr.files["theme/azom-child/style.css"] == b"/* concurrent live edit in wp-admin */\n"

    # B1-04 + B1-11: SFTP atomic_apply_bundle verifies expected_before_hashes, re-raises non-ENOENT in _ensure_remote_dir, and checks symlinks on delete/rollback
    site = _site("mock-staging")
    target = "/var/www/staging/wp-content/themes/azom-child/style.css"
    fake_sftp = _FakeSftpClient({target: b"/* live drifted */"})
    sftp_tr = SshDeployTransport(site, tmp_path)

    @contextmanager
    def _fake_conn():
        yield fake_sftp

    monkeypatch.setattr(sftp_tr, "_session", _fake_conn)
    with pytest.raises(TransportError) as stale_exc:
        sftp_tr.atomic_apply_bundle(
            [("write", target, b"/* new */")],
            expected_before_hashes={target: sha256_digest(b"/* old base */")},
        )
    assert stale_exc.value.error_class == "StaleFile"

    # B1-11: _ensure_remote_dir re-raises EACCES instead of swallowing it
    class _EaccesSftp(_FakeSftpClient):
        def lstat(self, path: str) -> SimpleNamespace:
            if path.endswith("azom-child"):
                raise OSError(errno.EACCES, "Permission denied")
            return super().lstat(path)

    with pytest.raises(OSError) as eacces_exc:
        SshDeployTransport._ensure_remote_dir(_EaccesSftp(), "/var/www/staging/wp-content/themes/azom-child")
    assert eacces_exc.value.errno == errno.EACCES

    # B1-11: delete & rollback_bundle reject symlinked leaf/ancestor
    class _SymSftp(_FakeSftpClient):
        def lstat(self, path: str) -> SimpleNamespace:
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
            return super().lstat(path)

    sym_sftp = _SymSftp({target: b"x"})
    monkeypatch.setattr(sftp_tr, "_session", contextmanager(lambda: (yield sym_sftp)))
    with pytest.raises(PathRejected):
        sftp_tr.atomic_apply_bundle([("delete", target, None)])
    with pytest.raises(PathRejected):
        sftp_tr.rollback_bundle({}, [target])

    # B1-11: _load_deploy_key uses _key_identity_digest (catches reformatted copy of reader key)
    rsa_key = paramiko.RSAKey.generate(2048)
    buf1 = io.StringIO()
    rsa_key.write_private_key(buf1)
    pem1 = buf1.getvalue()
    # Add extra trailing whitespace / blank lines so raw SHA-256 differs even if stripped differently or with comment lines
    reader_path = stored_key_path(tmp_path, site.id)
    reader_path.parent.mkdir(parents=True, exist_ok=True)
    reader_path.write_text(pem1, encoding="utf-8")

    deploy_path = tmp_path / "ssh-deploy-keys" / site.id
    deploy_path.parent.mkdir(parents=True, exist_ok=True)
    # Different textual representation (CRLF + trailing newline) of the same private key
    deploy_path.write_bytes(pem1.replace("\n", "\r\n").encode("utf-8"))
    with pytest.raises(TransportError) as reuse_exc:
        sftp_tr._load_deploy_key()
    assert reuse_exc.value.error_class == "SshDeployKeyReuseRefused"

    # B1-17 + U1-08 part 1: load_deploy_overlay validates schema & save_deploy_ssh preserves host_public_key unless cleared
    deploy_overlay_file(tmp_path).write_text(
        json.dumps({"sites": {"mock-staging": {"ssh_port": "not-an-int", "unknown_key": 1}}}),
        encoding="utf-8",
    )
    assert load_deploy_overlay(tmp_path) == {}
    # SshDeployTransport.__init__ does not crash on malformed overlay
    _ = SshDeployTransport(site, tmp_path)

    hk = "ssh-ed25519 bW9jay1lZDI1NTE5LWhvc3Qta2V5LWJ5dGVzLTMy"
    save_deploy_ssh(
        tmp_path,
        site,
        deploy_ssh_user="azom-wp-deployer",
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
        ssh_port="22",
        host_public_key=hk,
    )
    # Saving again without host_public_key preserves it
    save_deploy_ssh(
        tmp_path,
        site,
        deploy_ssh_user="azom-wp-deployer",
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
        ssh_port="2222",
    )
    ov_preserved = load_deploy_overlay(tmp_path)["mock-staging"]
    assert ov_preserved["host_public_key"] == hk
    assert ov_preserved["ssh_port"] == 2222

    # Explicitly clearing removes host_public_key
    save_deploy_ssh(
        tmp_path,
        site,
        deploy_ssh_user="azom-wp-deployer",
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
        ssh_port="2222",
        clear_host_key=True,
    )
    assert "host_public_key" not in load_deploy_overlay(tmp_path)["mock-staging"]
