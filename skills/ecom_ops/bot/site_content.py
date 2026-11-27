"""Telegram reads and confirm-only edits for shop content.

Posts, the site title, the tagline, and plugin on/off wait for Ja.
Theme files, theme switches, and plugin install/update/delete stay out.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ecom_ops.bot.dialog_actions import PendingAction
from ecom_ops.bot.wp_intents import (
    PACKAGE_FALLTHROUGH_REFUSE_RE as _PACKAGE_FALLTHROUGH_REFUSE_RE,
)
from ecom_ops.bot.wp_intents import (
    PACKAGE_REFUSE_RE as _PACKAGE_REFUSE_RE,
)
from ecom_ops.bot.wp_intents import (
    POST_CREATE_RE as _POST_CREATE_RE,
)
from ecom_ops.bot.wp_intents import (
    POST_DELETE_RE as _POST_DELETE_RE,
)
from ecom_ops.bot.wp_intents import (
    THEME_FALLTHROUGH_REFUSE_RE as _THEME_FALLTHROUGH_REFUSE_RE,
)
from ecom_ops.bot.wp_intents import (
    THEME_TOUCH_RE as _THEME_TOUCH_RE,
)
from ecom_ops.bot.wp_intents import (
    WP_CODE_FALLTHROUGH_RE as _WP_CODE_FALLTHROUGH_RE,
)
from ecom_ops.bot.wp_intents import (
    WP_FILE_REFUSE_RE as _FILE_RE,
)
from ecom_ops.bot.wp_intents import (
    WP_WORKSPACE_INTENT_RE as _WP_WORKSPACE_INTENT_RE,
)
from ecom_ops.security import SecurityError, sanitize_text
from ecom_ops.wp_ops.plugin_policy import (
    BRIDGE_PLUGIN_SLUG,
    CRITICAL_PLUGIN_SLUGS,
    is_bridge_plugin,
    normalize_plugin_slug,
)

_POST_NOUN_RE = re.compile(
    r"\b(?:inl[äa]gg(?:et|en|ets|ens)?|blogginl[äa]gg(?:et)?|posts?)\b",
    re.I,
)
_POST_ID_RE = re.compile(
    r"\b(?:inl[äa]gg(?:et)?|post)\s*#?\s*(\d{1,12})\b",
    re.I,
)
_POST_LIST_RE = re.compile(
    r"\b(?:visa|lista|hämta|hamta|vilka|n[äa]sta|fler(?:a)?)\s+"
    r"(?:\d{1,2}\s+)?(?:alla\s+)?(?:blogginl[äa]gg|inl[äa]gg|poster|posts)\b",
    re.I,
)
_POST_SHOW_RE = re.compile(
    r"\b(?:visa|öppna|oppna|kolla)?\s*(?:inl[äa]gg(?:et)?|post)\s*#?\s*(\d{1,12})\b",
    re.I,
)
_POST_FIELD_RE = re.compile(
    r"\b(?P<field>titel|title|texten|text|innehållet|innehallet|innehåll|innehall|"
    r"content|status)\b\s*(?:till|to|:)\s*(?P<value>.+)$",
    re.I | re.S,
)
_SITE_SHOW_RE = re.compile(
    r"\b(?:sajttitel|tagline(?:n)?|undertitel(?:n)?|sajtbeskrivning(?:en)?)\b"
    r"|\bvad\s+heter\s+sajten\b"
    r"|\b(?:visa|kolla)\s+(?:sajtens\s+titel|webbplatsens\s+titel|tagline(?:n)?)\b",
    re.I,
)
_SITE_FIELD_RE = re.compile(
    r"\b(?P<field>sajttitel|sajtens\s+titel|sajt(?:en|ens)\s+titel|"
    r"webbplatsens\s+titel|webbplats(?:en|ens)\s+titel|"
    r"tagline(?:n)?|undertitel(?:n)?|sajtbeskrivning(?:en)?|"
    r"webbplatsens\s+beskrivning|webbplats(?:en|ens)\s+beskrivning)\b"
    r"\s*(?:till|to|:)\s*(?P<value>.+)$",
    re.I | re.S,
)
_THEME_LIST_RE = re.compile(
    r"\b(?:visa|lista|hämta|hamta|vilka|vilket|which|list)\s+"
    r"(?:det\s+)?(?:aktiva?\s+|active\s+)?(?:tema(?:t|n|er)?|themes?)\b"
    r"|\baktiv(?:t|a)?\s+tema\b"
    r"|\b(?:tema|theme)\s+(?:är|ar|listan)\b",
    re.I,
)
_PLUGIN_LIST_RE = re.compile(
    r"\b(?:visa|lista|hämta|hamta|vilka|which|list)\s+"
    r"(?:aktiva?\s+|active\s+)?(?:plugins?|till[äa]gg(?:en)?)\b"
    r"|\b(?:plugins?|till[äa]gg)\s+(?:är|ar|listan|status)\b",
    re.I,
)
_PLUGIN_ID_TOKEN = r"(?P<id>[A-Za-z0-9][A-Za-z0-9_./-]{0,80})"
_PLUGIN_ON_RE = re.compile(
    rf"\b(?:aktivera|sl[aå]\s+p[aå]|s[äa]tt\s+p[aå])\s+"
    rf"(?:plugin(?:et)?|till[äa]gget?)\s+{_PLUGIN_ID_TOKEN}",
    re.I,
)
_PLUGIN_OFF_RE = re.compile(
    rf"\b(?:inaktivera|avaktivera|deaktivera|st[äa]ng\s+av|sl[aå]\s+av)\s+"
    rf"(?:plugin(?:et)?|till[äa]gget?)\s+{_PLUGIN_ID_TOKEN}",
    re.I,
)
_CRITICAL_PLUGIN_SLUGS = frozenset(CRITICAL_PLUGIN_SLUGS) | frozenset({"vipps-mobilepay"})
_REFUSE_BRIDGE_DEACTIVATE = (
    f"Pluginet {BRIDGE_PLUGIN_SLUG} är självskyddat och får inte inaktiveras via chatten."
)
_POST_STATUS = {
    "utkast": "draft",
    "draft": "draft",
    "publicera": "publish",
    "publicerad": "publish",
    "publish": "publish",
    "privat": "private",
    "private": "private",
    "väntande": "pending",
    "vantande": "pending",
    "pending": "pending",
}
_PLUGIN_STATUS_SV = {"active": "aktiv", "inactive": "inaktiv"}
_REFUSE_PACKAGE = (
    "Installation, uppdatering och radering väntar. "
    "Jag kan visa teman och plugins, och efter Ja slå på eller av ett befintligt plugin."
)
_REFUSE_THEME = (
    "Temabyte och temafiler görs inte i chatten. Jag kan visa vilket tema som är aktivt."
)
_REFUSE_FILE = "Tema- och pluginfiler ändras inte i chatten."
_REFUSE_POST_DELETE = "Inlägg raderas inte här. Jag kan visa dem och föreslå titel, text eller status."
_REFUSE_POST_CREATE = "Nya inlägg skapas inte här. Jag kan visa ett inlägg och föreslå en ändring."


@dataclass
class SiteTurn:
    consumed: bool
    results: list[tuple[str, str]] = field(default_factory=list)
    pending: PendingAction | None = None
    sticky_post_id: str | None = None
    sticky_product_id: str | None = None
    sticky_page_id: str | None = None


@dataclass
class _Write:
    kind: str
    target_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    preview: str = ""
    error: str | None = None


def site_content_turn(
    text: str,
    *,
    domain: str | None = None,
    sticky_post_id: str | None = None,
    allow_wp_ops_fallthrough: bool = False,
) -> SiteTurn:
    raw = text or ""
    effective_fallthrough = allow_wp_ops_fallthrough or bool(_WP_WORKSPACE_INTENT_RE.search(raw))
    refusal = _refusal(raw, allow_wp_ops_fallthrough=effective_fallthrough)
    if refusal:
        return SiteTurn(consumed=True, results=[("site_content", refusal)])
    if effective_fallthrough and (
        _FILE_RE.search(raw) or _WP_CODE_FALLTHROUGH_RE.search(raw)
    ):
        return SiteTurn(consumed=False)
    write = _parse_write(raw, sticky_post_id=sticky_post_id)
    if write is not None:
        if write.error:
            return SiteTurn(consumed=True, results=[("site_content", write.error)])
        if write.kind == "post_update":
            return _propose_post(write, domain=domain)
        if write.kind == "site_update":
            return _propose_site(write, domain=domain)
        return _propose_plugin(write, domain=domain)
    if _WP_CODE_FALLTHROUGH_RE.search(raw):
        return SiteTurn(consumed=False)
    read_name, target = _parse_read(raw)
    if read_name is None:
        return SiteTurn(consumed=False)
    if read_name == "list_posts":
        return _read_posts(domain=domain, status="any" if re.search(r"\balla\b", raw, re.I) else "publish")
    if read_name == "show_post":
        return _read_post(target or "", domain=domain)
    if read_name == "show_site":
        return _read_site(domain=domain)
    if read_name == "list_themes":
        return _read_themes(domain=domain)
    return _read_plugins(domain=domain)


def site_tool_result(
    name: str,
    args: dict[str, Any] | None,
    *,
    domain: str | None = None,
) -> str | None:
    """Run a model-written content read. Refuses writes."""
    tool = (name or "").strip()
    if tool in {
        "update_post",
        "update_settings",
        "update_site_identity",
        "set_plugin_status",
        "install_plugin",
        "delete_plugin",
        "switch_theme",
        "propose_post_update",
        "propose_site_update",
        "propose_plugin_status",
    }:
        return (
            "Ändringen är inte gjord. Säg vad som ska ändras, "
            "så kommer en bekräftelseknapp."
        )
    raw = args if isinstance(args, dict) else {}
    if tool == "list_posts":
        status = "any" if str(raw.get("status") or "") == "any" else "publish"
        turn = _read_posts(domain=domain, status=status)
    elif tool == "show_post":
        post_id = str(raw.get("post_id") or raw.get("id") or "").strip()
        if not post_id:
            return None
        turn = _read_post(post_id, domain=domain)
    elif tool == "show_site":
        turn = _read_site(domain=domain)
    elif tool == "list_themes":
        turn = _read_themes(domain=domain)
    elif tool == "list_plugins":
        turn = _read_plugins(domain=domain)
    else:
        return None
    if not turn.results:
        return None
    return turn.results[0][1]


def _wp_client(domain: str | None) -> Any:
    """Resolve WordPress client via ``ecom_ops.integrations.wordpress`` so test patches apply."""
    from ecom_ops.integrations import wordpress as wp_mod

    return wp_mod.wp_client_from_env(use_mock=None, domain=domain)


def _run_wp_read(
    *,
    domain: str | None,
    fail_prefix: str,
    build_turn: Callable[[Any], SiteTurn],
) -> SiteTurn:
    try:
        wp = _wp_client(domain)
        return build_turn(wp)
    except Exception as exc:
        return SiteTurn(
            consumed=True,
            results=[("site_content", f"{fail_prefix}: {_public_failure(exc)}")],
        )


def _execute_wp_write(
    *,
    actor: str,
    permission: Any,
    domain: str | None,
    action: str,
    target: str,
    perform: Callable[[Any], tuple[str, dict[str, Any], str]],
) -> tuple[bool, str]:
    from ecom_ops.audit import log_action
    from ecom_ops.rbac import require_permission, resolve_actor

    try:
        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, permission)
        wp = _wp_client(domain)
        target_id, details, message = perform(wp)
    except Exception as exc:
        return False, _fail_text(exc)
    log_action(
        actor=actor_obj.name,
        action=action,
        target=target,
        target_id=target_id,
        details=details,
    )
    return True, message


def execute_post_update(
    *,
    post_id: str,
    fields: dict[str, Any],
    actor: str,
    domain: str | None = None,
) -> tuple[bool, str]:
    from ecom_ops.rbac import Permission, resolve_actor
    from ecom_ops.security import validate_order_id
    from ecom_ops.wp_ops.change_ledger import resolve_ledger_site_id, safe_record_change
    from ecom_ops.wp_ops.config import is_mock_mode

    actor_name = resolve_actor(actor).name

    def _perform(wp: Any) -> tuple[str, dict[str, Any], str]:
        pid = validate_order_id(post_id)
        clean = _revalidate_post_fields(fields)
        before_snap: dict[str, Any] = {}
        try:
            cur = wp.get_post(pid, context="edit")
            raw_c = cur.raw or {}
            c_obj = raw_c.get("content") if isinstance(raw_c.get("content"), dict) else {}
            before_snap = {
                "title": cur.title,
                "content": str(c_obj.get("raw") or c_obj.get("rendered") or raw_c.get("content") or ""),
                "status": cur.status,
            }
        except Exception:
            before_snap = {}
        updated = wp.update_post(
            pid,
            title=clean.get("title"),
            content=clean.get("content"),
            status=clean.get("status"),
        )
        safe_record_change(
            site_id=resolve_ledger_site_id(domain=domain),
            environment="staging" if is_mock_mode() else "production",
            category="post",
            transport="wp_rest",
            actor=actor_name,
            summary=f"Inlägg {updated.id} ({_plain(updated.title)}): {', '.join(sorted(clean))}",
            target_ref=f"post:{updated.id}",
            before_data=before_snap,
            after_data={
                "title": updated.title,
                "content": clean.get("content", before_snap.get("content", "")),
                "status": updated.status,
            },
            metadata={"item_id": str(updated.id), "domain": domain or "", "fields": sorted(clean)},
        )
        shown = ", ".join(sorted(clean))
        return (
            str(updated.id),
            {"fields": sorted(clean), "domain": domain or ""},
            f"Inlägg {updated.id} ({_plain(updated.title)}) är uppdaterat. Fält: {shown}.",
        )

    return _execute_wp_write(
        actor=actor,
        permission=Permission.WP_OPS_OPERATE,
        domain=domain,
        action="telegram_post_update",
        target="post",
        perform=_perform,
    )


def execute_site_update(
    *,
    fields: dict[str, Any],
    actor: str,
    domain: str | None = None,
) -> tuple[bool, str]:
    from ecom_ops.rbac import Permission, resolve_actor
    from ecom_ops.wp_ops.change_ledger import resolve_ledger_site_id, safe_record_change
    from ecom_ops.wp_ops.config import is_mock_mode

    actor_name = resolve_actor(actor).name

    def _perform(wp: Any) -> tuple[str, dict[str, Any], str]:
        clean = _revalidate_site_fields(fields)
        before_snap: dict[str, Any] = {}
        try:
            cur_s = wp.get_settings()
            if isinstance(cur_s, dict):
                before_snap = {k: cur_s.get(k) for k in ("title", "description")}
        except Exception:
            before_snap = {}
        updated = wp.update_site_identity(
            title=clean.get("title"),
            description=clean.get("description"),
        )
        safe_record_change(
            site_id=resolve_ledger_site_id(domain=domain),
            environment="staging" if is_mock_mode() else "production",
            category="settings",
            transport="wp_rest",
            actor=actor_name,
            summary=f"Sajtinställningar: {', '.join(sorted(clean))}",
            target_ref="settings:site",
            before_data=before_snap,
            after_data={k: updated.get(k) for k in ("title", "description")},
            metadata={"domain": domain or "", "fields": sorted(clean)},
        )
        shown = ", ".join(sorted(clean))
        title = _plain(updated.get("title") or "")
        return (
            "site",
            {"fields": sorted(clean), "domain": domain or ""},
            f"Sajten ({title}) är uppdaterad. Fält: {shown}.",
        )

    return _execute_wp_write(
        actor=actor,
        permission=Permission.WP_OPS_OPERATE,
        domain=domain,
        action="telegram_site_update",
        target="settings",
        perform=_perform,
    )


def execute_plugin_status(
    *,
    plugin: str,
    status: str,
    actor: str,
    domain: str | None = None,
) -> tuple[bool, str]:
    from ecom_ops.integrations.wordpress import _validate_plugin_id
    from ecom_ops.rbac import Permission, resolve_actor
    from ecom_ops.wp_ops.change_ledger import resolve_ledger_site_id, safe_record_change
    from ecom_ops.wp_ops.config import is_mock_mode

    if str(status).strip().lower() == "inactive" and is_bridge_plugin(plugin):
        return False, _REFUSE_BRIDGE_DEACTIVATE

    actor_name = resolve_actor(actor).name

    def _perform(wp: Any) -> tuple[str, dict[str, Any], str]:
        plugin_id = _validate_plugin_id(plugin)
        normalized = str(status).strip().lower()
        if normalized not in _PLUGIN_STATUS_SV:
            raise SecurityError("Invalid plugin status")
        if normalized == "inactive" and is_bridge_plugin(plugin_id):
            raise SecurityError(_REFUSE_BRIDGE_DEACTIVATE)
        prev_status = "inactive" if normalized == "active" else "active"
        try:
            for row in wp.list_plugins():
                if str(row.get("plugin") or "").strip().lower() == plugin_id:
                    prev_status = str(row.get("status") or prev_status).strip().lower()
                    break
        except Exception:
            pass
        updated = wp.set_plugin_status(plugin_id, normalized)
        safe_record_change(
            site_id=resolve_ledger_site_id(domain=domain),
            environment="staging" if is_mock_mode() else "production",
            category="plugin_toggle",
            transport="wp_rest",
            actor=actor_name,
            summary=f"Plugin {plugin_id}: {prev_status} → {normalized}",
            target_ref=f"plugin:{plugin_id}",
            before_data={"plugin": plugin_id, "status": prev_status},
            after_data={"plugin": plugin_id, "status": normalized},
            metadata={"plugin": plugin_id, "domain": domain or ""},
        )
        name = _plain(updated.get("name") or plugin_id)
        label = _PLUGIN_STATUS_SV[normalized]
        return (
            plugin_id,
            {"status": normalized, "domain": domain or ""},
            f"Plugin {name} ({plugin_id}) är {label}.",
        )

    return _execute_wp_write(
        actor=actor,
        permission=Permission.WP_OPS_ADMIN,
        domain=domain,
        action="telegram_plugin_status",
        target="plugin",
        perform=_perform,
    )


def _refusal(text: str, *, allow_wp_ops_fallthrough: bool = False) -> str | None:
    if not allow_wp_ops_fallthrough and _FILE_RE.search(text):
        return _REFUSE_FILE
    package_re = _PACKAGE_FALLTHROUGH_REFUSE_RE if allow_wp_ops_fallthrough else _PACKAGE_REFUSE_RE
    if package_re.search(text):
        return _REFUSE_PACKAGE
    theme_re = _THEME_FALLTHROUGH_REFUSE_RE if allow_wp_ops_fallthrough else _THEME_TOUCH_RE
    if theme_re.search(text):
        return _REFUSE_THEME
    if _POST_DELETE_RE.search(text):
        return _REFUSE_POST_DELETE
    if _POST_CREATE_RE.search(text):
        return _REFUSE_POST_CREATE
    return None


def _parse_write(text: str, *, sticky_post_id: str | None) -> _Write | None:
    plugin_on = _PLUGIN_ON_RE.search(text)
    plugin_off = _PLUGIN_OFF_RE.search(text)
    if plugin_on or plugin_off:
        if plugin_on and (plugin_off is None or plugin_on.start() <= plugin_off.start()):
            match = plugin_on
            status = "active"
        else:
            match = plugin_off
            status = "inactive"
        if match is None:
            return None
        return _Write(
            kind="plugin_status",
            target_id=_clean_token(match.group("id")),
            fields={"status": status},
            label="status",
            preview=_PLUGIN_STATUS_SV[status],
        )
    site = _SITE_FIELD_RE.search(text)
    if site:
        return _site_write(site.group("field"), site.group("value"))
    if not _POST_NOUN_RE.search(text):
        return None
    field_match = _POST_FIELD_RE.search(text)
    if not field_match:
        return None
    return _post_write(text, field_match.group("field"), field_match.group("value"), sticky_post_id)


def _parse_read(text: str) -> tuple[str | None, str | None]:
    show = _POST_SHOW_RE.search(text)
    if show and show.group(1):
        return "show_post", show.group(1)
    if _POST_LIST_RE.search(text):
        return "list_posts", None
    if _SITE_SHOW_RE.search(text):
        return "show_site", None
    if _THEME_LIST_RE.search(text):
        return "list_themes", None
    if _PLUGIN_LIST_RE.search(text):
        return "list_plugins", None
    return None, None


def _post_write(text: str, token: str, value: str, sticky_post_id: str | None) -> _Write:
    cleaned = _strip_value(value)
    if not cleaned:
        return _Write(kind="post_update", error="Skriv vad det ska ändras till.")
    try:
        fields, label, preview = _coerce_post_field(token, cleaned)
    except SecurityError as exc:
        return _Write(kind="post_update", error=_coerce_error(exc))
    id_match = _POST_ID_RE.search(text)
    target = id_match.group(1) if id_match else None
    if target is None and sticky_post_id and re.search(r"\b(?:inl[äa]gget|posten)\b", text, re.I):
        target = str(sticky_post_id)
    if target is None:
        return _Write(
            kind="post_update",
            error="Vilket inlägg? Säg inläggsnumret, till exempel inlägg 5.",
        )
    return _Write(
        kind="post_update",
        target_id=target,
        fields=fields,
        label=label,
        preview=preview,
    )


def _site_write(token: str, value: str) -> _Write:
    cleaned = _strip_value(value)
    if not cleaned:
        return _Write(kind="site_update", error="Skriv vad det ska ändras till.")
    key = _site_storage_key(token)
    try:
        if key == "title":
            stored = sanitize_text(cleaned, max_len=200)
            label = "sajttitel"
        else:
            stored = sanitize_text(cleaned, max_len=300)
            label = "tagline"
    except SecurityError as exc:
        return _Write(kind="site_update", error=_coerce_error(exc))
    return _Write(
        kind="site_update",
        fields={key: stored},
        label=label,
        preview=_preview(stored),
    )


def _site_storage_key(token: str) -> str:
    flat = re.sub(r"\s+", " ", token.lower()).strip()
    if any(word in flat for word in ("tagline", "undertitel", "beskrivning")):
        return "description"
    return "title"


def _coerce_post_field(token: str, value: str) -> tuple[dict[str, Any], str, str]:
    flat = token.lower()
    if flat == "status":
        mapped = _POST_STATUS.get(value.lower())
        if not mapped:
            raise SecurityError("Ogiltig status")
        return {"status": mapped}, "status", mapped
    if flat in {"titel", "title"}:
        stored = sanitize_text(value, max_len=300)
        return {"title": stored}, "titel", _preview(stored)
    stored = sanitize_text(value, max_len=8000)
    return {"content": stored}, "text", _preview(stored)


def _propose_post(write: _Write, *, domain: str | None) -> SiteTurn:
    def _build(wp: Any) -> SiteTurn:
        post = wp.get_post(write.target_id or "")
        pid = str(post.id)
        pending = PendingAction(
            kind="post_update",
            payload={
                "post_id": pid,
                "fields": dict(write.fields),
                "label": write.label,
                "preview": write.preview,
                "domain": domain,
            },
        )
        propose = (
            f"FÖRESLÅ ändring (ej utförd): inlägg {pid} ({_plain(post.title)}) "
            f"{write.label} → {write.preview}."
        )
        return SiteTurn(
            consumed=True,
            results=[
                ("show_post", _render_post(post)),
                ("propose_post_update", propose),
            ],
            pending=pending,
            sticky_post_id=pid,
        )

    return _run_wp_read(domain=domain, fail_prefix="Inlägget svarade inte", build_turn=_build)


def _propose_site(write: _Write, *, domain: str | None) -> SiteTurn:
    def _build(wp: Any) -> SiteTurn:
        settings = wp.get_settings()
        pending = PendingAction(
            kind="site_update",
            payload={
                "fields": dict(write.fields),
                "label": write.label,
                "preview": write.preview,
                "domain": domain,
            },
        )
        propose = f"FÖRESLÅ ändring (ej utförd): {write.label} → {write.preview}."
        return SiteTurn(
            consumed=True,
            results=[
                ("show_site", _render_site(settings, host=_host(wp))),
                ("propose_site_update", propose),
            ],
            pending=pending,
        )

    return _run_wp_read(domain=domain, fail_prefix="Sajten svarade inte", build_turn=_build)


def _propose_plugin(write: _Write, *, domain: str | None) -> SiteTurn:
    token = write.target_id or ""

    def _build(wp: Any) -> SiteTurn:
        rows = wp.list_plugins()
        plugin, error = _resolve_plugin(rows, token)
        if error or plugin is None:
            return SiteTurn(consumed=True, results=[("site_content", error or "Pluginet saknas.")])
        plugin_id = str(plugin.get("plugin") or "")
        slug = normalize_plugin_slug(plugin_id)
        if write.fields.get("status") == "inactive" and is_bridge_plugin(slug):
            return SiteTurn(consumed=True, results=[("site_content", _REFUSE_BRIDGE_DEACTIVATE)])
        is_critical = (
            write.fields.get("status") == "inactive" and slug in _CRITICAL_PLUGIN_SLUGS
        )
        payload: dict[str, Any] = {
            "plugin": plugin_id,
            "status": write.fields.get("status"),
            "name": _component_name(plugin),
            "label": write.label,
            "preview": write.preview,
            "domain": domain,
        }
        if is_critical:
            payload["critical"] = True
        pending = PendingAction(kind="plugin_status", payload=payload)
        current = _PLUGIN_STATUS_SV.get(
            str(plugin.get("status") or ""), str(plugin.get("status") or "")
        )
        propose = (
            f"FÖRESLÅ ändring (ej utförd): plugin {plugin_id} "
            f"({_component_name(plugin)}) → {write.preview}. Nu: {current}."
        )
        if is_critical:
            propose = (
                "VARNING: kritiskt e-handelsplugin — inaktivering kan slå ut kassa/butik. "
                + propose
            )
        return SiteTurn(
            consumed=True,
            results=[
                ("list_plugins", _render_plugins([plugin], host=_host(wp))),
                ("propose_plugin_status", propose),
            ],
            pending=pending,
        )

    return _run_wp_read(domain=domain, fail_prefix="Pluginlistan svarade inte", build_turn=_build)


def _read_posts(*, domain: str | None, status: str) -> SiteTurn:
    def _build(wp: Any) -> SiteTurn:
        rows = wp.list_posts(per_page=20, status=status)
        sticky = str(rows[0].id) if len(rows) == 1 else None
        return SiteTurn(
            consumed=True,
            results=[("list_posts", _render_posts(rows, host=_host(wp)))],
            sticky_post_id=sticky,
        )

    return _run_wp_read(domain=domain, fail_prefix="Inläggslistan svarade inte", build_turn=_build)


def _read_post(post_id: str, *, domain: str | None) -> SiteTurn:
    def _build(wp: Any) -> SiteTurn:
        post = wp.get_post(post_id)
        return SiteTurn(
            consumed=True,
            results=[("show_post", _render_post(post))],
            sticky_post_id=str(post.id),
        )

    return _run_wp_read(domain=domain, fail_prefix="Inlägget svarade inte", build_turn=_build)


def _read_site(*, domain: str | None) -> SiteTurn:
    def _build(wp: Any) -> SiteTurn:
        settings = wp.get_settings()
        return SiteTurn(
            consumed=True,
            results=[("show_site", _render_site(settings, host=_host(wp)))],
        )

    return _run_wp_read(domain=domain, fail_prefix="Sajten svarade inte", build_turn=_build)


def _read_themes(*, domain: str | None) -> SiteTurn:
    def _build(wp: Any) -> SiteTurn:
        rows = wp.list_themes()
        return SiteTurn(
            consumed=True,
            results=[("list_themes", _render_themes(rows, host=_host(wp)))],
        )

    return _run_wp_read(domain=domain, fail_prefix="Temalistan svarade inte", build_turn=_build)


def _read_plugins(*, domain: str | None) -> SiteTurn:
    def _build(wp: Any) -> SiteTurn:
        rows = wp.list_plugins()
        return SiteTurn(
            consumed=True,
            results=[("list_plugins", _render_plugins(rows, host=_host(wp)))],
        )

    return _run_wp_read(domain=domain, fail_prefix="Pluginlistan svarade inte", build_turn=_build)


def _render_posts(rows: list[Any], *, host: str) -> str:
    if not rows:
        return f"WordPress {host} svarade med en tom inläggslista."
    lines = [f"Inlägg på {host} ({len(rows)}):"]
    for row in rows[:20]:
        lines.append(
            f"- {row.id} | {_plain(row.title)} | {row.status} | {row.link or ''}".rstrip()
        )
    lines.append("Säg ett inläggsnummer, eller «ändra inlägg 5 titel till …».")
    return "\n".join(lines)


def _render_post(post: Any) -> str:
    raw = post.raw if isinstance(post.raw, dict) else {}
    excerpt = _excerpt(raw.get("content") or raw.get("excerpt"))
    lines = [f"Inlägg {post.id} | {_plain(post.title)} | {post.status}"]
    if post.link:
        lines.append(str(post.link))
    if excerpt:
        lines.append(excerpt)
    lines.append(
        "Säg «ändra inlägget titel till …» eller «ändra inlägget text till …». "
        "Inget sparas förrän du trycker Ja."
    )
    return "\n".join(lines)


def _render_site(settings: dict[str, Any], *, host: str) -> str:
    title = _plain(settings.get("title") or "")
    tagline = _plain(settings.get("description") or "")
    return (
        f"Sajt på {host}\n"
        f"Titel: {title or '—'}\n"
        f"Tagline: {tagline or '—'}\n"
        "Säg «ändra sajttitel till …» eller «ändra tagline till …». "
        "Inget sparas förrän du trycker Ja."
    )


def _render_themes(rows: list[dict[str, Any]], *, host: str) -> str:
    if not rows:
        return f"WordPress {host} svarade med en tom temalista."
    ordered = _active_first(rows)
    lines = [f"Teman på {host} ({len(ordered)}):"]
    for row in ordered[:40]:
        stylesheet = str(row.get("stylesheet") or row.get("name") or "")
        status = _PLUGIN_STATUS_SV.get(str(row.get("status") or ""), str(row.get("status") or ""))
        lines.append(f"- {stylesheet} | {_component_name(row)} | {status}".rstrip())
    lines.append("Temabyte, installation och temafiler görs inte här.")
    return "\n".join(lines)


def _render_plugins(rows: list[dict[str, Any]], *, host: str) -> str:
    if not rows:
        return f"WordPress {host} svarade med en tom pluginlista."
    ordered = _active_first(rows)
    lines = [f"Plugins på {host} ({len(ordered)}):"]
    for row in ordered[:40]:
        plugin_id = str(row.get("plugin") or "")
        status = _PLUGIN_STATUS_SV.get(str(row.get("status") or ""), str(row.get("status") or ""))
        lines.append(f"- {plugin_id} | {_component_name(row)} | {status}".rstrip())
    if len(ordered) > 40:
        lines.append(f"Visar 40 av {len(ordered)}.")
    lines.append(
        "Säg «aktivera plugin …» eller «stäng av plugin …». "
        "Inget ändras förrän du trycker Ja."
    )
    return "\n".join(lines)


def _resolve_plugin(
    rows: list[dict[str, Any]],
    token: str,
) -> tuple[dict[str, Any] | None, str | None]:
    from ecom_ops.integrations.wordpress import _validate_plugin_id

    wanted = _clean_token(token).lower()
    if not wanted:
        return None, "Vilket plugin? Säg namnet, till exempel aktivera plugin akismet."
    exact: list[dict[str, Any]] = []
    try:
        plugin_id = _validate_plugin_id(wanted)
    except SecurityError:
        plugin_id = ""
    else:
        exact = [row for row in rows if str(row.get("plugin") or "").lower() == plugin_id]
    if len(exact) == 1:
        return exact[0], None
    fuzzy = [
        row
        for row in rows
        if wanted == _component_name(row).lower()
        or wanted in [part.lower() for part in str(row.get("plugin") or "").split("/") if part]
    ]
    if len(fuzzy) == 1:
        return fuzzy[0], None
    if not exact and not fuzzy:
        return None, f"Ingen plugin matchar «{wanted}»."
    lines = [f"Flera plugins matchar «{wanted}». Säg hela id:t:"]
    for row in (exact + fuzzy)[:8]:
        lines.append(f"- {row.get('plugin')} | {_component_name(row)}")
    return None, "\n".join(lines)


def _revalidate_post_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if key == "title":
            clean["title"] = sanitize_text(str(value), max_len=300)
        elif key == "content":
            clean["content"] = sanitize_text(str(value), max_len=8000)
        elif key == "status":
            if str(value) not in {"draft", "publish", "private", "pending"}:
                raise SecurityError("Ogiltig status")
            clean["status"] = str(value)
        else:
            raise SecurityError("No post fields to update")
    if not clean:
        raise SecurityError("No post fields to update")
    return clean


def _revalidate_site_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if key == "title":
            clean["title"] = sanitize_text(str(value), max_len=200)
        elif key == "description":
            clean["description"] = sanitize_text(str(value), max_len=300)
        else:
            raise SecurityError("No site fields to update")
    if not clean:
        raise SecurityError("No site fields to update")
    return clean


def _active_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: 0 if str(row.get("status") or "") == "active" else 1)


def _component_name(row: dict[str, Any]) -> str:
    return _plain(row.get("name") or row.get("stylesheet") or row.get("plugin") or "")


def _host(client: Any) -> str:
    return urlparse(getattr(client, "base_url", "") or "").hostname or str(
        getattr(client, "base_url", "") or ""
    )


def _clean_token(value: str) -> str:
    return value.strip().strip(".,!?;:\"'«»")


def _strip_value(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("«") and text.endswith("»") and len(text) >= 2:
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def _preview(value: str) -> str:
    flat = re.sub(r"\s+", " ", value).strip()
    if len(flat) <= 180:
        return flat
    return flat[:180] + "…"


def _excerpt(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("rendered") or value.get("raw") or ""
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= 180:
        return text
    return text[:180] + "…"


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("rendered") or value.get("raw") or ""
    return html.unescape(str(value or "")).strip()


def _coerce_error(exc: SecurityError) -> str:
    message = str(exc).lower()
    if "status" in message:
        return "Status kan vara utkast, publicerad, privat eller väntande."
    if "max length" in message or "too long" in message or "exceeds" in message:
        return "Texten är för lång för att sparas i chatten."
    if "empty" in message:
        return "Skriv vad det ska ändras till."
    return "Värdet kunde inte tolkas."


def _public_failure(exc: Exception) -> str:
    match = re.search(r"API error (\d+)", str(exc))
    if match:
        return f"HTTP {match.group(1)}"
    if "transport error" in str(exc).lower():
        return "nätverksfel"
    return type(exc).__name__


def _fail_text(exc: Exception) -> str:
    from ecom_ops.rbac import AccessDenied

    if isinstance(exc, AccessDenied):
        return str(exc)
    public = _public_failure(exc)
    if public.startswith("HTTP") or public == "nätverksfel":
        return f"Skrivningen svarade inte: {public}"
    return "Värdet kunde inte sparas."
