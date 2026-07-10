"""Product description tests."""

from ecom_ops.actions.product_desc import ProductDescService


def test_generate_from_product_id(woo, telemetry, escalation):
    svc = ProductDescService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.generate(product_id="501", language="sv", actor="agent")
    assert result.ok
    assert result.short_description
    assert "Azom Pro Headset" in (result.description or "")
    assert not result.published


def test_generate_and_publish(woo, telemetry, escalation):
    svc = ProductDescService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.generate(
        product_id="501",
        language="no",
        features="støysensering, bluetooth 5.3",
        publish=True,
        actor="agent",
    )
    assert result.ok
    assert result.published
    product = woo.get_product("501")
    assert product["description"]
    assert product["short_description"]


def test_generate_named_only(woo, telemetry, escalation):
    svc = ProductDescService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.generate(name="Test Widget", language="en")
    assert result.ok
    assert "Test Widget" in (result.short_description or "")


def test_jonatan_denied(woo, telemetry, escalation):
    svc = ProductDescService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.generate(name="X", actor="jonatan")
    assert not result.ok
    assert result.escalated
