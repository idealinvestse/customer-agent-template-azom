"""V1 pilot actions."""

from ecom_ops.actions.mail import MailService, fetch_mail, send_mail
from ecom_ops.actions.order_status import OrderStatusService, update_order_status
from ecom_ops.actions.product_desc import ProductDescService, generate_product_description
from ecom_ops.actions.ssh_ops import SSHOpsService, run_ssh_command
from ecom_ops.actions.support import SupportService, handle_support_message

__all__ = [
    "MailService",
    "OrderStatusService",
    "ProductDescService",
    "SSHOpsService",
    "SupportService",
    "fetch_mail",
    "generate_product_description",
    "handle_support_message",
    "run_ssh_command",
    "send_mail",
    "update_order_status",
]
