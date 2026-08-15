"""nb/da template drafts + mailbox language + market→Woo domain."""

from __future__ import annotations

from ecom_ops.actions.support import SupportCategory, classify_message, draft_reply
from ecom_ops.cases.mailbox_probe import probe_mailbox_matrix
from ecom_ops.order_context import woo_domain_from_market


def test_nb_da_order_status_drafts():
    nb = draft_reply(
        category=SupportCategory.ORDER_STATUS,
        customer_name="Anna",
        order_id="1001",
        language="nb",
    )
    da = draft_reply(
        category=SupportCategory.ORDER_STATUS,
        customer_name="Anna",
        order_id="1001",
        language="da",
    )
    assert "Hei Anna" in nb
    assert "Vennlig hilsen" in nb
    assert "ordre 1001" in nb
    assert "Hej Anna" in da
    assert "Venlig hilsen" in da
    assert "ordre 1001" in da
    assert "Tack" not in da
    assert "Tack" not in nb


def test_da_classify_fixtures():
    assert classify_message("hvor er min ordre 4401").value == "order_status"
    assert classify_message("sporingsnummer på levering").value == "shipping"
    assert classify_message("fortrydelsesretten og returnere").value == "return"


def test_woo_domain_from_market():
    assert woo_domain_from_market("no") == "no"
    assert woo_domain_from_market("dk") == "dk"
    assert woo_domain_from_market("se") == "se"
    assert woo_domain_from_market("azom.dk") == "dk"
    assert woo_domain_from_market("") is None


def test_mailbox_matrix_does_not_enable(monkeypatch):
    monkeypatch.delenv("MAIL_NO_USERNAME", raising=False)
    matrix = probe_mailbox_matrix()
    assert matrix["enabled_flipped"] is False
    ids = {r["id"]: r for r in matrix["mailboxes"]}
    assert ids["support_no"]["enabled"] is False
    assert ids["support_dk"]["enabled"] is False
    assert ids["support_no"]["status"] in {"not_configured", "disabled_ready"}
