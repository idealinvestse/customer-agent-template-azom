"""Import-path smoke for dashboard route modules (Phase 4 split)."""

from __future__ import annotations

import ast
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "infrastructure" / "dashboard"

EXPECTED_ROUTES = {
    "/health",
    "/live",
    "/ready",
    "/webhooks/woo",
    "/webhooks/messenger",
    "/cases",
    "/cases/poll",
    "/cases/bulk-close",
    "/oscar",
    "/oscar/secrets",
    "/marketing",
    "/oauth/gmail/start",
    "/oauth/google/start",
}


def _route_strings(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "route":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, str
            ):
                found.add(node.args[0].value)
    return found


def test_dashboard_route_table_still_present():
    routes: set[str] = set()
    for py in DASH.glob("*.py"):
        routes |= _route_strings(py)
    missing = EXPECTED_ROUTES - routes
    assert not missing, f"missing dashboard routes: {sorted(missing)}"


def test_factory_registers_split_modules():
    src = (DASH / "app.py").read_text(encoding="utf-8")
    for name in (
        "register_health_routes",
        "register_webhook_routes",
        "register_core_routes",
        "register_cases_routes",
        "register_oscar_routes",
        "register_marketing_routes",
        "register_oauth_routes",
    ):
        assert name in src
