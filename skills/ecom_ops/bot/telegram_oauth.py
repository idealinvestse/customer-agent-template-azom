"""Oscar-only Telegram chat flows for ChatGPT Plus OAuth and xAI Grok Build login."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from ecom_ops.audit import log_action
from ecom_ops.bot.actors import ChannelActorDenied, resolve_channel_actor
from ecom_ops.bot.reply import ActionButton, ActionMarkup, BotReply, bot_reply
from ecom_ops.model_router import circuit_breaker
from ecom_ops.model_router.base import ModelRequest
from ecom_ops.model_router.errors import ModelAuthError, ModelConfigError, ModelRouteError
from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute
from ecom_ops.model_router.router import (
    TEST_PROMPT,
    get_model_router,
    load_last_test,
    save_last_test,
)
from ecom_ops.model_router.xai_device_login import device_login_status, start_device_login
from ecom_ops.rbac import Permission, resolve_actor
from ecom_ops.telemetry import default_telemetry

_OAUTH_CMD_CALLBACK_RE = re.compile(
    r"^/oauth(?:@[A-Za-z0-9_]+)?\s+(?:callback|complete)(?:\s|$)",
    re.IGNORECASE,
)
_URL_TOKEN_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_OAUTH_VERBS_RE = re.compile(
    r"\b(?:oauth|länka|lanka|koppla|anslut|logga\s+in|återanslut|ateranslut)\b",
    re.IGNORECASE,
)
_CHATGPT_KW_RE = re.compile(r"\b(?:chatgpt|openai)\b", re.IGNORECASE)
_XAI_KW_RE = re.compile(r"(?:x\.ai|\bxai\b|\bgrok\b)", re.IGNORECASE)
_SAFE_URL_PREFIXES = ("https://accounts.x.ai/", "https://auth.x.ai/")


def is_ephemeral_oauth_message(text: str) -> bool:
    """Return True if ``text`` carries an ephemeral OAuth callback URL/code."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _OAUTH_CMD_CALLBACK_RE.match(raw):
        return True
    tokens = raw.split()
    if len(tokens) == 1 and tokens[0].lower().startswith(("http://", "https://")):
        parsed = urlparse(tokens[0])
        host = (parsed.hostname or "").lower()
        q = parsed.query or ""
        has_state = "state=" in q
        has_code_or_err = "code=" in q or "error=" in q
        is_callback_path = parsed.path.rstrip("/").endswith("/oauth/openai/callback")
        is_loopback = host in {"127.0.0.1", "localhost"}
        if has_state and has_code_or_err and (is_callback_path or is_loopback):
            return True
    return False


def extract_ephemeral_callback_url(text: str) -> str:
    """Strip any leading ``/oauth callback`` prefix and return the callback URL."""
    raw = (text or "").strip()
    stripped = _OAUTH_CMD_CALLBACK_RE.sub("", raw, count=1).strip()
    if stripped.lower().startswith(("http://", "https://")):
        return stripped.split()[0]
    match = _URL_TOKEN_RE.search(stripped)
    return match.group(0) if match else stripped


def match_natural_oauth_intent(text: str) -> str | None:
    """Detect deterministic Swedish natural-language OAuth linking requests."""
    raw = (text or "").strip()
    if not raw or raw.startswith("/") or len(raw) > 160:
        return None
    if not _OAUTH_VERBS_RE.search(raw):
        return None
    wants_chatgpt = bool(_CHATGPT_KW_RE.search(raw))
    wants_xai = bool(_XAI_KW_RE.search(raw))
    if wants_chatgpt and wants_xai:
        return "panel"
    if wants_chatgpt:
        return "chatgpt"
    if wants_xai:
        return "xai"
    return None


