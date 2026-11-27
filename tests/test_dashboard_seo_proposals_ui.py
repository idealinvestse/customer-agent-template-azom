"""Tests for Proposals 1-6 Dashboard SEO Studio UI, Batch Rollback, Link Lifecycle, and Compact CLI."""

from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"

from test_dashboard_seo_ui import (  # noqa: E402
    _assert_strict_csp,
    _basic_auth,
    _extract_csrf,
    _load_dashboard_app,
)


@pytest.fixture(autouse=True)
def _no_live_sockets(monkeypatch):
    def _blocked_connect(*_args, **_kwargs):
        raise AssertionError("Live network socket connection attempted in offline test")

    monkeypatch.setattr(socket, "create_connection", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)


@pytest.fixture()
def proposals_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jon-pw")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "osc-pw")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-seo-proposals-secret-key")
    monkeypatch.setenv("AZOM_DISABLE_BACKGROUND_WORKERS", "1")
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    app = _load_dashboard_app()
    return (
        app.test_client(),
        _basic_auth("jonatan", "jon-pw"),
        _basic_auth("oscar", "osc-pw"),
        tmp_path / "data",
    )


def test_batch_fix_plans_serp_preview_filters_diff_and_batch_rollback_ui(proposals_client):
    """Proposal 1 & 6: 1-click batch creation, SERP preview, issue filters, Oscar apply, and batch rollback."""
    client, jonatan, oscar, data_dir = proposals_client
    from ecom_ops.wp_ops.change_ledger import ChangeLedger

    # 1. Jonatan runs 2 audits so Audit Trend Delta strip populates
    hub1 = client.get("/seo?site=mock-staging", headers=jonatan)
    assert hub1.status_code == 200
    hub1_html = _assert_strict_csp(hub1, "/seo")
    csrf_j = _extract_csrf(hub1_html)
    assert "data-seo-site-switcher" in hub1_html
    assert "/wp/mock-staging/plugins" in hub1_html
    assert "/wp/mock-staging/changes?category=seo_meta" in hub1_html
    assert "Senaste SEO-ändringar i Change Vault" in hub1_html

    for _ in range(2):
        r_aud = client.post(
            "/seo/audit",
            headers=jonatan,
            data={
                "_csrf": csrf_j,
                "site_id": "mock-staging",
                "market": "no",
                "scope": "site",
                "path": "/",
            },
            follow_redirects=False,
        )
        assert r_aud.status_code == 302
        audit_url = r_aud.headers["Location"]

    audit_id = audit_url.rsplit("/", 1)[-1]
    det = client.get(audit_url, headers=jonatan)
    assert det.status_code == 200
    det_html = _assert_strict_csp(det, audit_url)
    # 1-click batch buttons, SERP preview, interactive filters & Audit Diff strip
    assert "Skapa bulk-alt-batch" in det_html
    assert "Skapa meta-batch" in det_html
    assert 'data-serp-preview="1"' in det_html
    assert 'data-serp-mode="desktop"' in det_html
    assert 'data-serp-mode="mobile"' in det_html
    assert 'id="seo-serp-title-meter"' in det_html
    assert 'id="seo-serp-desc-meter"' in det_html
    assert 'data-seo-issue-filter="critical"' in det_html
    assert 'data-seo-issue-filter="media"' in det_html
    assert 'data-seo-next-issue="1"' in det_html
    assert 'id="seo-issue-search"' in det_html
    assert "data-seo-audit-diff" in det_html

    # 2. Jonatan creates a batch plan from audit (scope=all)
    b_post = client.post(
        "/seo/batches/from-audit",
        headers=jonatan,
        data={
            "_csrf": csrf_j,
            "site_id": "mock-staging",
            "audit_id": audit_id,
            "scope": "all",
            "next_url": "/seo?site=mock-staging",
        },
        follow_redirects=False,
    )
    assert b_post.status_code == 302

    # Verify batch appears on /seo with Before vs. Proposed diff & expected_sha256
    hub_osc = client.get("/seo?site=mock-staging", headers=oscar)
    assert hub_osc.status_code == 200
    hub_osc_html = _assert_strict_csp(hub_osc, "/seo")
    m_batch = re.search(r"(seob-[a-f0-9]{12})", hub_osc_html)
    assert m_batch, "Expected seob-* batch plan on /seo"
    batch_id = m_batch.group(1)
    assert "Före vs. Föreslaget" in hub_osc_html
    m_sha = re.search(
        rf'action="/seo/batches/{batch_id}/apply".*?name="expected_sha256"\s+value="([a-f0-9]{{64}})"',
        hub_osc_html,
        flags=re.S,
    )
    assert m_sha, "Expected pinned expected_sha256 for batch apply"
    batch_sha = m_sha.group(1)
    csrf_o = _extract_csrf(hub_osc_html)

    # 3. Jonatan CANNOT apply or rollback batch -> 403
    assert (
        client.post(
            f"/seo/batches/{batch_id}/apply",
            headers=jonatan,
            data={"_csrf": csrf_j, "site_id": "mock-staging", "expected_sha256": batch_sha},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/seo/batches/{batch_id}/rollback",
            headers=jonatan,
            data={"_csrf": csrf_j, "site_id": "mock-staging", "confirm_batch_id": batch_id},
        ).status_code
        == 403
    )

    # 4. Oscar applies batch -> creates Change Vault entries tagged with batch_id
    osc_apply = client.post(
        f"/seo/batches/{batch_id}/apply",
        headers=oscar,
        data={
            "_csrf": csrf_o,
            "site_id": "mock-staging",
            "expected_sha256": batch_sha,
            "next_url": "/seo?site=mock-staging",
        },
        follow_redirects=True,
    )
    assert osc_apply.status_code == 200
    applied_html = _assert_strict_csp(osc_apply, "/seo")
    assert "Batch Rollback" in applied_html
    assert f"/wp/mock-staging/changes?batch_id={batch_id}" in applied_html

    ledger = ChangeLedger(data_dir=data_dir)
    batch_changes = ledger.list_batch_changes(batch_id, site_id="mock-staging")
    assert len(batch_changes) >= 1
    assert all(ch["status"] == "applied" for ch in batch_changes)

    # 5. Oscar rolls back the entire batch via /seo/batches/<batch_id>/rollback
    osc_rb = client.post(
        f"/seo/batches/{batch_id}/rollback",
        headers=oscar,
        data={
            "_csrf": csrf_o,
            "site_id": "mock-staging",
            "confirm_batch_id": batch_id,
            "next_url": "/seo?site=mock-staging",
        },
        follow_redirects=True,
    )
    assert osc_rb.status_code == 200
    after_rb = ledger.list_batch_changes(batch_id, site_id="mock-staging")
    assert after_rb and all(ch["status"] == "rolled_back" for ch in after_rb)


