"""OpenClaw-compatible slash commands for Azom Telegram bot (datalasse-style)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ecom_ops import __version__
from ecom_ops.bot.reply import BotReply, approve_case_keyboard
from ecom_ops.bot.store import ConversationStore, clamp_messages

HandlerFn = Callable[["CommandContext"], str | BotReply]


@dataclass
class CommandContext:
    chat_id: str | int
    text: str
    args: str
    store: ConversationStore
    slots: dict[str, Any] = field(default_factory=dict)

    @property
    def session(self) -> dict[str, Any]:
        state = self.store.get(self.chat_id) or {}
        session = dict(state.get("session") or {})
        return session

    def save_session(self, **updates: Any) -> None:
        state = self.store.get(self.chat_id) or {
            "flow": None,
            "step": None,
            "slots": {},
            "messages": [],
            "tool_digest": "",
            "updated_at": 0,
        }
        session = dict(state.get("session") or {})
        session.update(updates)
        state["session"] = session
        state["messages"] = clamp_messages(state.get("messages"))
        state["tool_digest"] = str(state.get("tool_digest") or "")
        # preserve flow if mid-dialog
        self.store.set(self.chat_id, state)


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: HandlerFn
    aliases: tuple[str, ...] = ()


def _parse_command(text: str) -> tuple[str | None, str]:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None, raw
    # Telegram: /cmd@botname args
    first, _, rest = raw.partition(" ")
    cmd = first[1:].split("@", 1)[0].lower()
    return cmd or None, rest.strip()


def cmd_help(ctx: CommandContext) -> str:
    return (
        "AzomOps · OpenClaw hybrid\n"
        "Skriv /commands för full katalog.\n\n"
        "Fråga fritt — jag hämtar order/ärenden/status när det behövs.\n"
        "Approve via knapp eller /cases approve (aldrig silent send).\n\n"
        "Vanliga kommandon:\n"
        "/status · /whoami · /new · /reset · /stop\n"
        "/tools · /tasks · /usage · /model\n"
        "/order · /cases · /health · /brief\n"
        "/context — dialogminne + senaste tool-digest"
    )


def cmd_commands(ctx: CommandContext) -> str:
    lines = ["Kommandokatalog (OpenClaw-kompatibel + Azom):"]
    for spec in COMMANDS:
        alias = f" (alias: {', '.join('/' + a for a in spec.aliases)})" if spec.aliases else ""
        lines.append(f"/{spec.name} — {spec.description}{alias}")
    return "\n".join(lines)


def cmd_status(ctx: CommandContext) -> str:
    import os

    from ecom_ops.config import load_app_config
    from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured
    from ecom_ops.telemetry import Telemetry

    try:
        cfg = load_app_config()
        cost = Telemetry().sum_cost_usd()
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
        session = ctx.session
        return (
            f"AzomOps status\n"
            f"Version: {__version__}\n"
            f"Runtime: {'mock' if mock else 'live'}\n"
            f"Customer: {cfg.customer.customer}\n"
            f"Domains: {', '.join(cfg.customer.domains)}\n"
            f"OpenRouter cap: ${cfg.limits.openrouter_cap} · used ~${cost:.4f}\n"
            f"Gmail OAuth: {'connected' if GmailOAuthStore().has_tokens() else 'not connected'}"
            f" / configured={gmail_oauth_configured()}\n"
            f"Telegram: {'yes' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'no'}\n"
            f"Session model: {session.get('model', 'default')}\n"
            f"Verbose: {session.get('verbose', 'off')}\n"
            f"Think: {session.get('think', 'default')}"
        )
    except Exception as exc:
        return f"Status error: {exc}"


def cmd_whoami(ctx: CommandContext) -> str:
    from ecom_ops.bot.actors import resolve_telegram_actor

    actor = resolve_telegram_actor(ctx.chat_id)
    return (
        f"Sender / session\n"
        f"chat_id: {ctx.chat_id}\n"
        f"actor: {actor}\n"
        f"Alias: /id"
    )


def cmd_new(ctx: CommandContext) -> str:
    model = ctx.args.strip() or None
    ctx.store.clear(ctx.chat_id)
    if model:
        ctx.store.set(
            ctx.chat_id,
            {
                "flow": None,
                "step": None,
                "slots": {},
                "messages": [],
                "tool_digest": "",
                "session": {"model": model},
            },
        )
        return f"Ny session startad. Model pin: {model}"
    return "Ny session startad (/new). Föregående dialog rensad."


def cmd_reset(ctx: CommandContext) -> str:
    soft = ctx.args.lower().startswith("soft")
    session = ctx.session if soft else {}
    ctx.store.clear(ctx.chat_id)
    if soft and session:
        ctx.store.set(
            ctx.chat_id,
            {
                "flow": None,
                "step": None,
                "slots": {},
                "messages": [],
                "tool_digest": "",
                "session": session,
            },
        )
        return "Session reset (soft) — settings behållna, dialog rensad."
    return "Session reset. Skriv /status eller /help."


def cmd_stop(ctx: CommandContext) -> str:
    state = ctx.store.get(ctx.chat_id)
    session = (state or {}).get("session") or {}
    messages = clamp_messages((state or {}).get("messages"))
    digest = str((state or {}).get("tool_digest") or "")
    ctx.store.clear(ctx.chat_id)
    if session or messages or digest:
        ctx.store.set(
            ctx.chat_id,
            {
                "flow": None,
                "step": None,
                "slots": {},
                "messages": messages,
                "tool_digest": digest,
                "session": session,
            },
        )
    return "Stop — pågående flöde avbrutet."


def cmd_tools(ctx: CommandContext) -> str:
    verbose = "verbose" in ctx.args.lower()
    chat_tools = [
        ("lookup_order", "Orderstatus (read-only, via fritext)"),
        ("list_cases / show_case", "Ärenden (read-only)"),
        ("ops_snapshot", "Status/budget/cases-count"),
    ]
    slash_tools = [
        ("order-status", "Uppdatera Woo orderstatus (operator/CLI)"),
        ("product-desc", "Generera produktbeskrivning"),
        ("support", "Klassificera + draft-svar"),
        ("mail", "Send/fetch/reply (RBAC)"),
        ("cases", "Mail→ärende poll + approve/send"),
        ("ssh", "Allowlistad SSH health"),
        ("escalation", "Ticket till Oscar"),
    ]
    lines = ["Chat-verktyg (fritext, read-only):"]
    for name, desc in chat_tools:
        lines.append(f"• {name} — {desc}" if verbose else f"• {name}")
    lines.append("Slash / CLI:")
    for name, desc in slash_tools:
        lines.append(f"• {name} — {desc}" if verbose else f"• {name}")
    if not verbose:
        lines.append("(/tools verbose för beskrivningar)")
    lines.append("Utskick kräver /cases approve (eller knappen).")
    return "\n".join(lines)


def cmd_tasks(ctx: CommandContext) -> str:
    lines = ["Tasks / köer:"]
    try:
        from ecom_ops.cases.service import CaseService

        cases = CaseService().list_open(limit=5)
        lines.append(f"Öppna cases: {len(cases)}")
        for c in cases[:5]:
            lines.append(f"  - [{c.category}] {c.subject[:40]}")
    except Exception as exc:
        lines.append(f"Cases: error ({exc})")
    try:
        import json
        import os
        from pathlib import Path

        path = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data")) / "escalations.jsonl"
        open_n = 0
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                    if obj.get("status", "open") == "open":
                        open_n += 1
                except json.JSONDecodeError:
                    pass
        lines.append(f"Öppna eskaleringar: {open_n}")
    except Exception:
        lines.append("Eskaleringar: n/a")
    return "\n".join(lines)


def cmd_usage(ctx: CommandContext) -> str:
    from ecom_ops.config import load_app_config
    from ecom_ops.telemetry import Telemetry

    arg = ctx.args.strip().lower() or "cost"
    cfg = load_app_config()
    cost = Telemetry().sum_cost_usd()
    if arg in {"off", "tokens", "full"}:
        ctx.save_session(usage=arg)
        return f"Usage footer: {arg}"
    if arg in {"reset", "clear", "default"}:
        ctx.save_session(usage="off")
        return "Usage override cleared."
    return (
        f"Usage / cost\n"
        f"Local telemetry cost: ${cost:.4f}\n"
        f"OpenRouter cap: ${cfg.limits.openrouter_cap}\n"
        f"Session usage mode: {ctx.session.get('usage', 'off')}\n"
        f"Tips: /usage cost | /usage off"
    )


def cmd_model(ctx: CommandContext) -> str:
    arg = ctx.args.strip()
    if not arg or arg in {"status", "list"}:
        return (
            f"Model\n"
            f"Session: {ctx.session.get('model', 'default')}\n"
            f"Default: OPENROUTER_MODEL / gpt-4o-mini (LLM-chat på fritext)\n"
            f"Sätt: /model <name> · /model default"
        )
    if arg == "default":
        ctx.save_session(model="default")
        return "Model pin cleared (default)."
    ctx.save_session(model=arg)
    return f"Model pin: {arg} (används i LLM-chat)."


def cmd_verbose(ctx: CommandContext) -> str:
    arg = (ctx.args.strip().lower() or "status")
    if arg in {"on", "off", "full"}:
        ctx.save_session(verbose=arg)
        return f"Verbose: {arg}"
    return f"Verbose: {ctx.session.get('verbose', 'off')} — /verbose on|off|full"


def cmd_think(ctx: CommandContext) -> str:
    arg = (ctx.args.strip().lower() or "status")
    if arg and arg != "status":
        ctx.save_session(think=arg)
        return f"Think level: {arg}"
    return f"Think: {ctx.session.get('think', 'default')} — /think <level|default>"


def cmd_skill(ctx: CommandContext) -> str:
    name = (ctx.args.strip() or "ecom-ops").lower()
    if name in {"ecom-ops", "ecom_ops", "list"}:
        return (
            "Skill: ecom-ops\n"
            "Actions: order-status, product-desc, support, mail, ssh, cases\n"
            "CLI: python -m ecom_ops --help\n"
            "Docs: skills/ecom-ops/SKILL.md"
        )
    return f"Okänd skill {name!r}. Prova /skill ecom-ops"


def cmd_context(ctx: CommandContext) -> str:
    state = ctx.store.get(ctx.chat_id)
    flow = (state or {}).get("flow")
    n_msg = len((state or {}).get("messages") or [])
    model = ctx.session.get("model", "default")
    digest = str((state or {}).get("tool_digest") or "").strip()
    digest_line = digest.replace("\n", " · ")[:200] if digest else "(none)"
    return (
        f"Context\n"
        f"flow: {flow or 'idle'}\n"
        f"turns: {n_msg}\n"
        f"model: {model}\n"
        f"tool_digest: {digest_line}\n"
        f"session keys: {', '.join(sorted(ctx.session.keys())) or '(none)'}\n"
        f"/new rensar historik · /reset soft behåller settings"
    )


# Azom domain commands (kept as first-class)

def cmd_health(ctx: CommandContext) -> str:
    try:
        from ecom_ops.actions.ssh_ops import SSHOpsService
        from ecom_ops.bot.actors import resolve_telegram_actor

        actor = resolve_telegram_actor(ctx.chat_id)
        results = SSHOpsService().health(actor=actor)
        lines = ["SSH health:"]
        for r in results:
            ok = "ok" if r.ok else "fail"
            cmd = r.result.command if r.result else "?"
            lines.append(f"{cmd}: {ok}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Health error: {exc}"


def cmd_brief(ctx: CommandContext) -> str:
    try:
        from ecom_ops.config import load_app_config
        from ecom_ops.telemetry import Telemetry

        cfg = load_app_config()
        cost = Telemetry().sum_cost_usd()
        return (
            f"Daily brief\n"
            f"Customer: {cfg.customer.customer}\n"
            f"Domains: {', '.join(cfg.customer.domains)}\n"
            f"LLM cost USD: {cost:.4f} / cap {cfg.limits.openrouter_cap}"
        )
    except Exception as exc:
        return f"Brief error: {exc}"


def cmd_cases(ctx: CommandContext) -> str | BotReply:
    """ /cases | /cases show <id> | /cases approve <id> | /cases close <id> """
    try:
        from ecom_ops.bot.actors import resolve_telegram_actor
        from ecom_ops.cases.service import CaseService

        actor = resolve_telegram_actor(ctx.chat_id)
        svc = CaseService()
        parts = ctx.args.split(maxsplit=1)
        sub = (parts[0].lower() if parts else "list")
        rest = parts[1].strip() if len(parts) > 1 else ""

        if not ctx.args.strip():
            sub = "list"

        if sub in {"help", "?"}:
            return (
                "/cases — lista öppna+eskalerade\n"
                "/cases show <id8> — detalj + draft\n"
                "/cases approve <id8> — skicka draft\n"
                "/cases close <id8> — stäng utan svar"
            )

        if sub in {"list", "ls"}:
            cases = svc.list_open(limit=10)
            if not cases:
                return "Inga öppna/eskalerade ärenden."
            # Escalated → high → suggest-approve → newest
            cases = list(cases)
            cases.sort(key=lambda c: c.created_at or "", reverse=True)
            cases.sort(key=lambda c: 0 if getattr(c, "suggest_approve", False) else 1)
            cases.sort(key=lambda c: 0 if (c.priority or "") == "high" else 1)
            cases.sort(key=lambda c: 0 if c.status == "escalated" else 1)
            lines = [f"Kö ({len(cases)}):"]
            for c in cases:
                badge = "!" if c.priority == "high" or c.status == "escalated" else "-"
                if getattr(c, "suggest_approve", False):
                    conf = getattr(c, "classify_confidence", None)
                    conf_s = f" {conf:.0%}" if isinstance(conf, (int, float)) else ""
                    suggest = f" ★föreslå{conf_s}"
                else:
                    suggest = ""
                lines.append(
                    f"{badge} {c.id[:8]} | {c.status} | {c.category}{suggest} | {c.subject[:36]}"
                )
            lines.append("\n/cases show|approve|close <id8>  (★föreslå = bekräfta med approve)")
            return "\n".join(lines)

        if sub in {"show", "get", "view"}:
            if not rest:
                return "Ange id: /cases show <id8>"
            case = svc.store.resolve_id_prefix(rest) or svc.get(rest)
            if not case:
                return f"Hittade inte case {rest!r}"
            return _case_show_reply(case)

        if sub in {"approve", "reply", "send"}:
            if not rest:
                return "Ange id: /cases approve <id8>"
            case = svc.store.resolve_id_prefix(rest) or svc.get(rest)
            if not case:
                return f"Hittade inte case {rest!r}"
            result = svc.approve_and_send(case.id, actor=actor)
            if result.ok:
                return f"Skickat. Case {case.id[:8]} → replied."
            return f"Misslyckades: {result.message}"

        if sub == "close":
            if not rest:
                return "Ange id: /cases close <id8>"
            case = svc.store.resolve_id_prefix(rest) or svc.get(rest)
            if not case:
                return f"Hittade inte case {rest!r}"
            result = svc.close(case.id, actor=actor, reason="telegram")
            if result.ok:
                return f"Stängt. Case {case.id[:8]}."
            return f"Misslyckades: {result.message}"

        # Bare id prefix: /cases <id8>
        case = svc.store.resolve_id_prefix(sub) or svc.get(sub)
        if case:
            return _case_show_reply(case)

        return (
            f"Okänt: /cases {sub}\n"
            "/cases · show · approve · close"
        )
    except Exception as exc:
        return f"Cases error: {exc}"


def _format_case_show(case: Any) -> str:
    draft = (case.draft_reply or "")[:400]
    lines = [
        f"Case {case.id[:8]} ({case.status})",
        f"Från: {case.from_addr}",
        f"Ämne: {case.subject}",
        f"Kategori: {case.category} · prio: {case.priority or 'normal'}",
    ]
    if getattr(case, "suggest_approve", False):
        conf = getattr(case, "classify_confidence", None)
        conf_s = f" ({conf:.0%})" if isinstance(conf, (int, float)) else ""
        lines.append(f"★ Föreslå godkänn{conf_s}")
        lines.append(f"Bekräfta skicka: /cases approve {case.id[:8]}")
    if case.order_id:
        lines.append(f"Order: {case.order_id}")
    if case.escalation_id:
        lines.append(f"Eskalering: {case.escalation_id}")
    lines.append(f"\nDraft:\n{draft or '(tom)'}")
    lines.append(f"\n/cases approve {case.id[:8]} · /cases close {case.id[:8]}")
    return "\n".join(lines)


def _case_show_reply(case: Any) -> BotReply:
    """Case show with explicit approve button (same path as /cases approve)."""
    text = _format_case_show(case)
    return BotReply(
        text=text,
        reply_markup=approve_case_keyboard(case.id[:8]),
    )


def cmd_order(ctx: CommandContext) -> str:
    """Handled specially in BotHandler for multi-step; this is for bare /order."""
    arg = ctx.args.strip()
    if arg:
        return f"__ORDER__:{arg}"
    return "__ORDER_PROMPT__"


COMMANDS: list[CommandSpec] = [
    CommandSpec("help", "Kort hjälp", cmd_help),
    CommandSpec("commands", "Full kommandokatalog", cmd_commands),
    CommandSpec("status", "Runtime/status (OpenClaw-style)", cmd_status),
    CommandSpec("whoami", "Visa chat_id / actor", cmd_whoami, aliases=("id",)),
    CommandSpec("new", "Ny session", cmd_new),
    CommandSpec("reset", "Reset session (soft behåller settings)", cmd_reset),
    CommandSpec("stop", "Avbryt pågående flöde", cmd_stop, aliases=("cancel",)),
    CommandSpec("tools", "Lista agent-tools", cmd_tools),
    CommandSpec("tasks", "Öppna cases + eskaleringar", cmd_tasks),
    CommandSpec("usage", "Kostnad / usage-läge", cmd_usage),
    CommandSpec("model", "Visa/sätt session model pin", cmd_model),
    CommandSpec("verbose", "Verbose on|off|full", cmd_verbose, aliases=("v",)),
    CommandSpec("think", "Think-nivå", cmd_think, aliases=("thinking", "t")),
    CommandSpec("skill", "Kör/visa skill", cmd_skill),
    CommandSpec("context", "Session context", cmd_context),
    CommandSpec("health", "SSH health checks", cmd_health),
    CommandSpec("brief", "KPI brief", cmd_brief),
    CommandSpec("cases", "Ärenden: list|show|approve|close", cmd_cases),
    CommandSpec("order", "Orderstatus (read-only)", cmd_order),
]

_BY_NAME: dict[str, CommandSpec] = {}
for _spec in COMMANDS:
    _BY_NAME[_spec.name] = _spec
    for _a in _spec.aliases:
        _BY_NAME[_a] = _spec


TELEGRAM_MENU_COMMANDS: list[dict[str, str]] = [
    {"command": s.name, "description": s.description[:40]}
    for s in COMMANDS
    if s.name
    in {
        "help",
        "commands",
        "status",
        "whoami",
        "new",
        "reset",
        "stop",
        "tools",
        "tasks",
        "usage",
        "model",
        "cases",
        "order",
        "health",
        "brief",
        "skill",
    }
]


def dispatch_openclaw_command(
    chat_id: str | int,
    text: str,
    store: ConversationStore,
) -> str | BotReply | None:
    """Return reply if text is an OpenClaw/Azom slash command, else None."""
    name, args = _parse_command(text)
    if not name:
        return None
    # /start → help
    if name == "start":
        name = "help"
    spec = _BY_NAME.get(name)
    if not spec:
        return f"Okänt kommando /{name}. /commands · /help"
    ctx = CommandContext(chat_id=chat_id, text=text, args=args, store=store)
    return spec.handler(ctx)
