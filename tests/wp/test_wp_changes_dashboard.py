"""Tests for Wave 4 Dashboard /wp/<site_id>/changes, /bridge/*, /ssh/deploy, and /deploy-live."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _helpers import (
    REPO_ROOT,
    clear_dashboard_password_env,
    dashboard_auth_headers,
    load_dashboard_app,
    passing_catalog,
    seed_mock_child_theme,
)

from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.change_ledger import ChangeLedger
from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme


def _setup_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed_fn=None):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "mock-staging-secret-1234567890")
    clear_dashboard_password_env(monkeypatch)
    clear_rbac_cache()
    if seed_fn is not None:
        seed_fn(tmp_path)
    app = load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_changes_dashboard_rbac_csp_verify_rollback_and_bridge_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_change: dict[str, str] = {}

    def _seed(data_dir: Path) -> None:
        row = ChangeLedger(data_dir=data_dir).record_change(
            site_id="mock-staging",
            environment="staging",
            category="product",
            transport="woo_rest",
            actor="oscar",
            summary="Prisjustering testprodukt",
            target_ref="product:501",
            before_data={"id": 501, "name": "Azom Pro Headset", "regular_price": "1990"},
            after_data={"id": 501, "name": "Azom Pro Headset", "regular_price": "2490"},
        )
        created_change["id"] = row["change_id"]
        created_change["before_sha256"] = row["before_sha256"]

    client = _setup_app(tmp_path, monkeypatch, seed_fn=_seed)
    cid = created_change["id"]

    jonatan = dashboard_auth_headers(client, "jonatan")
    oscar = dashboard_auth_headers(client, "oscar")

    # Viewer (Jonatan) can view list & detail, cannot rollback or flush cache
    list_resp = client.get("/wp/mock-staging/changes", headers=jonatan)
    assert list_resp.status_code == 200
    list_html = list_resp.get_data(as_text=True)
    assert cid in list_html
    assert "Återställ ändring" not in list_html

    detail_j = client.get(f"/wp/mock-staging/changes/{cid}", headers=jonatan)
    assert detail_j.status_code == 200
    assert "Återställ ändring" not in detail_j.get_data(as_text=True)

    assert client.post(f"/wp/mock-staging/changes/{cid}/rollback", headers=jonatan).status_code == 403
    assert client.post("/wp/mock-staging/bridge/cache-flush", headers=jonatan).status_code == 403
    assert client.get("/wp/mock-staging/bridge/download-zip", headers=jonatan).status_code == 403

    # Oscar can verify, inspect diff, check bridge health, flush cache, toggle maintenance, download ZIP, and rollback
    detail_o = client.get(f"/wp/mock-staging/changes/{cid}", headers=oscar)
    assert detail_o.status_code == 200
    detail_html = detail_o.get_data(as_text=True)
    assert "Återställ ändring" in detail_html
    # Strict CSP: no inline <script> without src=... and no onclick= handlers
    inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", detail_html, re.I)
    assert inline_scripts == []
    assert "onclick=" not in detail_html.lower()

    assert client.post("/wp/mock-staging/bridge/health", headers=oscar).status_code in (200, 302)
    assert client.post("/wp/mock-staging/bridge/cache-flush", headers=oscar).status_code in (200, 302)
    assert (
        client.post(
            "/wp/mock-staging/bridge/maintenance",
            data={"enabled": "1", "duration_seconds": "120"},
            headers=oscar,
        ).status_code
        in (200, 302)
    )
    zip_resp = client.get("/wp/mock-staging/bridge/download-zip", headers=oscar)
    assert zip_resp.status_code == 200
    assert zip_resp.data[:2] == b"PK"

    ver = client.post(f"/wp/mock-staging/changes/{cid}/verify", headers=oscar)
    assert ver.status_code in (200, 302)

    rb = client.post(
        f"/wp/mock-staging/changes/{cid}/rollback",
        data={"expected_before_sha256": created_change["before_sha256"]},
        headers=oscar,
    )
    assert rb.status_code in (200, 302)
    row_after = ChangeLedger(data_dir=tmp_path).get_change("mock-staging", cid)
    assert row_after is not None and row_after["status"] == "rolled_back"


def test_ssh_deploy_settings_route_and_review_deploy_live_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta: dict[str, str] = {}
    monkeypatch.setattr(
        "ecom_ops.wp_ops.review_plan.run_catalog",
        passing_catalog,
    )

    def _seed(data_dir: Path) -> None:
        seed_mock_child_theme(data_dir, site_id="mock-staging", slug="azom-child")
        plans = ReviewPlans(data_dir=data_dir, registry_path=REPO_ROOT / "config" / "wp_sites.mock.yaml")
        opened = plans.open(
            "mock-staging",
            "theme",
            "azom-child",
            actor="oscar",
            summary="Dashboard deploy-live test",
            verified_theme=VerifiedTheme("azom-child", "parent-base"),
        )
        assert opened.ok is True
        pid = opened.data["plan_id"]
        plans.patch_file(
            "mock-staging",
            pid,
            "style.css",
            ".btn { background: #111; color: #fff; }",
            ".btn { background: #222; color: #fff; }",
            actor="oscar",
        )
        checked = plans.run_checks("mock-staging", pid, actor="oscar")
        assert checked.ok is True
        row = plans.get(pid, site_id="mock-staging")
        assert row is not None
        meta["plan_id"] = pid
        meta["base_sha"] = row["base_sha256"]
        meta["work_sha"] = row["work_sha256"]

    client = _setup_app(tmp_path, monkeypatch, seed_fn=_seed)
    jonatan = dashboard_auth_headers(client, "jonatan")
    oscar = dashboard_auth_headers(client, "oscar")

    # Configure dedicated *-wp-deployer SSH overlay via dashboard
    assert client.post("/wp/mock-staging/ssh/deploy", data={}, headers=jonatan).status_code == 403
    ssh_resp = client.post(
        "/wp/mock-staging/ssh/deploy",
        data={
            "deploy_ssh_user": "azom-wp-deployer",
            "files_root": "/var/www/staging",
            "ssh_host": "staging.example.test",
            "ssh_port": "22",
        },
        headers=oscar,
    )
    assert ssh_resp.status_code in (200, 302)
    assert (tmp_path / "wp_deploy_ssh.json").is_file()

    # Legacy POST .../production stays refused even with valid hashes
    prod_refused = client.post(
        f"/wp/mock-staging/reviews/{meta['plan_id']}/production",
        data={"base_sha256": meta["base_sha"], "work_sha256": meta["work_sha"]},
        headers=oscar,
    )
    assert prod_refused.status_code == 200
    assert "BackupNotVerified" in prod_refused.get_data(as_text=True)

    # New /deploy-live works via mock Bridge + records ChangeLedger entry
    dl_resp = client.post(
        f"/wp/mock-staging/reviews/{meta['plan_id']}/deploy-live",
        data={
            "base_sha256": meta["base_sha"],
            "work_sha256": meta["work_sha"],
            "transport_mode": "bridge",
        },
        headers=oscar,
    )
    assert dl_resp.status_code == 200
    assert ChangeLedger(data_dir=tmp_path).list_changes("mock-staging")


def test_stage2_deploy_ssh_host_key_preservation_and_changes_filters_and_rollback_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressions for U1-08 (deploy host_public_key preservation), U1-14 (category filter + non-rollbackable guards), U1-16 (SEO link)."""
    import base64
    import json

    created: dict[str, str] = {}

    def _seed(data_dir: Path) -> None:
        ledger = ChangeLedger(data_dir=data_dir)
        draft_row = ledger.record_change(
            site_id="mock-staging",
            environment="staging",
            category="page_draft",
            transport="wp_rest",
            actor="oscar",
            summary="Sidutkast test",
            target_ref="page:99",
            before_data={},
            after_data={"id": 99, "status": "draft"},
        )
        created["draft_id"] = draft_row["change_id"]
        prod_row = ledger.record_change(
            site_id="mock-staging",
            environment="staging",
            category="seo_meta",
            transport="bridge",
            actor="oscar",
            summary="SEO meta uppdatering",
            target_ref="product:501",
            before_data={"meta_title": "Före"},
            after_data={"meta_title": "Efter"},
        )
        created["seo_id"] = prod_row["change_id"]
        created["seo_sha"] = prod_row["before_sha256"]

    client = _setup_app(tmp_path, monkeypatch, seed_fn=_seed)
    jonatan = dashboard_auth_headers(client, "jonatan")
    oscar = dashboard_auth_headers(client, "oscar")

    # U1-14 & U1-16: wp_changes has seo_meta, media_alt, maintenance options + SEO Studio link
    list_html = client.get("/wp/mock-staging/changes", headers=oscar).get_data(as_text=True)
    for cat in ("seo_meta", "media_alt", "maintenance"):
        assert f'value="{cat}"' in list_html
    assert "/seo?site=mock-staging" in list_html

    # U1-14: page_draft hides rollback section on detail view
    draft_html = client.get(
        f"/wp/mock-staging/changes/{created['draft_id']}", headers=oscar
    ).get_data(as_text=True)
    assert "Återställ ändring" not in draft_html

    # U1-14: when kill switch blocks rollback (can_rollback=False), execute button is disabled
    monkeypatch.setenv("AZOM_WP_DEPLOY_KILL", "1")
    blocked_html = client.get(
        f"/wp/mock-staging/changes/{created['seo_id']}", headers=oscar
    ).get_data(as_text=True)
    assert "disabled aria-disabled=\"true\"" in blocked_html
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # U1-08: deploy host_public_key is stored, shown to Oscar (hidden from Jonatan), and preserved when omitted
    hk_blob = base64.b64encode(b"azom-deploy-host-key-1234").decode("ascii")
    deploy_hk = f"ssh-ed25519 {hk_blob} deploy-test"
    save1 = client.post(
        "/wp/mock-staging/ssh/deploy",
        data={
            "deploy_ssh_user": "azom-wp-deployer",
            "files_root": "/var/www/staging",
            "ssh_host": "staging.example.test",
            "ssh_port": "22",
            "host_public_key": deploy_hk,
        },
        headers=oscar,
    )
    assert save1.status_code in (200, 302)
    stored1 = json.loads((tmp_path / "wp_deploy_ssh.json").read_text(encoding="utf-8"))
    assert stored1["sites"]["mock-staging"]["host_public_key"] == deploy_hk

    # Oscar sees pre-populated textarea; Jonatan does not
    osc_ssh_html = client.get("/wp/mock-staging/ssh", headers=oscar).get_data(as_text=True)
    assert 'id="deploy_host_public_key"' in osc_ssh_html
    assert hk_blob in osc_ssh_html
    jon_ssh_html = client.get("/wp/mock-staging/ssh", headers=jonatan).get_data(as_text=True)
    assert hk_blob not in jon_ssh_html

    # Re-saving without host_public_key preserves the stored key (does NOT wipe it)
    save2 = client.post(
        "/wp/mock-staging/ssh/deploy",
        data={
            "deploy_ssh_user": "azom-wp-deployer",
            "files_root": "/var/www/staging",
            "ssh_host": "staging.example.test",
            "ssh_port": "2222",
        },
        headers=oscar,
    )
    assert save2.status_code in (200, 302)
    stored2 = json.loads((tmp_path / "wp_deploy_ssh.json").read_text(encoding="utf-8"))
    assert stored2["sites"]["mock-staging"]["host_public_key"] == deploy_hk
    assert stored2["sites"]["mock-staging"]["ssh_port"] == 2222


