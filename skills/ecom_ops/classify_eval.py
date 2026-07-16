"""Evaluate keyword classify + suggest eligibility against fixture pack (SB4 tooling)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecom_ops.actions.support import classify_message, extract_order_id
from ecom_ops.cases.suggest import is_suggest_approve_eligible

DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "support_classify"
)


def load_fixtures(directory: Path | str | None = None) -> list[dict[str, Any]]:
    base = Path(directory) if directory else DEFAULT_FIXTURE_DIR
    rows: list[dict[str, Any]] = []
    if not base.is_dir():
        return rows
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("id"):
            rows.append(data)
    return rows


def evaluate_fixtures(
    fixtures: list[dict[str, Any]] | None = None,
    *,
    directory: Path | str | None = None,
) -> dict[str, Any]:
    fx = fixtures if fixtures is not None else load_fixtures(directory)
    results: list[dict[str, Any]] = []
    n_cat_ok = n_oid_ok = n_sug_ok = 0
    for item in fx:
        text = str(item.get("text") or "")
        cat = classify_message(text)
        got_oid = extract_order_id(text)
        exp_cat = str(item.get("expected_category") or "")
        exp_oid = item.get("order_id_in_text")
        conf = float(item.get("suggest_with_llm_confidence") or 0.0)
        escalated = cat.value == "abuse"
        suggest = is_suggest_approve_eligible(
            category=cat.value,
            confidence=conf,
            order_id=got_oid,
            escalated=escalated,
        )
        cat_ok = cat.value == exp_cat
        if exp_oid is None:
            oid_ok = got_oid is None
        else:
            oid_ok = got_oid == str(exp_oid)
        sug_ok = suggest is bool(item.get("expect_suggest_approve"))
        if cat_ok:
            n_cat_ok += 1
        if oid_ok:
            n_oid_ok += 1
        if sug_ok:
            n_sug_ok += 1
        results.append(
            {
                "id": item.get("id"),
                "category_ok": cat_ok,
                "got_category": cat.value,
                "order_id_ok": oid_ok,
                "got_order_id": got_oid,
                "suggest_ok": sug_ok,
                "got_suggest": suggest,
            }
        )
    n = len(fx)
    return {
        "ok": n > 0 and n_cat_ok == n and n_oid_ok == n and n_sug_ok == n,
        "n": n,
        "category_pass": n_cat_ok,
        "order_id_pass": n_oid_ok,
        "suggest_pass": n_sug_ok,
        "results": results,
    }
