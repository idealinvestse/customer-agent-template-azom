"""Support automation tests."""

from ecom_ops.actions.support import (
    SupportCategory,
    SupportService,
    classify_message,
    extract_order_id,
)


def test_classify_and_extract():
    assert classify_message("Where is order 12345?") == SupportCategory.ORDER_STATUS
    assert extract_order_id("Ordernr: 998877") == "998877"
    assert classify_message("I want a refund please") == SupportCategory.RETURN
    assert classify_message("We will call the lawyer about GDPR complaint") == SupportCategory.ABUSE


def test_extract_order_id_sv_no_dk_and_subject():
    assert extract_order_id("Hej, status på beställning 445566") == "445566"
    assert extract_order_id("Hvor er min ordre 778899?") == "778899"
    assert extract_order_id("Var er bestilling 112233") == "112233"
    assert extract_order_id("1001\n\nHej var är paketet?") == "1001"
    assert extract_order_id("#1001") == "1001"
    assert extract_order_id("Status på 998877 tack") == "998877"
    # Prefer labeled over bare body noise when labeled present
    assert extract_order_id("Ref 9999 ordernummer 1001 snälla") == "1001"


def test_support_draft_reply(telemetry, escalation):
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "Hej, var är min order 1001?",
        customer_name="Anna",
        customer_email="anna@example.com",
        language="sv",
        actor="agent",
    )
    assert result.ok
    assert result.category == SupportCategory.ORDER_STATUS
    assert result.order_id == "1001"
    assert result.reply and "Anna" in result.reply
    assert not result.escalated


def test_critical_escalates_to_oscar(telemetry, escalation):
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "This is a chargeback and legal threat regarding order 55",
        actor="agent",
    )
    assert result.ok
    assert result.escalated
    assert result.ticket_id
    assert result.category == SupportCategory.ABUSE


def test_jonatan_cannot_reply(telemetry, escalation):
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle("Hej", actor="jonatan")
    assert not result.ok
    assert result.escalated


def test_order_id_from_unique_email(telemetry, escalation, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "Hej, var är min leverans?",
        customer_email="customer@example.com",
        customer_name="Kund",
        actor="agent",
        use_mock=True,
    )
    assert result.ok
    # Mock order 1001 has billing email customer@example.com
    assert result.order_id == "1001"


def test_order_id_not_guessed_on_multi_email(telemetry, escalation, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    from ecom_ops.integrations.woocommerce import InMemoryWooTransport

    # Two orders same email → must not pick either for auto order_id
    transport = InMemoryWooTransport()
    transport.orders["1003"] = {
        "id": 1003,
        "status": "completed",
        "currency": "SEK",
        "total": "10.00",
        "billing": {"email": "customer@example.com"},
    }

    import ecom_ops.integrations.woocommerce as woo_mod

    original = woo_mod.client_from_env

    def _client(*, base_url=None, use_mock=None):
        return woo_mod.WooCommerceClient(
            base_url="https://mock.local", transport=transport
        )

    monkeypatch.setattr(woo_mod, "client_from_env", _client)
    try:
        svc = SupportService(telemetry=telemetry, escalation=escalation)
        result = svc.handle(
            "Hej, var är min leverans?",
            customer_email="customer@example.com",
            actor="agent",
            use_mock=True,
        )
        assert result.ok
        assert result.order_id is None
    finally:
        monkeypatch.setattr(woo_mod, "client_from_env", original)
