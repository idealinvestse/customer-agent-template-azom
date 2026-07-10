"""External system integrations (WooCommerce, SSH, email)."""

from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailMessage,
    MailProvider,
    client_from_env as mail_client_from_env,
    config_from_env as mail_config_from_env,
)
from ecom_ops.integrations.ssh import SSHClient, SSHResult
from ecom_ops.integrations.woocommerce import WooCommerceClient, WooOrder

__all__ = [
    "InMemoryMailTransport",
    "MailClient",
    "MailConfig",
    "MailMessage",
    "MailProvider",
    "SSHClient",
    "SSHResult",
    "WooCommerceClient",
    "WooOrder",
    "mail_client_from_env",
    "mail_config_from_env",
]
