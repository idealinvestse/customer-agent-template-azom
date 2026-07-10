"""Compatibility shim: prefer ecom_ops package.

Legacy import path kept for Moss skill layout (skills/ecom-ops/).
"""

from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailMessage,
    MailProvider,
    client_from_env as mail_client_from_env,
)
from ecom_ops.integrations.ssh import SSHClient, SSHResult
from ecom_ops.integrations.woocommerce import (
    InMemoryWooTransport,
    WooCommerceClient,
    WooOrder,
    client_from_env,
)

__all__ = [
    "EcomIntegrations",
    "InMemoryMailTransport",
    "InMemoryWooTransport",
    "MailClient",
    "MailConfig",
    "MailMessage",
    "MailProvider",
    "SSHClient",
    "SSHResult",
    "WooCommerceClient",
    "WooOrder",
    "client_from_env",
    "mail_client_from_env",
]


class EcomIntegrations:
    """Facade used by automation scripts."""

    def __init__(self, *, use_mock: bool = False) -> None:
        self.woo = client_from_env(use_mock=use_mock)
        self.ssh = SSHClient(host="azom-vps")
        self.mail = mail_client_from_env(use_mock=use_mock)

    def order_status(self, order_id: str, status: str, site: str = "azom"):
        from ecom_ops.actions.order_status import OrderStatusService

        return OrderStatusService(woo=self.woo).update(
            order_id=order_id, status=status, site=site
        )

    def product_desc(self, **kwargs):
        from ecom_ops.actions.product_desc import ProductDescService

        return ProductDescService(woo=self.woo).generate(**kwargs)

    def support(self, message: str, **kwargs):
        from ecom_ops.actions.support import SupportService

        return SupportService().handle(message, **kwargs)

    def ssh_run(self, command: str, **kwargs):
        from ecom_ops.actions.ssh_ops import SSHOpsService

        return SSHOpsService(client=self.ssh).run(command, **kwargs)

    def mail_send(self, **kwargs):
        from ecom_ops.actions.mail import MailService

        return MailService(client=self.mail).send(**kwargs)

    def mail_fetch(self, **kwargs):
        from ecom_ops.actions.mail import MailService

        return MailService(client=self.mail).fetch(**kwargs)
