"""CLI + config load tests."""

import json

from ecom_ops.cli import main
from ecom_ops.config import load_app_config


def test_load_app_config():
    cfg = load_app_config()
    assert cfg.customer.customer == "azom"
    assert "no" in cfg.customer.domains
    assert cfg.rbac.roles["oscar"] == "full_admin"
    assert cfg.rbac.escalation_critical == "oscar"
    assert cfg.limits.openrouter_cap == 100


def test_cli_order_status(capsys):
    code = main(
        [
            "--mock",
            "order-status",
            "--order-id",
            "1001",
            "--status",
            "completed",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["new_status"] == "completed"


def test_cli_support(capsys):
    code = main(["support", "--message", "Order 1002 shipping delay"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True


def test_cli_ssh_escalate(capsys):
    code = main(["--mock", "ssh", "--command", "reboot"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["escalated"] is True


def test_cli_version(capsys):
    code = main(["version"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "2.0.0"


def test_cli_status(capsys):
    code = main(["status"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["version"] == "2.0.0"
    assert "customer" in out
