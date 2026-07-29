"""Mail connector: Gmail, Outlook/Exchange, generic IMAP/POP3/SMTP + MS Graph."""

from __future__ import annotations

import os
from typing import Any

from ecom_ops.integrations.mail_providers import (
    PROVIDER_DEFAULTS,
    GraphMailTransport,
    InMemoryMailTransport,
    MailConfig,
    MailMessage,
    MailProvider,
    MailTransport,
    Pop3Transport,
    SmtpImapTransport,
    build_xoauth2_string,
)
from ecom_ops.security import SecurityError, get_env, sanitize_text, validate_email

# Re-export public API for existing imports
__all__ = [
    "MailProvider",
    "PROVIDER_DEFAULTS",
    "MailMessage",
    "MailConfig",
    "MailTransport",
    "build_xoauth2_string",
    "SmtpImapTransport",
    "Pop3Transport",
    "GraphMailTransport",
    "InMemoryMailTransport",
    "config_from_env",
    "MailClient",
    "client_from_env",
]


def config_from_env(
    *,
    provider: str | MailProvider | None = None,
    env_prefix: str | None = None,
) -> MailConfig:
    """Build MailConfig from environment variables.

    When ``env_prefix`` is set (e.g. ``MAIL_SE_``), credential fields
    (USERNAME, PASSWORD, FROM, OAUTH_*, GRAPH_*) prefer ``{prefix}NAME`` and
    fall back to unprefixed ``MAIL_*`` / ``GRAPH_*`` only when the prefixed
    variable is absent. Host/port/TLS fields prefer ``{prefix}SMTP_HOST`` etc.
    and fall back to unprefixed ``SMTP_*`` / ``IMAP_*`` / ``POP3_*`` / defaults.
    """
    prefix = (env_prefix or "").strip()

    def _prefixed(name: str) -> str | None:
        if not prefix:
            return None
        return get_env(f"{prefix}{name}")

    def _mail_cred(suffix: str) -> str | None:
        if prefix:
            val = _prefixed(suffix)
            if val is not None:
                return val
        return get_env(f"MAIL_{suffix}")

    def _graph_cred(suffix: str) -> str | None:
        if prefix:
            val = _prefixed(f"GRAPH_{suffix}")
            if val is not None:
                return val
        return get_env(f"GRAPH_{suffix}")

    def _host(name: str, default: str = "") -> str:
        if prefix:
            val = _prefixed(name)
            if val is not None:
                return val or ""
        return get_env(name, default) or ""

    def _int(name: str, default: int) -> int:
        if prefix:
            raw = get_env(f"{prefix}{name}")
            if raw is not None:
                try:
                    return int(raw)
                except ValueError as exc:
                    raise SecurityError(
                        f"Invalid integer env {prefix}{name}={raw!r}"
                    ) from exc
        raw = get_env(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise SecurityError(f"Invalid integer env {name}={raw!r}") from exc

    def _bool(name: str, default: bool) -> bool:
        if prefix:
            raw = get_env(f"{prefix}{name}")
            if raw is not None:
                return raw.lower() in {"1", "true", "yes", "on"}
        raw = get_env(name)
        if raw is None:
            return default
        return raw.lower() in {"1", "true", "yes", "on"}

    prov_raw = (
        str(provider)
        if provider
        else get_env("MAIL_PROVIDER", "generic_imap") or "generic_imap"
    )
    try:
        prov = MailProvider(prov_raw.lower().strip())
    except ValueError as exc:
        raise SecurityError(
            f"Invalid MAIL_PROVIDER {prov_raw!r}. "
            f"Allowed: {[p.value for p in MailProvider]}"
        ) from exc

    defaults = PROVIDER_DEFAULTS.get(prov, {})

    password = _mail_cred("PASSWORD")
    if password is None:
        password = get_env("SMTP_PASSWORD", "") or ""

    config = MailConfig(
        provider=prov,
        username=_mail_cred("USERNAME") or "",
        password=password or "",
        from_addr=_mail_cred("FROM") or "",
        smtp_host=_host("SMTP_HOST", defaults.get("smtp_host", "")),
        smtp_port=_int("SMTP_PORT", int(defaults.get("smtp_port", 587))),
        smtp_use_tls=_bool("SMTP_USE_TLS", bool(defaults.get("smtp_use_tls", True))),
        smtp_use_ssl=_bool("SMTP_USE_SSL", bool(defaults.get("smtp_use_ssl", False))),
        imap_host=_host("IMAP_HOST", defaults.get("imap_host", "")),
        imap_port=_int("IMAP_PORT", int(defaults.get("imap_port", 993))),
        imap_use_ssl=_bool("IMAP_USE_SSL", bool(defaults.get("imap_use_ssl", True))),
        pop3_host=_host("POP3_HOST", defaults.get("pop3_host", "")),
        pop3_port=_int("POP3_PORT", int(defaults.get("pop3_port", 995))),
        pop3_use_ssl=_bool("POP3_USE_SSL", bool(defaults.get("pop3_use_ssl", True))),
        oauth_access_token=_mail_cred("OAUTH_ACCESS_TOKEN") or "",
        oauth_refresh_token=_mail_cred("OAUTH_REFRESH_TOKEN") or "",
        oauth_client_id=_mail_cred("OAUTH_CLIENT_ID") or "",
        oauth_client_secret=_mail_cred("OAUTH_CLIENT_SECRET") or "",
        oauth_token_url=_mail_cred("OAUTH_TOKEN_URL") or "",
        graph_tenant_id=_graph_cred("TENANT_ID") or "",
        graph_client_id=_graph_cred("CLIENT_ID") or "",
        graph_client_secret=_graph_cred("CLIENT_SECRET") or "",
        graph_user=_graph_cred("USER") or "",
        graph_base_url=_host(
            "GRAPH_BASE_URL",
            defaults.get("graph_base_url", "https://graph.microsoft.com/v1.0"),
        )
        or "https://graph.microsoft.com/v1.0",
    )
    from ecom_ops.oauth.gmail import apply_stored_gmail_tokens

    return apply_stored_gmail_tokens(config)


def _transport_for(config: MailConfig) -> MailTransport:
    if config.provider == MailProvider.EXCHANGE_GRAPH:
        return GraphMailTransport(config)
    if config.provider == MailProvider.GENERIC_POP3:
        return Pop3Transport(config)
    return SmtpImapTransport(config)


class MailClient:
    """Unified mail client for send/fetch/mark_read across providers."""

    def __init__(
        self,
        *,
        config: MailConfig | None = None,
        transport: MailTransport | None = None,
    ) -> None:
        self.config = config or config_from_env()
        self.transport = transport or _transport_for(self.config)

    def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body: str,
        cc: str | list[str] | None = None,
        html_body: str | None = None,
        from_addr: str | None = None,
        in_reply_to: str | None = None,
        references_header: str | None = None,
    ) -> dict[str, Any]:
        to_list = _normalize_addrs(to)
        cc_list = _normalize_addrs(cc) if cc else []
        for addr in to_list + cc_list:
            validate_email(addr)
        subj = sanitize_text(subject, max_len=500)
        body_s = sanitize_text(body, max_len=100_000)
        html_s = sanitize_text(html_body, max_len=200_000) if html_body else None
        sender = from_addr or self.config.effective_from
        if sender:
            validate_email(sender)
        message = MailMessage(
            subject=subj,
            body=body_s,
            html_body=html_s,
            from_addr=sender or "",
            to_addrs=to_list,
            cc_addrs=cc_list,
            in_reply_to=(in_reply_to or "").strip() or None,
            references_header=(references_header or "").strip() or None,
        )
        return self.transport.send(message)

    def fetch_unread(
        self, *, folder: str = "INBOX", limit: int = 20
    ) -> list[MailMessage]:
        return self.transport.fetch(folder=folder, unread_only=True, limit=limit)

    def fetch(
        self, *, folder: str = "INBOX", unread_only: bool = False, limit: int = 20
    ) -> list[MailMessage]:
        return self.transport.fetch(
            folder=folder, unread_only=unread_only, limit=limit
        )

    def mark_read(self, uid: str, *, folder: str = "INBOX") -> None:
        if not uid or not str(uid).strip():
            raise SecurityError("uid is required")
        self.transport.mark_read(str(uid).strip(), folder=folder)

    def reply(
        self,
        original: MailMessage,
        *,
        body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        parent = (original.message_id or "").strip() or None
        refs_parts: list[str] = []
        if original.references_header:
            refs_parts.extend(original.references_header.split())
        if parent:
            refs_parts.append(parent)
        seen: set[str] = set()
        refs: list[str] = []
        for part in refs_parts:
            p = part.strip()
            if p and p not in seen:
                seen.add(p)
                refs.append(p)
        return self.send(
            to=original.from_addr,
            subject=subject,
            body=body,
            html_body=html_body,
            in_reply_to=parent,
            references_header=" ".join(refs) if refs else None,
        )


def _normalize_addrs(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts
    return [str(v).strip() for v in value if str(v).strip()]


def client_from_env(
    *,
    provider: str | MailProvider | None = None,
    env_prefix: str | None = None,
    use_mock: bool | None = None,
) -> MailClient:
    mock = use_mock
    if mock is None:
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    if mock:
        return MailClient(
            config=MailConfig(
                provider=MailProvider.GENERIC_IMAP,
                username="mock@azom.se",
                from_addr="support@azom.se",
            ),
            transport=InMemoryMailTransport(),
        )
    return MailClient(
        config=config_from_env(provider=provider, env_prefix=env_prefix)
    )
