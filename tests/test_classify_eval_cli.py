"""classify-eval CLI + fixture scorer."""

from __future__ import annotations

import json

from ecom_ops.classify_eval import evaluate_fixtures
from ecom_ops.cli import main


def test_evaluate_fixtures_default_pack():
    result = evaluate_fixtures()
    assert result["n"] >= 6
    assert result["ok"] is True
    assert result["category_pass"] == result["n"]


def test_cli_classify_eval(capsys):
    code = main(["classify-eval"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["n"] >= 6