def test_gsc_parity_deep_crawl_and_drift_digest_ui(proposals_client):
    """Proposals 2, 3, 5: GSC quick-wins card, Nordic parity POST, Deep Crawl POST, and /seo/digest."""
    client, jonatan, _oscar, _data_dir = proposals_client

    hub = client.get("/seo?site=mock-staging", headers=jonatan)
    assert hub.status_code == 200
    html = _assert_strict_csp(hub, "/seo")
    assert "GSC Strike-Distance Quick-Wins" in html
    assert "GSC: Mock" in html
    assert "Nordisk SE/NO/DK-paritet" in html
    assert "Veckovis SEO-" in html
    csrf = _extract_csrf(html)

    # GET /seo/parity (navigable from Telegram/Messenger action cards)
    get_par = client.get("/seo/parity?site=mock-staging", headers=jonatan)
    assert get_par.status_code == 200
    assert "Nordisk SE/NO/DK-paritet" in _assert_strict_csp(get_par, "/seo/parity")

    # Trigger POST /seo/parity
    r_par = client.post(
        "/seo/parity",
        headers=jonatan,
        data={"_csrf": csrf, "site_id": "mock-staging", "next_url": "/seo?site=mock-staging"},
        follow_redirects=True,
    )
    assert r_par.status_code == 200
    par_html = _assert_strict_csp(r_par, "/seo")
    assert "paritetskontroll" in par_html.lower()

    # Trigger POST /seo/deep-crawl
    r_dc = client.post(
        "/seo/deep-crawl",
        headers=jonatan,
        data={
            "_csrf": csrf,
            "site_id": "mock-staging",
            "max_pages": "5",
            "next_url": "/seo?site=mock-staging",
        },
        follow_redirects=True,
    )
    assert r_dc.status_code == 200
    dc_html = _assert_strict_csp(r_dc, "/seo")
    assert "Senaste djupa crawl" in dc_html

    # GET /seo/digest (HTML + JSON)
    dig_html_resp = client.get("/seo/digest?site=mock-staging", headers=jonatan)
    assert dig_html_resp.status_code == 200
    dig_html = _assert_strict_csp(dig_html_resp, "/seo/digest")
    assert "Veckorapport SEO" in dig_html

    dig_json_resp = client.get("/seo/digest?site=mock-staging&format=json", headers=jonatan)
    assert dig_json_resp.status_code == 200
    dig_json = json.loads(dig_json_resp.get_data(as_text=True))
    assert dig_json["ok"] is True
    assert dig_json["site_id"] == "mock-staging"
    assert "gsc_summary" in dig_json and "plugin_summary" in dig_json and "markdown" in dig_json


