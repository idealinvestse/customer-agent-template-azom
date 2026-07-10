"""Security validation tests."""

import pytest

from ecom_ops.security import (
    SecurityError,
    is_critical_ssh_command,
    is_ssh_allowlisted,
    redact_secrets,
    validate_email,
    validate_order_id,
    validate_order_status,
    validate_site,
)


def test_validate_order_id_ok():
    assert validate_order_id("1001") == "1001"
    assert validate_order_id(42) == "42"


def test_validate_order_id_bad():
    with pytest.raises(SecurityError):
        validate_order_id("abc")
    with pytest.raises(SecurityError):
        validate_order_id("../etc/passwd")


def test_validate_order_status():
    assert validate_order_status("Completed") == "completed"
    with pytest.raises(SecurityError):
        validate_order_status("shipped-yesterday")


def test_validate_site_and_email():
    assert validate_site("azom") == "azom"
    with pytest.raises(SecurityError):
        validate_site("../x")
    assert validate_email("a@b.co") == "a@b.co"
    with pytest.raises(SecurityError):
        validate_email("not-an-email")


def test_ssh_allowlist_and_critical():
    assert is_ssh_allowlisted("uptime")
    assert is_ssh_allowlisted("df -h")
    assert is_critical_ssh_command("rm -rf /")
    assert is_critical_ssh_command("sed -i 's/a/b/' file.php")
    assert is_critical_ssh_command("vim wp-config.php")
    assert not is_critical_ssh_command("uptime")


def test_redact_secrets():
    payload = {"token": "abc", "nested": {"api_key": "x", "ok": 1}}
    red = redact_secrets(payload)
    assert red["token"] == "***REDACTED***"
    assert red["nested"]["api_key"] == "***REDACTED***"
    assert red["nested"]["ok"] == 1
