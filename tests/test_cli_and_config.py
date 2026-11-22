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
    assert cfg.customer.budget_cap_llm == 100


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
    code = main(["--mock", "support", "--message", "Order 1002 shipping delay"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["classify_method"] == "keyword"


def test_cli_mock_skips_model_router(capsys, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("model router was called in mock mode")

    monkeypatch.setattr("ecom_ops.llm.chat_completion", boom)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "openrouter")

    support = main(["--mock", "support", "--message", "Var är order 1001?"])
    assert support == 0
    support_out = json.loads(capsys.readouterr().out)
    assert support_out["classify_method"] == "keyword"

    product = main(
        ["--mock", "product-desc", "--name", "Lampa", "--features", "matt", "--language", "sv"]
    )
    assert product == 0
    product_out = json.loads(capsys.readouterr().out)
    assert product_out["ok"] is True


def test_cli_ssh_escalate(capsys):
    code = main(["--mock", "ssh", "--command", "reboot"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["escalated"] is True


def test_cli_version(capsys):
    code = main(["version"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "3.0.0"


def test_cli_status(capsys):
    code = main(["status"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["version"] == "3.0.0"
    assert "customer" in out