def _require_oscar_telegram(chat_id: str | int, *, channel: str) -> tuple[str | None, BotReply | None]:
    if (channel or "telegram").strip().lower() != "telegram":
        return None, BotReply(
            text="OAuth-länkning kan bara köras i Telegram eller i dashboarden (/oscar)."
        )
    try:
        actor_name = resolve_channel_actor("telegram", chat_id)
        actor = resolve_actor(actor_name)
    except (ChannelActorDenied, KeyError):
        return None, BotReply(
            text="Din chat saknar giltig Oscar-behörighet för OAuth-hantering."
        )
    if not actor.has(Permission.ADMIN):
        return None, BotReply(
            text="Endast Oscar (ADMIN) får hantera ChatGPT- och xAI-OAuth i Telegram."
        )
    return actor_name, None


def _panel_actions() -> ActionMarkup:
    return ActionMarkup(
        rows=[
            [
                ActionButton(label="🔗 Anslut ChatGPT", payload="oauth:chatgpt:start"),
                ActionButton(label="⚡ Anslut xAI", payload="oauth:xai:start"),
            ],
            [
                ActionButton(label="🧪 Testa ChatGPT", payload="oauth:test:chatgpt_oauth"),
                ActionButton(label="🧪 Testa xAI", payload="oauth:test:xai_grok_build"),
            ],
            [
                ActionButton(label="🔄 Uppdatera status", payload="oauth:status"),
                ActionButton(label="🔌 Koppla från ChatGPT", payload="oauth:chatgpt:disconnect"),
            ],
        ]
    )


def _format_status_reply() -> BotReply:
    router = get_model_router()
    routes = router.routes()
    cg = routes.get("chatgpt_oauth") or {}
    xai = routes.get("xai_grok_build") or {}
    xai_login = device_login_status()
    preferred = router.preferred_route_id() or "ingen"
    breaker = circuit_breaker.status_snapshot()
    cg_br = (breaker.get("route:chatgpt_oauth") or {}).get("state", "closed")
    xai_br = (breaker.get("route:xai_grok_build") or {}).get("state", "closed")
    last = load_last_test()

    cg_conn = "ja" if cg.get("connected") else "nej"
    cg_plan = "beviljad" if cg.get("plan_usage") else "saknas"
    cg_reauth = "ja" if cg.get("needs_reauth") else "nej"
    cg_exp = cg.get("expires_at_iso") or "-"
    xai_conf = "ja" if xai.get("configured") else "nej"

    lines = [
        "Modell-OAuth (Oscar · Telegram)",
        f"Aktiv primär rutt: {preferred}",
        "",
        "ChatGPT Plus (chatgpt_oauth):",
        f"• Ansluten: {cg_conn} · Plananvändning: {cg_plan}",
        f"• needs_reauth: {cg_reauth} · gen: {cg.get('generation', 0)} · breaker: {cg_br}",
        f"• Modell: {cg.get('model') or 'gpt-5.2'} · Utgår: {cg_exp}",
        f"• Redirect URI: {cg.get('redirect_uri') or '-'}",
        "",
        "xAI Grok Build (xai_grok_build):",
        f"• Konfigurerad/aktiv: {xai_conf} · breaker: {xai_br}",
        f"• Device-login: {xai_login.get('status') or 'idle'}"
        + (f" ({xai_login.get('detail')})" if xai_login.get("detail") else ""),
        f"• Modell: {xai.get('model') or os.environ.get('XAI_MODEL') or 'grok-4'}",
    ]
    if last and last.get("route_id"):
        status_txt = "ok" if last.get("ok") else f"misslyckades ({last.get('error_class', '')})"
        lines.append("")
        lines.append(f"Senaste test: {last.get('route_id')} · {status_txt}")
    return bot_reply("\n".join(lines), actions=_panel_actions())