def test_link_package_draft_edit_delivery_verify_and_cumulative_banner_ui(proposals_client):
    """Proposal 4: Cumulative anchor banner, draft edit, Oscar delivery URL, and placement verification."""
    client, jonatan, oscar, _data_dir = proposals_client

    lp_list = client.get("/seo/link-packages?site=mock-staging", headers=jonatan)
    assert lp_list.status_code == 200
    lp_html = _assert_strict_csp(lp_list, "/seo/link-packages")
    assert 'data-seo-cumulative-anchor="true"' in lp_html
    csrf_j = _extract_csrf(lp_html)

    # Create draft package
    created = client.post(
        "/seo/link-packages",
        headers=jonatan,
        data={
            "_csrf": csrf_j,
            "site_id": "mock-staging",
            "market": "no",
            "name": "NO Draft Edit Test",
            "budget_sek": "15000",
            "link_count": "4",
        },
        follow_redirects=False,
    )
    assert created.status_code == 302
    detail_url = created.headers["Location"]
    pkg_id = detail_url.rsplit("/", 1)[-1]

    # Draft detail shows in-place edit form and cumulative anchor banner
    det_j = client.get(detail_url, headers=jonatan)
    assert det_j.status_code == 200
    det_j_html = _assert_strict_csp(det_j, detail_url)
    assert 'data-seo-link-draft-edit="true"' in det_j_html
    assert 'data-seo-cumulative-anchor="true"' in det_j_html
    assert "Verifiera placeringar" in det_j_html

    # Jonatan edits draft in-place
    r_edit = client.post(
        f"/seo/link-packages/{pkg_id}/edit",
        headers=jonatan,
        data={
            "_csrf": csrf_j,
            "name": "NO Draft Edited Name",
            "budget_sek": "19000",
            "notes": "Uppdaterade redaktionella riktlinjer.",
        },
        follow_redirects=True,
    )
    assert r_edit.status_code == 200
    edit_html = _assert_strict_csp(r_edit, detail_url)
    assert "NO Draft Edited Name" in edit_html
    assert "19000 SEK" in edit_html

    # Jonatan CANNOT record delivery -> 403
    assert (
        client.post(
            f"/seo/link-packages/{pkg_id}/deliver",
            headers=jonatan,
            data={
                "_csrf": csrf_j,
                "target_index": "1",
                "published_url": "https://motor.no/artikkel/carplay/",
            },
        ).status_code
        == 403
    )

    # Oscar records delivery URL
    det_o = client.get(detail_url, headers=oscar)
    csrf_o = _extract_csrf(_assert_strict_csp(det_o, detail_url))
    r_dlv = client.post(
        f"/seo/link-packages/{pkg_id}/deliver",
        headers=oscar,
        data={
            "_csrf": csrf_o,
            "target_index": "1",
            "published_url": "https://motor.no/artikkel/carplay/",
        },
        follow_redirects=True,
    )
    assert r_dlv.status_code == 200
    dlv_html = _assert_strict_csp(r_dlv, detail_url)
    assert "https://motor.no/artikkel/carplay/" in dlv_html

    # Jonatan verifies placement -> badges appear
    r_ver = client.post(
        f"/seo/link-packages/{pkg_id}/verify",
        headers=jonatan,
        data={"_csrf": csrf_j},
        follow_redirects=True,
    )
    assert r_ver.status_code == 200
    ver_html = _assert_strict_csp(r_ver, detail_url)
    assert "HTTP 200" in ver_html
    assert "Indexbar" in ver_html
    assert "rel: dofollow" in ver_html


