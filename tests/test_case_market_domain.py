"""case.market → Woo domain= wiring."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.order_context import woo_domain_from_market, resolve_order_panel


def test_woo_domain_from_market():
    assert woo_domain_from_market("se") == "se"
    assert woo_domain_from_market("NO") == "no"
    assert woo_domain_from_market("azom.dk") == "dk"
    assert woo_domain_from_market("") is None
    assert woo_domain_from_market("unknown") is None


def test_regenerate_draft_passes_market_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_no",
        subject="NO order",
        from_addr="a@b.co",
        body="Order 1001",
        category="order_status",
        draft_reply="Hej",
        order_id="1001",
        message_id="<no-market@x>",
        status="open",
        market="no",
    )
    svc = CaseService(store=store)
    with patch("ecom_ops.cases.service.resolve_order_context") as mock_ctx:
        mock_ctx.return_value = "[Order 1001]\nStatus: processing"
        result = svc.regenerate_draft(case.id, actor="agent", use_mock=True)
    assert result.ok
    mock_ctx.assert_called()
    _args, kwargs = mock_ctx.call_args
    assert kwargs.get("domain") == "no"


def test_dashboard_order_panel_uses_market(monkeypatch):
    panel = resolve_order_panel("1001", use_mock=True, domain="no")
    assert panel is not None
    assert panel["ok"] is True