def _start_chatgpt_oauth(actor_name: str) -> BotReply:
    route = OpenAIChatGPTOAuthRoute()
    try:
        auth_url, _state = route.start_authorization()
    except ModelConfigError as exc:
        return bot_reply(
            f"ChatGPT OAuth saknar nödvändig konfiguration ({exc}).",
            actions=_panel_actions(),
        )
    redirect_uri = route.status().get("redirect_uri") or ""
    is_loopback = "127.0.0.1" in redirect_uri or "localhost" in redirect_uri
    log_action(
        actor=actor_name,
        action="chatgpt_oauth_telegram_start",
        target="chatgpt_oauth",
        details={"redirect_loopback": is_loopback},
    )
    if is_loopback:
        hint = (
            "Efter godkännande skickas du till 127.0.0.1 på din egen enhet.\n"
            "Kopiera hela adressfälts-URL:en (?code=...&state=...) och klistra in den "
            "direkt som ett meddelande här (eller /oauth callback <URL>).\n"
            "Azom raderar ditt meddelande direkt och sparar aldrig koder i SQLite/loggar."
        )
    else:
        hint = (
            f"Efter godkännande slutförs kopplingen automatiskt via {redirect_uri}.\n"
            "Skulle webbläsaren hamna på en ej nåbar callback-URL kan du även klistra "
            "in hela callback-URL:en direkt här i chatten."
        )
    text = (
        "ChatGPT Plus OAuth (SIWC · PKCE)\n"
        "Öppna länken nedan som Oscar och godkänn plananvändning:\n\n"
        f"{hint}"
    )
    actions = ActionMarkup(
        rows=[
            [ActionButton(label="🌐 Logga in med ChatGPT Plus", url=auth_url)],
            [
                ActionButton(label="🔄 Kontrollera status", payload="oauth:status"),
                ActionButton(label="🧪 Testa ChatGPT", payload="oauth:test:chatgpt_oauth"),
            ],
        ]
    )
    return bot_reply(text, actions=actions)


def _disconnect_chatgpt_oauth(actor_name: str) -> BotReply:
    OpenAIChatGPTOAuthRoute().disconnect()
    log_action(
        actor=actor_name,
        action="chatgpt_oauth_disconnect",
        target="chatgpt_oauth",
        details={"source": "telegram"},
    )
    return bot_reply(
        "ChatGPT Plus OAuth är frånkopplat och sessionen raderad.",
        actions=ActionMarkup(
            rows=[
                [ActionButton(label="🔗 Anslut ChatGPT", payload="oauth:chatgpt:start")],
                [ActionButton(label="🔄 Uppdatera status", payload="oauth:status")],
            ]
        ),
    )


def format_xai_login_reply(snap: dict[str, str], *, started: bool = False) -> BotReply:
    """Render xAI Grok Build device-login status with inline URL/check buttons."""
    st = str(snap.get("status") or "idle")
    uri = str(snap.get("verification_uri") or "").strip()
    code = str(snap.get("user_code") or "").strip()
    detail = str(snap.get("detail") or "").strip()
    safe_uri = uri if uri.startswith(_SAFE_URL_PREFIXES) else ""

    if st == "finished":
        return bot_reply(
            "✅ xAI Grok Build är inloggat och aktiverat (AZOM_XAI_GROK_BUILD=1).\n"
            "Grok Build äger sessionen; Azom rör aldrig auth.json.",
            actions=ActionMarkup(
                rows=[
                    [
                        ActionButton(label="🧪 Testa xAI", payload="oauth:test:xai_grok_build"),
                        ActionButton(label="🔄 Uppdatera status", payload="oauth:status"),
                    ]
                ]
            ),
        )
    if st == "failed":
        extra = ""
        if detail == "grok saknas":
            extra = "\nInstallera Grok Build CLI på hosten eller sätt XAI_GROK_BIN."
        return bot_reply(
            f"⚠️ xAI Grok Build-inloggningen misslyckades ({detail or 'okänt fel'}).{extra}",
            actions=ActionMarkup(
                rows=[
                    [
                        ActionButton(label="⚡ Försök igen", payload="oauth:xai:start"),
                        ActionButton(label="🔄 Uppdatera status", payload="oauth:status"),
                    ]
                ]
            ),
        )
    rows: list[list[ActionButton]] = []
    if safe_uri:
        rows.append([ActionButton(label="🌐 Öppna xAI-inloggning", url=safe_uri)])
    rows.append(
        [
            ActionButton(label="🔄 Kontrollera xAI-status", payload="oauth:xai:status"),
            ActionButton(label="🧪 Testa xAI", payload="oauth:test:xai_grok_build"),
        ]
    )
    prefix = "xAI Grok Build (device-auth startad)" if started else "xAI Grok Build (device-auth status)"
    lines = [prefix, f"Status: {st}"]
    if code:
        lines.append(f"Engångskod: {code}")
    if safe_uri:
        lines.append(f"Verifieringslänk: {safe_uri}")
    lines.append("Grok Build äger sessionen på hosten; Azom läser aldrig auth.json.")
    return bot_reply("\n".join(lines), actions=ActionMarkup(rows=rows))