def test_changes_batch_id_filter_batch_rollback_and_plugins_funnel_canary_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proposal 1 & 5: ?batch_id= filter + Oscar batch rollback + Woo funnel canary display on wp_plugins."""
    from ecom_ops.wp_ops.plugin_service import PluginOpsService

    batch_id = "seob-abcdef123456"

    def _seed(data_dir: Path) -> None:
        ledger = ChangeLedger(data_dir=data_dir)
        ledger.record_change(
            site_id="mock-staging",
            environment="staging",
            category="seo_meta",
            transport="bridge",
            actor="oscar",
            summary="Batch meta 1",
            target_ref="product:501",
            before_data={"meta_title": "Old 1"},
            after_data={"meta_title": "New 1"},
            batch_id=batch_id,
        )
        ledger.record_change(
            site_id="mock-staging",
            environment="staging",
            category="media_alt",
            transport="bridge",
            actor="oscar",
            summary="Batch alt 2",
            target_ref="media:101",
            before_data={"alt_text": ""},
            after_data={"alt_text": "CarPlay skärm Volvo"},
            batch_id=batch_id,
        )
        ledger.record_change(
            site_id="mock-staging",
            environment="staging",
            category="product",
            transport="woo_rest",
            actor="oscar",
            summary="Unrelated change",
            target_ref="product:502",
            before_data={"regular_price": "100"},
            after_data={"regular_price": "120"},
        )
        # Plan and execute a plugin update so canary_paths & canary_results appear on /wp/mock-staging/plugins
        psvc = PluginOpsService(data_dir=data_dir)
        planned = psvc.plan_plugin_updates(
            "mock-staging",
            actor="oscar",
            selection_mode="all",
            include_critical=True,
        )
        assert planned.ok is True
        plu_id = planned.data["plan_id"]
        plu_sha = planned.data["plan_sha256"]
        executed = psvc.execute_plugin_update_plan(
            "mock-staging",
            plu_id,
            actor="oscar",
            expected_sha256=plu_sha,
            allow_critical=True,
        )
        assert executed.ok is True

    client = _setup_app(tmp_path, monkeypatch, seed_fn=_seed)
    jonatan = dashboard_auth_headers(client, "jonatan")
    oscar = dashboard_auth_headers(client, "oscar")

    # 1. ?batch_id= filters changes table and shows Batch Rollback panel
    filtered_j = client.get(f"/wp/mock-staging/changes?batch_id={batch_id}", headers=jonatan)
    assert filtered_j.status_code == 200
    f_html_j = filtered_j.get_data(as_text=True)
    assert "Batch meta 1" in f_html_j
    assert "Batch alt 2" in f_html_j
    assert "Unrelated change" not in f_html_j
    assert "Återställ hela batchen (Oscar)" not in f_html_j

    # Jonatan denied batch rollback -> 403
    assert (
        client.post(
            f"/wp/mock-staging/changes/batch/{batch_id}/rollback",
            data={"confirm_batch_id": batch_id},
            headers=jonatan,
        ).status_code
        == 403
    )

    # Oscar sees batch rollback button and executes it
    filtered_o = client.get(f"/wp/mock-staging/changes?batch_id={batch_id}", headers=oscar)
    assert filtered_o.status_code == 200
    assert "Återställ hela batchen (Oscar)" in filtered_o.get_data(as_text=True)

    rb_o = client.post(
        f"/wp/mock-staging/changes/batch/{batch_id}/rollback",
        data={"confirm_batch_id": batch_id},
        headers=oscar,
    )
    assert rb_o.status_code in (200, 302)
    rows_after = ChangeLedger(data_dir=tmp_path).list_batch_changes(batch_id, site_id="mock-staging")
    assert len(rows_after) == 2
    assert all(r["status"] == "rolled_back" for r in rows_after)

    # 2. /wp/mock-staging/plugins renders WooCommerce funnel canary paths & results
    plugins_resp = client.get("/wp/mock-staging/plugins", headers=oscar)
    assert plugins_resp.status_code == 200
    p_html = plugins_resp.get_data(as_text=True)
    assert 'data-canary-paths="true"' in p_html
    assert 'data-canary-results="true"' in p_html
    assert "/shop/" in p_html
    assert "/cart/" in p_html
    assert "/checkout/" in p_html

