"""External system integrations (WooCommerce, WordPress, SSH, email, webhooks)."""

from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailMessage,
    MailProvider,
)
from ecom_ops.integrations.mail import (
    client_from_env as mail_client_from_env,
)
from ecom_ops.integrations.mail import (
    config_from_env as mail_config_from_env,
)
from ecom_ops.integrations.ssh import SSHClient, SSHResult
from ecom_ops.integrations.webhooks import (
    WebhookEvent,
    WebhookReceiver,
    verify_webhook_signature,
)
from ecom_ops.integrations.woocommerce import (
    ShipmentTracking,
    WooCommerceClient,
    WooOrder,
    WooSystemStatus,
)
from ecom_ops.integrations.wordpress import (
    InMemoryWpTransport,
    WordPressClient,
    WpPost,
    wp_client_from_env,
)

__all__ = [
    "InMemoryMailTransport",
    "InMemoryWpTransport",
    "MailClient",
    "MailConfig",
    "MailMessage",
    "MailProvider",
    "SSHClient",
    "SSHResult",
    "ShipmentTracking",
    "WebhookEvent",
    "WebhookReceiver",
    "WooCommerceClient",
    "WooOrder",
    "WooSystemStatus",
    "WordPressClient",
    "WpPost",
    "mail_client_from_env",
    "mail_config_from_env",
    "verify_webhook_signature",
    "wp_client_from_env",
]