def _start_xai_oauth(actor_name: str) -> BotReply:
    snap = start_device_login(wait_s=6.0)
    log_action(
        actor=actor_name,
        action="xai_device_login_telegram_start",
        target="xai_grok_build",
        details={"status": snap.get("status") or "unknown"},
    )
    return format_xai_login_reply(snap, started=True)


def _test_route(route_id: str, actor_name: str) -> BotReply:
    norm = "chatgpt_oauth" if "chatgpt" in route_id or "openai" in route_id else "xai_grok_build"
    router = get_model_router()
    try:
        res = router.complete_route(
            norm,
            ModelRequest(
                messages=[{"role": "user", "content": TEST_PROMPT}],
                max_output_tokens=512,
                temperature=0.0,
                timeout=30,
                actor=actor_name,
            ),
        )
    except ModelRouteError as exc:
        err_code = getattr(exc, "code", "") or ""
        save_last_test(
            route_id=norm,
            ok=False,
            error_class=type(exc).__name__,
            error_code=err_code,
            model="",
            provider="",
        )
        return bot_reply(
            f"❌ Isolerat test av {norm} misslyckades (ingen fallback användes): "
            f"{type(exc).__name__}" + (f" ({err_code})" if err_code else ""),
            actions=_panel_actions(),
        )
    save_last_test(
        route_id=norm,
        ok=True,
        error_class="",
        error_code="",
        model=res.model,
        provider=res.provider,
    )
    if res.cost_usd:
        default_telemetry.record(
            action="llm_model_test",
            site="azom",
            cost_usd=float(res.cost_usd),
            actor=actor_name,
            meta={
                "route_id": res.route_id,
                "provider": res.provider,
                "model": res.model,
                "cost_kind": "estimated",
            },
        )
    return bot_reply(
        f"✅ {norm} svarade utan fallback!\nModell: {res.model} · Svar: {res.content[:80]}",
        actions=_panel_actions(),
    )


