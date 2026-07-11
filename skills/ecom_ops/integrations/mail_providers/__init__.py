"""Mail provider transports (split from integrations.mail)."""

from ecom_ops.integrations.mail_providers.graph import GraphMailTransport
from ecom_ops.integrations.mail_providers.memory import InMemoryMailTransport
from ecom_ops.integrations.mail_providers.models import (
    PROVIDER_DEFAULTS,
    MailConfig,
    MailMessage,
    MailProvider,
    MailTransport,
    build_xoauth2_string,
    parse_email_message,
)
from ecom_ops.integrations.mail_providers.pop3 import Pop3Transport
from ecom_ops.integrations.mail_providers.smtp_imap import SmtpImapTransport

__all__ = [
    "MailProvider",
    "PROVIDER_DEFAULTS",
    "MailMessage",
    "MailConfig",
    "MailTransport",
    "build_xoauth2_string",
    "parse_email_message",
    "SmtpImapTransport",
    "Pop3Transport",
    "GraphMailTransport",
    "InMemoryMailTransport",
]