def test_cli_proposals_compact_commands_and_tables(tmp_path, monkeypatch, capsys):
    """Proposals 1-6 CLI: audits/fixes/batches/link-packs (--table), batch-propose/apply, gsc, parity, diff, digest, link-pack-edit/verify, wp plugins-plans, wp rollback-batch."""
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    from ecom_ops.cli import main

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "cli_p16_data"))
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # 1. Run 2 audits + list with --table
    assert main(["--mock", "--actor", "jonatan", "seo", "audit", "--site", "mock-staging"]) == 0
    a1 = json.loads(capsys.readouterr().out)
    assert main(["--mock", "--actor", "jonatan", "seo", "audit", "--site", "mock-staging"]) == 0
    a2 = json.loads(capsys.readouterr().out)

    assert main(["--mock", "--actor", "jonatan", "seo", "audits", "--site", "mock-staging", "--table"]) == 0
    audits_tbl = capsys.readouterr().out
    assert "AUDIT_ID" in audits_tbl and a2["audit_id"] in audits_tbl

    # 2. seo diff
    assert main(["--mock", "--actor", "jonatan", "seo", "diff", "--site", "mock-staging"]) == 0
    diff_out = json.loads(capsys.readouterr().out)
    assert diff_out["ok"] is True and diff_out["has_previous"] is True
    assert diff_out["previous_id"] == a1["audit_id"]

    # 3. seo batch-propose, batches --table, batch-apply (Jonatan denied, Oscar allowed)
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "batch-propose",
                "--site",
                "mock-staging",
                "--audit",
                a2["audit_id"],
                "--scope",
                "all",
            ]
        )
        == 0
    )
    bp = json.loads(capsys.readouterr().out)
    assert bp["ok"] is True
    batch_id = bp["batch"]["batch_id"]
    batch_sha = bp["batch"]["plan_sha256"]

    assert main(["--mock", "--actor", "jonatan", "seo", "batches", "--site", "mock-staging", "--format", "table"]) == 0
    batches_tbl = capsys.readouterr().out
    assert "BATCH_ID" in batches_tbl and batch_id in batches_tbl

    # Jonatan denied batch-apply
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "batch-apply",
                "--site",
                "mock-staging",
                "--batch",
                batch_id,
                "--expected-sha256",
                batch_sha,
            ]
        )
        == 1
    )
    capsys.readouterr()

    # Oscar succeeds batch-apply
    assert (
        main(
            [
                "--mock",
                "--actor",
                "oscar",
                "seo",
                "batch-apply",
                "--site",
                "mock-staging",
                "--batch",
                batch_id,
                "--expected-sha256",
                batch_sha,
            ]
        )
        == 0
    )
    ba = json.loads(capsys.readouterr().out)
    assert ba["ok"] is True and ba["applied_count"] >= 1

    # 4. wp changes --batch + wp rollback-batch (preview, Jonatan denied, Oscar execute)
    assert main(["--mock", "--actor", "jonatan", "wp", "changes", "--id", "mock-staging", "--batch", batch_id, "--table"]) == 0
    ch_tbl = capsys.readouterr().out
    assert "CHANGE_ID" in ch_tbl and batch_id in ch_tbl

    assert main(["--mock", "--actor", "jonatan", "wp", "rollback-batch", "--id", "mock-staging", "--batch", batch_id]) == 0
    rb_prev = json.loads(capsys.readouterr().out)
    assert rb_prev["ok"] is True and rb_prev["batch_id"] == batch_id

    assert main(["--mock", "--actor", "jonatan", "wp", "rollback-batch", "--id", "mock-staging", "--batch", batch_id, "--execute"]) == 1
    capsys.readouterr()

    assert main(["--mock", "--actor", "oscar", "wp", "rollback-batch", "--id", "mock-staging", "--batch", batch_id, "--execute"]) == 0
    rb_exec = json.loads(capsys.readouterr().out)
    assert rb_exec["ok"] is True and rb_exec["rolled_back_count"] >= 1

    # 5. seo fixes --table, gsc --table, parity --table, digest (JSON & --markdown)
    assert main(["--mock", "--actor", "jonatan", "seo", "fixes", "--site", "mock-staging", "--table"]) == 0
    assert "PLAN_ID" in capsys.readouterr().out

    assert main(["--mock", "--actor", "jonatan", "seo", "gsc", "--site", "mock-staging", "--table"]) == 0
    assert "QUERY" in capsys.readouterr().out

    assert main(["--mock", "--actor", "jonatan", "seo", "parity", "--site", "mock-staging", "--table"]) == 0
    assert "CODE" in capsys.readouterr().out

    assert main(["--mock", "--actor", "jonatan", "seo", "digest", "--site", "mock-staging"]) == 0
    dig_json = json.loads(capsys.readouterr().out)
    assert dig_json["ok"] is True

    assert main(["--mock", "--actor", "jonatan", "seo", "digest", "--site", "mock-staging", "--markdown"]) == 0
    assert "Veckorapport SEO" in capsys.readouterr().out

    # 6. seo link-packs --table, link-pack-edit, link-pack-verify --table
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "link-pack",
                "--site",
                "mock-staging",
                "--name",
                "CLI Edit Pack",
                "--market",
                "no",
                "--budget",
                "15000",
                "--links",
                "4",
            ]
        )
        == 0
    )
    lp_id = json.loads(capsys.readouterr().out)["package"]["package_id"]

    assert main(["--mock", "--actor", "jonatan", "seo", "link-packs", "--site", "mock-staging", "--table"]) == 0
    lp_tbl = capsys.readouterr().out
    assert "PACKAGE_ID" in lp_tbl and lp_id in lp_tbl

    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "link-pack-edit",
                "--id",
                lp_id,
                "--name",
                "CLI Edited Pack",
                "--budget",
                "17500",
            ]
        )
        == 0
    )
    ed_out = json.loads(capsys.readouterr().out)
    assert ed_out["ok"] is True and ed_out["link_package"]["name"] == "CLI Edited Pack"

    assert main(["--mock", "--actor", "jonatan", "seo", "link-pack-verify", "--id", lp_id, "--table"]) == 0
    assert "PUBLISHED_URL" in capsys.readouterr().out

    # 7. wp plugins-plans --table
    assert main(["--mock", "--actor", "oscar", "wp", "plugins-plan-update", "--id", "mock-staging"]) == 0
    plu_id = json.loads(capsys.readouterr().out)["data"]["plan_id"]
    assert main(["--mock", "--actor", "jonatan", "wp", "plugins-plans", "--id", "mock-staging", "--table"]) == 0
    plans_tbl = capsys.readouterr().out
    assert "PLAN_ID" in plans_tbl and plu_id in plans_tbl


