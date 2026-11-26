"""Tests for centralized WP ops config (fail-closed mock defaults), unified secret redaction, and PII leaf module."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ecom_ops.integrations.wp_files_ssh import SshFileTransport, build_file_transport
from ecom_ops.integrations.wp_org_client import WpOrgClientError, fetch_wporg_json
from ecom_ops.wp_ops import config as wp_config
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.code_index import _scrub_signature
from ecom_ops.wp_ops.credentials import redact_secret_text, scrub_compact
from ecom_ops.wp_ops.file_changes import _scrub_search_text, rest_reads_allowed
from ecom_ops.wp_ops.files import LocalTree, _redact_secret_lines
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.pii import contains_real_pii, normalize_pii_text
from ecom_ops.wp_ops.registry import load_sites
from ecom_ops.wp_ops.ssh_settings import describe_ssh
from ecom_ops.wp_ops.wporg_intel import fetch_component_intel

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sample_site():
    return parse_site(
        {
            "id": "shop-live",
            "display_name": "Shop Live",
            "canonical_url": "https://shop.internal",
            "environment": "staging",
            "business_owner": "customer",
            "hosting_provider": "hetzner",
            "wordpress_version": "6.7",
            "php_version": "8.3",
            "woocommerce": True,
            "critical_urls": ["https://shop.internal/"],
            "maintenance_window": "02:00-04:00",
            "secret_ref": "WP_SECRET_SHOP_LIVE",
            "files_root": "/var/www/shop-live",
            "ssh_host": "shop.internal",
            "ssh_user": "azom-wp-reader",
            "ssh_port": 22,
            "alert_owner": "oscar",
            "alert_confirmations": 2,
            "check_interval_seconds": 300,
            "timeout_seconds": 10,
            "dedupe_window_seconds": 30,
            "capabilities": {
                "hosting_api": "unknown",
                "wp_cli": "unknown",
                "staging": "available",
                "backup": "unknown",
                "restore": "unknown",
                "uptime": "available",
                "security_scanner": "unknown",
            },
        }
    )


def test_unset_azom_use_mock_is_fail_closed_across_wp_ops_and_integrations(monkeypatch, tmp_path):
    """When AZOM_USE_MOCK is unset or blank, every WP ops & WP integration gate stays in mock mode."""
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")
    monkeypatch.setenv("AZOM_WPORG_INTEL", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    assert wp_config.is_mock_mode() is True
    assert wp_config.rest_reads_allowed() is False
    assert rest_reads_allowed() is False
    assert wp_config.ssh_files_enabled() is False
    assert wp_config.wporg_intel_live_enabled() is False

    # Blank AZOM_USE_MOCK also stays mock
    monkeypatch.setenv("AZOM_USE_MOCK", "   ")
    assert wp_config.is_mock_mode() is True
    assert wp_config.ssh_files_enabled() is False
    assert wp_config.wporg_intel_live_enabled() is False

    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)

    # registry selects wp_sites.mock.yaml
    sites = load_sites(data_dir=tmp_path)
    assert {s.id for s in sites} == {"mock-shop", "mock-staging"}

    # ssh_settings.describe_ssh reports mock=True, ssh_enabled=False
    site = _sample_site()
    view = describe_ssh(site, site, tmp_path, include_paths=False)
    assert view["mock"] is True
    assert view["ssh_enabled"] is False

    # wp_files_ssh.build_file_transport stays LocalTree (never SshFileTransport)
    transport = build_file_transport(site, tmp_path)
    assert isinstance(transport, LocalTree)

    # wporg_intel stays in mock mode (never hits live api.wordpress.org)
    intel = fetch_component_intel("plugin", "woocommerce")
    assert intel["source"] == "mock"
    assert intel["intel_enabled"] is False

    # wp_org_client refuses live network fetch
    with pytest.raises(WpOrgClientError):
        fetch_wporg_json("https://api.wordpress.org/plugins/info/1.2/?action=plugin_information")


def test_explicit_azom_use_mock_zero_enables_explicit_opt_in_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_WPORG_INTEL", "0")

    assert wp_config.is_mock_mode() is False
    assert wp_config.rest_reads_allowed() is True
    assert wp_config.ssh_files_enabled() is False
    assert wp_config.wporg_intel_live_enabled() is False

    site = _sample_site()
    assert isinstance(build_file_transport(site, tmp_path), LocalTree)

    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")
    monkeypatch.setenv("AZOM_WPORG_INTEL", "1")
    assert wp_config.ssh_files_enabled() is True
    assert wp_config.wporg_intel_live_enabled() is True
    assert isinstance(build_file_transport(site, tmp_path), SshFileTransport)
    view = describe_ssh(site, site, tmp_path, include_paths=False)
    assert view["mock"] is False
    assert view["ssh_enabled"] is True


def test_data_dir_and_config_dir_path_helpers(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "env-data"))
    assert wp_config.data_dir_path() == tmp_path / "env-data"
    assert wp_config.data_dir_path(tmp_path / "explicit") == tmp_path / "explicit"

    monkeypatch.setenv("AZOM_CONFIG_DIR", str(tmp_path / "cfg"))
    assert wp_config.config_dir_path() == tmp_path / "cfg"
    monkeypatch.delenv("AZOM_CONFIG_DIR", raising=False)
    assert wp_config.config_dir_path() == REPO_ROOT / "config"


def test_unified_secret_redaction_and_delegates():
    samples = [
        ("https://admin:supersecretpass@db.example.com/app", "supersecretpass"),
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("key = 'xai-1234567890abcdef'", "xai-1234567890abcdef"),
        ("key = 'sk-proj-1234567890abcdef'", "sk-proj-1234567890abcdef"),
        ("token = 'my-secret-token-value'", "my-secret-token-value"),
        ("define('DB_PASSWORD', 'prod_db_pw_999');", "prod_db_pw_999"),
    ]
    for raw, leaked in samples:
        redacted = redact_secret_text(raw)
        assert leaked not in redacted, f"redact_secret_text leaked {leaked!r}"
        assert leaked not in _redact_secret_lines(raw), f"files._redact_secret_lines leaked {leaked!r}"
        assert leaked not in _scrub_search_text(raw), f"file_changes._scrub_search_text leaked {leaked!r}"
        whole = redact_secret_text(raw, redact_whole_lines=True)
        assert leaked not in whole
        assert "[credential-bearing source line redacted]" in whole

    # scrub_compact and code_index._scrub_signature now catch xai-, sk-proj-, URI creds, and Basic headers
    for raw, leaked in samples[:5]:
        compact = scrub_compact(raw)
        assert leaked not in compact, f"scrub_compact leaked {leaked!r}"
        assert _scrub_signature(raw) == compact

    assert "[REDACTED]" in _redact_secret_lines("token = 'my-secret-token-value'")
    assert "my-extra-bound-secret" not in redact_secret_text(
        "prefix my-extra-bound-secret suffix",
        extra_secrets=["my-extra-bound-secret"],
    )


def test_pii_leaf_module_and_no_review_plan_agent_tools_cycle():
    # Synthetic addresses allowed
    assert contains_real_pii("kund@example.com") is False
    assert contains_real_pii("order@azom.invalid") is False
    assert contains_real_pii({"customer": "test@demo.example", "note": "Order #1001"}) is False

    # Real customer PII detected (raw, URL-encoded, HTML-entity-encoded, zero-width obfuscated, nested)
    assert contains_real_pii("anna.svensson@gmail.com") is True
    assert contains_real_pii("anna%40gmail.com") is True
    assert contains_real_pii("anna&#64;gmail.com") is True
    assert contains_real_pii("anna\u200b@gmail.com") is True
    assert contains_real_pii("Ring +46 70 123 45 67") is True
    assert contains_real_pii("Mobil 070-123 45 67") is True
    assert contains_real_pii("Personnummer 19850615-1234") is True
    assert contains_real_pii("Fødselsnummer 150685 12345") is True
    assert contains_real_pii({"nested": [{"k": "ola@online.no"}]}) is True
    assert normalize_pii_text("a%40b&#46;se\u200b") == "a@b.se"

    # WpAgentTools._contains_real_pii alias remains intact
    assert WpAgentTools._contains_real_pii("anna@gmail.com") is True
    assert WpAgentTools._contains_real_pii("test@example.com") is False

    # review_plan.py no longer imports agent_tools (circular import broken)
    review_plan_src = (REPO_ROOT / "skills" / "ecom_ops" / "wp_ops" / "review_plan.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(review_plan_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "agent_tools" in node.module:
            raise AssertionError(f"review_plan.py still imports {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "agent_tools" in alias.name:
                    raise AssertionError(f"review_plan.py still imports {alias.name}")
