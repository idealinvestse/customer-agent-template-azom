"""External system integrations (WooCommerce, SSH, email)."""

from ecom_ops.integrations.ssh import SSHClient, SSHResult
from ecom_ops.integrations.woocommerce import WooCommerceClient, WooOrder

__all__ = [
    "SSHClient",
    "SSHResult",
    "WooCommerceClient",
    "WooOrder",
]