def test_cli_seo_vendors_and_autopilot_json_and_table(tmp_path, monkeypatch, capsys):
    """CLI `seo vendors` and `seo autopilot` in JSON and --table modes with strict bounds and HITL flags."""
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    from ecom_ops.cli import main

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "cli_auto_data"))
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # 1. seo vendors JSON
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "vendors",
                "--market",
                "no",
                "--budget",
                "15000",
                "--links",
                "6",
                "--goal",
                "organic_growth",
            ]
        )
        == 0
    )
    v_json = json.loads(capsys.readouterr().out)
    assert v_json["ok"] is True
    assert v_json["selected_vendor_id"]
    assert len(v_json["candidates"]) >= 2
    assert v_json["disclosure_checklist"]
    assert v_json["no_auto_purchase_notice"]

    # 2. seo vendors --table (with --no-web-research)
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "vendors",
                "--market",
                "se",
                "--budget",
                "15000",
                "--links",
                "6",
                "--no-web-research",
                "--table",
            ]
        )
        == 0
    )
    v_tbl = capsys.readouterr().out
    assert "VENDOR_ID" in v_tbl and "SCORE" in v_tbl and "bazoom" in v_tbl

    # Invalid bounds rejected
    assert main(["--mock", "--actor", "jonatan", "seo", "vendors", "--budget", "inf"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert main(["--mock", "--actor", "jonatan", "seo", "vendors", "--links", "0"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False

    # 3. seo autopilot JSON (with --with-batch)
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "autopilot",
                "--site",
                "mock-staging",
                "--topic",
                "Trådlös CarPlay Volvo & BMW",
                "--market",
                "no",
                "--budget",
                "15000",
                "--links",
                "6",
                "--with-batch",
            ]
        )
        == 0
    )
    ap_json = json.loads(capsys.readouterr().out)
    assert ap_json["ok"] is True
    assert ap_json["executed"] is False
    assert ap_json["external_order_placed"] is False
    assert ap_json["approval_required"] is True
    assert ap_json["audit"]["audit_id"].startswith("seoa-")
    assert ap_json["strategy"]["strategy_id"].startswith("seos-")
    assert ap_json["campaign"]["campaign_id"].startswith("seoc-")
    assert ap_json["link_package"]["package_id"].startswith("seol-")
    assert ap_json["link_package"]["status"] == "draft"
    assert ap_json["link_package"]["selected_vendor_id"]

    # 4. seo autopilot --table (with --no-web-research)
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "autopilot",
                "--site",
                "mock-staging",
                "--topic",
                "CarPlay NO",
                "--market",
                "no",
                "--budget",
                "15000",
                "--links",
                "6",
                "--no-web-research",
                "--table",
            ]
        )
        == 0
    )
    ap_tbl = capsys.readouterr().out
    assert "STAGE" in ap_tbl and "ARTIFACT_ID" in ap_tbl
    assert "seos-" in ap_tbl and "seoc-" in ap_tbl and "seol-" in ap_tbl
