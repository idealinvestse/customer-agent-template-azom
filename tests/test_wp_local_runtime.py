"""Unit tests for the opt-in local WordPress sandbox and browser smoke boundaries.

These tests run in the default suite (AZOM_WP_SANDBOX=0) without launching PHP,
WordPress, or a browser. They lock the fail-closed gates and isolation rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecom_ops.integrations.wp_browser_smoke import assert_loopback_http_url, run_browser_render
from ecom_ops.integrations.wp_local_runtime import LocalWordPressSandbox, build_php_child_env
from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS, overlay_key_allowed
from ecom_ops.wp_ops.review_runner import SandboxGate
from ecom_ops.wp_ops.review_workspace import ReviewError


def _known_site():
    return type(
        "Site",
        (),
        {"php_version": "8.3.37", "wordpress_version": "7.1.5", "woocommerce": False},
    )()


def _valid_live_record(plan_id: str) -> dict:
    return {
        "engine": "local_php_wp",
        "database": "synthetic_sqlite",
        "network": "loopback_only",
        "env_names": ["AZOM_REVIEW_SYNTHETIC", "TEMP"],
        "state": "running",
        "process_alive": True,
        "bind_host": "127.0.0.1",
        "port": None,
        "published_port": False,
        "docker_socket": False,
        "broad_host_mount": False,
        "payments_enabled": False,
        "checkout_enabled": False,
        "admin_password_stored": False,
        "pid": 4242,
        "plan_id": plan_id,
    }


def test_runtime_dir_is_protected_from_dashboard_overlays() -> None:
    assert "AZOM_WP_RUNTIME_DIR" in PROTECTED_OVERLAY_KEYS
    assert overlay_key_allowed("AZOM_WP_RUNTIME_DIR") is False
    assert overlay_key_allowed("AZOM_WP_SANDBOX") is False


def test_php_child_env_strips_host_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WP_APP_PASSWORD", "secret-wp")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", "secret-shop")
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck_secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    env = build_php_child_env()
    assert env["AZOM_REVIEW_SYNTHETIC"] == "1"
    for forbidden in ("WP_APP_PASSWORD", "WP_SECRET_MOCK_SHOP", "WOO_CONSUMER_KEY", "OPENROUTER_API_KEY"):
        assert forbidden not in env


def test_local_sandbox_refuses_when_flag_is_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    theme = tmp_path / "theme"
    theme.mkdir()
    (theme / "style.css").write_text("/* Theme Name: Test */\n", encoding="utf-8")
    sb = LocalWordPressSandbox(
        plan_id="wr-0123456789ab",
        sandbox_dir=tmp_path / "sb",
        theme_slug="azom-test",
        theme_dir=theme,
        runtime_dir=tmp_path / "rt",
    )
    with pytest.raises(ReviewError) as raised:
        sb.start()
    assert raised.value.error_class == "SandboxDisabled"


@pytest.mark.parametrize(
    ("patch", "expected_error"),
    [
        ({"process_alive": False}, "SandboxRuntimeUnavailable"),
        ({"state": "unavailable"}, "SandboxRuntimeUnavailable"),
        ({"published_port": True}, "SandboxRuntimeUnavailable"),
        ({"docker_socket": True}, "SandboxRuntimeUnavailable"),
        ({"broad_host_mount": True}, "SandboxRuntimeUnavailable"),
        ({"bind_host": "0.0.0.0"}, "SandboxRuntimeUnavailable"),
        ({"engine": "docker"}, "SandboxRuntimeUnavailable"),
        ({"pid": 0}, "SandboxRuntimeUnavailable"),
        ({"env_names": ["AZOM_REVIEW_SYNTHETIC", "WP_APP_PASSWORD"]}, "SandboxSecretRefused"),
        ({"env_names": ["AZOM_REVIEW_SYNTHETIC", "WP_SECRET_MOCK_SHOP"]}, "SandboxSecretRefused"),
    ],
)
def test_sandbox_gate_rejects_unsafe_or_dead_runtime_records(
    monkeypatch: pytest.MonkeyPatch, patch: dict, expected_error: str
) -> None:
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")
    gate = SandboxGate(runtime_starter=lambda _s, pid, **_kw: {**_valid_live_record(pid), **patch})
    with pytest.raises(ReviewError) as raised:
        gate.start(_known_site(), "wr-0123456789ab", high_risk=False)
    assert raised.value.error_class == expected_error


def test_sandbox_gate_reports_running_only_for_verified_local_process_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")
    stopped_calls: list[dict] = []
    gate = SandboxGate(
        runtime_starter=lambda _s, pid, **_kw: _valid_live_record(pid),
        runtime_stopper=lambda rec: stopped_calls.append(dict(rec)),
    )
    started = gate.start(_known_site(), "wr-0123456789ab", high_risk=False)
    assert started["state"] == "running"
    assert started["engine"] == "local_php_wp"
    assert started["network"] == "loopback_only"
    assert started["port"] is None
    assert started["published_port"] is False
    assert started["docker_socket"] is False
    assert started["broad_host_mount"] is False
    stopped = gate.stop(started)
    assert len(stopped_calls) == 1
    assert stopped["state"] == "stopped"
    assert stopped["container"] == "stopped"
    assert stopped["port"] is None


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://azom.no/",
        "http://azom.no/",
        "http://0.0.0.0:8080/",
        "https://127.0.0.1:8080/",
        "http://127.0.0.1/",
        "http://user:pass@127.0.0.1:8080/",
        "file:///etc/passwd",
    ],
)
def test_browser_smoke_refuses_non_loopback_urls(bad_url: str) -> None:
    with pytest.raises(ReviewError) as raised:
        assert_loopback_http_url(bad_url)
    assert raised.value.error_class == "NonLoopbackUrlRefused"


def test_browser_render_missing_binary_stays_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ecom_ops.integrations.wp_browser_smoke.find_browser_binary", lambda: None)
    result = run_browser_render("http://127.0.0.1:18080/", tmp_path / "artifacts", label="base")
    assert result["ok"] is False
    assert result["executed"] is False
    assert result["simulated"] is False
    assert result["availability"] == "unavailable"
    assert result["reason"] == "browser_binary_missing"