def handle_ephemeral_oauth_callback(
    chat_id: str | int,
    raw_text: str,
    *,
    channel: str = "telegram",
) -> BotReply:
    """Complete ChatGPT OAuth from a pasted callback URL without persisting secrets."""
    actor_name, denied = _require_oscar_telegram(chat_id, channel=channel)
    if denied is not None or actor_name is None:
        return denied or BotReply(text="Behörighet saknas.")
    callback_url = extract_ephemeral_callback_url(raw_text)
    if not callback_url:
        return bot_reply(
            "Klistra in hela callback-URL:en efter godkännande i webbläsaren.",
            actions=_panel_actions(),
        )
    route = OpenAIChatGPTOAuthRoute()
    try:
        session = route.exchange_callback_url(callback_url)
    except ModelAuthError as exc:
        err_map = {
            "incomplete_registration": "Registreringen saknar utfärdat client_id — starta om via Anslut ChatGPT.",
            "client_mismatch": "Callback client_id matchar inte väntande session — starta om inloggningen.",
            "invalid_grant": "Auktoriseringskoden har löpt ut eller redan använts — försök igen.",
            "missing_callback_params": "URL:en saknar code eller state — klistra in hela adressfälts-URL:en.",
        }
        reason = err_map.get(exc.code, f"ChatGPT OAuth avvisades ({exc.code or 'auth_error'}).")
        return bot_reply(f"⚠️ {reason}", actions=_panel_actions())
    except ModelRouteError as exc:
        return bot_reply(
            f"⚠️ ChatGPT OAuth misslyckades ({type(exc).__name__}).",
            actions=_panel_actions(),
        )
    log_action(
        actor=actor_name,
        action="chatgpt_oauth_telegram_connect",
        target="chatgpt_oauth",
        details={
            "generation": session.generation,
            "plan_usage": "chatgpt.tokens.use.direct" in session.scopes,
        },
    )
    plan_txt = "beviljad" if "chatgpt.tokens.use.direct" in session.scopes else "saknas"
    return bot_reply(
        f"✅ ChatGPT Plus OAuth ansluten (generation {session.generation}, plananvändning: {plan_txt})!",
        actions=ActionMarkup(
            rows=[
                [
                    ActionButton(label="🧪 Testa ChatGPT", payload="oauth:test:chatgpt_oauth"),
                    ActionButton(label="🔄 Uppdatera status", payload="oauth:status"),
                ]
            ]
        ),
    )


def handle_oauth_command(
    chat_id: str | int,
    args: str,
    *,
    channel: str = "telegram",
) -> BotReply:
    """Dispatch ``/oauth ...`` commands in Telegram for Oscar."""
    actor_name, denied = _require_oscar_telegram(chat_id, channel=channel)
    if denied is not None or actor_name is None:
        return denied or BotReply(text="Behörighet saknas.")
    parts = [p for p in (args or "").strip().split() if p]
    if not parts or parts[0].lower() == "status":
        return _format_status_reply()
    sub = parts[0].lower()
    rest = parts[1].lower() if len(parts) > 1 else ""
    if sub in {"chatgpt", "openai"}:
        if rest in {"disconnect", "logout", "off"}:
            return _disconnect_chatgpt_oauth(actor_name)
        return _start_chatgpt_oauth(actor_name)
    if sub in {"disconnect", "logout"}:
        return _disconnect_chatgpt_oauth(actor_name)
    if sub in {"xai", "x.ai", "grok"}:
        if rest == "status":
            return format_xai_login_reply(device_login_status(), started=False)
        return _start_xai_oauth(actor_name)
    if sub == "test":
        target = rest or "chatgpt_oauth"
        return _test_route(target, actor_name)
    if sub in {"callback", "complete"}:
        raw_url = " ".join(parts[1:])
        return handle_ephemeral_oauth_callback(chat_id, raw_url, channel=channel)
    return _format_status_reply()


def handle_oauth_callback_query(
    chat_id: str | int,
    data: str,
    *,
    channel: str = "telegram",
) -> BotReply | None:
    """Handle ``oauth:*`` inline button callbacks."""
    raw = (data or "").strip()
    if not raw.startswith("oauth:"):
        return None
    actor_name, denied = _require_oscar_telegram(chat_id, channel=channel)
    if denied is not None or actor_name is None:
        return denied or BotReply(text="Behörighet saknas.")
    if raw == "oauth:status":
        return _format_status_reply()
    if raw == "oauth:chatgpt:start":
        return _start_chatgpt_oauth(actor_name)
    if raw == "oauth:chatgpt:disconnect":
        return _disconnect_chatgpt_oauth(actor_name)
    if raw == "oauth:xai:start":
        return _start_xai_oauth(actor_name)
    if raw == "oauth:xai:status":
        return format_xai_login_reply(device_login_status(), started=False)
    if raw in {"oauth:test:chatgpt_oauth", "oauth:test:xai_grok_build"}:
        target = raw.split(":", 2)[2]
        return _test_route(target, actor_name)
    return _format_status_reply()
