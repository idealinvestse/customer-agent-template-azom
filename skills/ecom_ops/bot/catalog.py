"""Telegram catalog reads and confirm-only product/page edits.

Writes stay in the pending payload until the confirm button. This module
never puts the new text into a callback.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ecom_ops.bot.dialog_actions import PendingAction
from ecom_ops.security import SecurityError, sanitize_text, validate_order_id

_VERB_RE = re.compile(
    r"\b(?:ändra|andra|sätt|satt|uppdatera|byt|set|change|update)\b",
    re.I,
)
_FIELD_WORDS = (
    "lagerstatus",
    "kort beskrivning",
    "short description",
    "produkttext",
    "beskrivningen",
    "beskrivning",
    "innehållet",
    "innehallet",
    "innehåll",
    "innehall",
    "priset",
    "price",
    "pris",
    "lagersaldo",
    "lagret",
    "lager",
    "stock",
    "namn",
    "name",
    "titel",
    "title",
    "content",
    "status",
    "texten",
    "text",
)
_FIELD_ALT = "|".join(re.escape(word) for word in _FIELD_WORDS)
_FIELD_RE = re.compile(
    rf"\b(?P<field>{_FIELD_ALT})\b\s*(?:till|to|:)\s*(?P<value>.+)$",
    re.I | re.S,
)
_PRODUCT_NOUN_RE = re.compile(r"\b(?:produkt(?:en|er)?|product)\b", re.I)
_PAGE_NOUN_RE = re.compile(r"\b(?:sid(?:an|a|or|orna|e)|page)\b", re.I)
_PRODUCT_ID_RE = re.compile(r"\b(?:produkt(?:en)?|product)\s*#?\s*(\d{1,12})\b", re.I)
_PAGE_ID_RE = re.compile(r"\b(?:sid(?:an|a)|page)\s*#?\s*(\d{1,12})\b", re.I)
_PRODUCT_QUERY_RE = re.compile(
    rf"\bprodukt(?:en)?\s+(.+?)\s+(?:{_FIELD_ALT})\b",
    re.I | re.S,
)
_PAGE_QUERY_RE = re.compile(
    r"\bsid(?:an|a|e)\s+(.+?)\s+(?:"
    r"titel|title|texten|text|innehållet|innehallet|innehåll|innehall|"
    r"content|status"
    r")\b",
    re.I | re.S,
)
_PRODUCT_SHOW_RE = re.compile(
    r"\b(?:visa|öppna|oppna|kolla|visa\s+mig)?\s*produkt(?:en)?\s*#?\s*(\d{1,12})\b",
    re.I,
)
_PRODUCT_LIST_RE = re.compile(
    r"\b(?:visa|lista|hämta|hamta|vilka|n[äa]sta|fler(?:a)?|mer)\s+"
    r"(?:\d{1,2}\s+)?(?:alla\s+)?produkter\b"
    r"|\bprodukt(?:data|katalog(?:en)?)\b"
    r"|\b(?:sök|sok|hitta)\s+produkter?\b",
    re.I,
)
_PRODUCT_SEARCH_RE = re.compile(
    r"\b(?:sök|sok|hitta)\s+produkter?\s+(.+)$"
    r"|\bprodukter\s+(?:som\s+heter|med|om)\s+(.+)$",
    re.I,
)
_PAGE_LIST_RE = re.compile(
    r"\b(?:visa|lista|hämta|hamta|vilka|n[äa]sta|fler(?:a)?)\s+"
    r"(?:\d{1,2}\s+)?sidor(?:na)?\b",
    re.I,
)
_PAGE_TITLE_RE = re.compile(
    r"(?:^|\b(?:visa|öppna|oppna|kolla|hämta|hamta)\s+)sid(?:an|a)\s+(.+)$",
    re.I,
)
_STICKY_PRODUCT_RE = re.compile(r"\b(?:visa|öppna|oppna|kolla)\s+produkten\b", re.I)
_STICKY_PAGE_RE = re.compile(r"\b(?:visa|öppna|oppna|kolla)\s+sidan\s*$", re.I)
_ORDER_STATUS_WORDS = frozenset(
    {
        "completed",
        "complete",
        "klar",
        "färdig",
        "fardig",
        "levererad",
        "processing",
        "behandles",
        "behandlas",
        "pending",
        "väntande",
        "vantande",
        "cancelled",
        "canceled",
        "avbruten",
        "refunded",
        "återbetald",
        "aterbetald",
        "failed",
        "misslyckad",
        "on-hold",
        "on hold",
        "pausad",
    }
)
_PAGE_STATUS = {
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
_STOCK_STATUS = {
    "i lager": "instock",
    "på lager": "instock",
    "pa lager": "instock",
    "instock": "instock",
    "slut": "outofstock",
    "slutsåld": "outofstock",
    "slutsald": "outofstock",
    "outofstock": "outofstock",
    "restorder": "onbackorder",
    "restnoterad": "onbackorder",
    "onbackorder": "onbackorder",
}
_STOCK_LABEL = {
    "instock": "i lager",
    "outofstock": "slut",
    "onbackorder": "restorder",
}


@dataclass
class CatalogWrite:
    kind: str
    target_id: str | None = None
    query: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    preview: str = ""
    error: str | None = None


@dataclass
class CatalogRead:
    kind: str
    target_id: str | None = None
    query: str | None = None
    status: str = "publish"


@dataclass
class CatalogTurn:
    consumed: bool
    results: list[tuple[str, str]] = field(default_factory=list)
    pending: PendingAction | None = None
    sticky_product_id: str | None = None
    sticky_page_id: str | None = None


def catalog_write_turn(
    text: str,
    *,
    domain: str | None = None,
    sticky_product_id: str | None = None,
    sticky_page_id: str | None = None,
) -> CatalogTurn:
    write = parse_catalog_write(
        text,
        sticky_product_id=sticky_product_id,
        sticky_page_id=sticky_page_id,
    )
    if write is None:
        return CatalogTurn(consumed=False)
    if write.error:
        return CatalogTurn(consumed=True, results=[("catalog", write.error)])
    if write.kind == "product_update":
        return _propose_product(write, domain=domain)
    return _propose_page(write, domain=domain)


def catalog_read_turn(
    text: str,
    *,
    domain: str | None = None,
    sticky_product_id: str | None = None,
    sticky_page_id: str | None = None,
) -> CatalogTurn:
    read = parse_catalog_read(text)
    if read is None:
        return CatalogTurn(consumed=False)
    if read.kind == "show_product" and not read.target_id and sticky_product_id:
        read.target_id = str(sticky_product_id)
    if read.kind == "show_page" and not read.target_id and not read.query and sticky_page_id:
        read.target_id = str(sticky_page_id)
    if read.kind in {"list_products", "show_product"}:
        return _read_product(read, domain=domain)
    return _read_page(read, domain=domain)


def catalog_tool_result(
    name: str,
    args: dict[str, Any] | None,
    *,
    domain: str | None = None,
) -> str | None:
    """Run a model-written catalog read. Refuses writes."""
    tool = (name or "").strip()
    if tool in {
        "update_product",
        "update_page",
        "propose_product_update",
        "propose_page_update",
    }:
        return (
            "Ändringen är inte gjord. Säg vilket fält som ska ändras, "
            "så kommer en bekräftelseknapp."
        )
    raw = args if isinstance(args, dict) else {}
    if tool == "list_products":
        read = CatalogRead(
            kind="list_products",
            query=str(raw.get("search") or raw.get("query") or "") or None,
            status="any" if str(raw.get("status") or "") == "any" else "publish",
        )
        turn = _read_product(read, domain=domain)
    elif tool == "show_product":
        pid = str(raw.get("product_id") or raw.get("id") or "").strip()
        if not pid:
            return None
        turn = _read_product(CatalogRead(kind="show_product", target_id=pid), domain=domain)
    elif tool == "list_pages":
        query = str(raw.get("search") or raw.get("query") or "") or None
        turn = _read_page(CatalogRead(kind="list_pages", query=query), domain=domain)
    elif tool == "show_page":
        pid = str(raw.get("page_id") or raw.get("id") or "").strip()
        query = str(raw.get("search") or raw.get("title") or "") or None
        turn = _read_page(
            CatalogRead(kind="show_page", target_id=pid or None, query=query),
            domain=domain,
        )
    else:
        return None
    if not turn.results:
        return None
    return turn.results[0][1]


def parse_catalog_write(
    text: str,
    *,
    sticky_product_id: str | None = None,
    sticky_page_id: str | None = None,
) -> CatalogWrite | None:
    raw = text or ""
    field_match = _FIELD_RE.search(raw)
    if not field_match:
        return None
    has_verb = bool(_VERB_RE.search(raw))
    has_noun = bool(_PRODUCT_NOUN_RE.search(raw) or _PAGE_NOUN_RE.search(raw))
    if not has_verb and not has_noun:
        return None
    token = re.sub(r"\s+", " ", field_match.group("field").lower()).strip()
    value = _strip_value(field_match.group("value"))
    if not value:
        return CatalogWrite(kind="catalog", error="Skriv vad det ska ändras till.")
    if token == "status" and not _PAGE_NOUN_RE.search(raw):
        if value.lower() in _ORDER_STATUS_WORDS:
            return None
    kind = _write_kind(raw, token, sticky_product_id, sticky_page_id)
    if kind is None:
        return CatalogWrite(
            kind="catalog",
            error="Säg om det gäller en produkt eller en sida, och vilket fält.",
        )
    try:
        fields, label, preview = _coerce_field(kind, token, value)
    except SecurityError as exc:
        return CatalogWrite(kind=kind, error=_coerce_error(exc))
    target_id, query = _write_target(raw, kind)
    if target_id is None and query is None:
        sticky = sticky_product_id if kind == "product_update" else sticky_page_id
        if sticky:
            target_id = str(sticky)
        else:
            if kind == "product_update":
                error = "Vilken produkt? Säg produktnumret, till exempel produkt 501."
            else:
                error = "Vilken sida? Säg sidnummer eller sidans titel."
            return CatalogWrite(kind=kind, error=error)
    return CatalogWrite(
        kind=kind,
        target_id=target_id,
        query=query,
        fields=fields,
        label=label,
        preview=preview,
    )


def parse_catalog_read(text: str) -> CatalogRead | None:
    raw = (text or "").strip()
    if not raw:
        return None
    product_show = _PRODUCT_SHOW_RE.search(raw)
    page_id = _PAGE_ID_RE.search(raw)
    product_list = _PRODUCT_LIST_RE.search(raw)
    page_list = _PAGE_LIST_RE.search(raw)
    if product_show and (page_id is None or product_show.start() <= page_id.start()):
        return CatalogRead(kind="show_product", target_id=product_show.group(1))
    if page_id and (product_show is None or page_id.start() < product_show.start()):
        return CatalogRead(kind="show_page", target_id=page_id.group(1))
    if product_list and (page_list is None or product_list.start() <= page_list.start()):
        return CatalogRead(
            kind="list_products",
            query=_product_search(raw),
            status="any" if re.search(r"\balla\b", raw, re.I) else "publish",
        )
    if page_list:
        return CatalogRead(kind="list_pages")
    title = _PAGE_TITLE_RE.search(raw)
    if title and not re.fullmatch(r"\d{1,12}", title.group(1).strip()):
        query = _strip_value(title.group(1))
        if query:
            return CatalogRead(kind="show_page", query=query[:60])
    if _STICKY_PRODUCT_RE.search(raw):
        return CatalogRead(kind="show_product")
    if _STICKY_PAGE_RE.search(raw):
        return CatalogRead(kind="show_page")
    return None


def execute_product_update(
    *,
    product_id: str,
    fields: dict[str, Any],
    actor: str,
    domain: str | None = None,
) -> tuple[bool, str]:
    from ecom_ops.audit import log_action
    from ecom_ops.integrations.woocommerce import client_from_env
    from ecom_ops.rbac import Permission, require_permission, resolve_actor

    try:
        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.PRODUCT_DESC_WRITE)
        pid = validate_order_id(product_id)
        clean = _revalidate_product_fields(fields)
        woo = client_from_env(use_mock=None, domain=domain)
        updated = woo.update_product(pid, clean)
    except Exception as exc:
        return False, _fail_text(exc)
    name = str((updated or {}).get("name") or pid)
    log_action(
        actor=actor_obj.name,
        action="telegram_product_update",
        target="product",
        target_id=pid,
        details={"fields": sorted(clean), "domain": domain or ""},
    )
    shown = ", ".join(sorted(clean))
    return True, f"Produkt {pid} ({name}) är uppdaterad. Fält: {shown}."


def execute_page_update(
    *,
    page_id: str,
    fields: dict[str, Any],
    actor: str,
    domain: str | None = None,
) -> tuple[bool, str]:
    from ecom_ops.audit import log_action
    from ecom_ops.integrations.wordpress import wp_client_from_env
    from ecom_ops.rbac import Permission, require_permission, resolve_actor

    try:
        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.WP_OPS_OPERATE)
        pid = validate_order_id(page_id)
        clean = _revalidate_page_fields(fields)
        wp = wp_client_from_env(use_mock=None, domain=domain)
        updated = wp.update_page(
            pid,
            title=clean.get("title"),
            content=clean.get("content"),
            status=clean.get("status"),
        )
    except Exception as exc:
        return False, _fail_text(exc)
    log_action(
        actor=actor_obj.name,
        action="telegram_page_update",
        target="page",
        target_id=str(updated.id),
        details={"fields": sorted(clean), "domain": domain or ""},
    )
    shown = ", ".join(sorted(clean))
    return True, f"Sida {updated.id} ({updated.title}) är uppdaterad. Fält: {shown}."


def _propose_product(write: CatalogWrite, *, domain: str | None) -> CatalogTurn:
    from ecom_ops.integrations.woocommerce import client_from_env

    try:
        woo = client_from_env(use_mock=None, domain=domain)
    except Exception as exc:
        return CatalogTurn(
            consumed=True,
            results=[("catalog", f"Produkten svarade inte: {_public_api_failure(exc)}")],
        )
    product, error = _resolve_product(woo, write)
    if error or product is None:
        return CatalogTurn(consumed=True, results=[("catalog", error or "Produkten saknas.")])
    pid = str(product.get("id") or "")
    pending = PendingAction(
        kind="product_update",
        payload={
            "product_id": pid,
            "fields": dict(write.fields),
            "label": write.label,
            "preview": write.preview,
            "domain": domain,
        },
    )
    current = _plain_title(product.get("name") or pid)
    propose = (
        f"FÖRESLÅ ändring (ej utförd): produkt {pid} {write.label} → {write.preview}. "
        f"Nuvarande namn: {current}."
    )
    return CatalogTurn(
        consumed=True,
        results=[
            ("show_product", render_product(product)),
            ("propose_product_update", propose),
        ],
        pending=pending,
        sticky_product_id=pid,
    )


def _propose_page(write: CatalogWrite, *, domain: str | None) -> CatalogTurn:
    from ecom_ops.integrations.wordpress import wp_client_from_env

    try:
        wp = wp_client_from_env(use_mock=None, domain=domain)
    except Exception as exc:
        return CatalogTurn(
            consumed=True,
            results=[("catalog", f"Sidan svarade inte: {_public_api_failure(exc)}")],
        )
    page, error = _resolve_page(wp, write)
    if error or page is None:
        return CatalogTurn(consumed=True, results=[("catalog", error or "Sidan saknas.")])
    pid = str(page.id)
    pending = PendingAction(
        kind="page_update",
        payload={
            "page_id": pid,
            "fields": dict(write.fields),
            "label": write.label,
            "preview": write.preview,
            "domain": domain,
        },
    )
    propose = (
        f"FÖRESLÅ ändring (ej utförd): sida {pid} ({_plain_title(page.title)}) "
        f"{write.label} → {write.preview}."
    )
    return CatalogTurn(
        consumed=True,
        results=[
            ("show_page", render_page(page)),
            ("propose_page_update", propose),
        ],
        pending=pending,
        sticky_page_id=pid,
    )


def _read_product(read: CatalogRead, *, domain: str | None) -> CatalogTurn:
    from ecom_ops.integrations.woocommerce import client_from_env

    try:
        woo = client_from_env(use_mock=None, domain=domain)
        host = urlparse(woo.base_url).hostname or woo.base_url
        if read.kind == "show_product":
            if not read.target_id:
                return CatalogTurn(
                    consumed=True,
                    results=[("catalog", "Vilken produkt? Säg produktnumret.")],
                )
            product = woo.get_product(read.target_id)
            variations = _safe_variations(woo, read.target_id, product)
            return CatalogTurn(
                consumed=True,
                results=[("show_product", render_product(product, variations=variations))],
                sticky_product_id=str(product.get("id") or read.target_id),
            )
        rows = woo.list_products(
            per_page=20,
            page=1,
            search=read.query,
            status=read.status,
        )
    except Exception as exc:
        return CatalogTurn(
            consumed=True,
            results=[("catalog", f"Produktlistan svarade inte: {_public_api_failure(exc)}")],
        )
    sticky = str(rows[0].get("id")) if len(rows) == 1 else None
    return CatalogTurn(
        consumed=True,
        results=[("list_products", render_product_list(rows, host=host, query=read.query))],
        sticky_product_id=sticky,
    )


def _read_page(read: CatalogRead, *, domain: str | None) -> CatalogTurn:
    from ecom_ops.integrations.wordpress import wp_client_from_env

    try:
        wp = wp_client_from_env(use_mock=None, domain=domain)
        host = urlparse(wp.base_url).hostname or wp.base_url
        if read.target_id:
            page = wp.get_page(read.target_id)
            return CatalogTurn(
                consumed=True,
                results=[("show_page", render_page(page))],
                sticky_page_id=str(page.id),
            )
        if read.query:
            rows = wp.list_pages(per_page=10, search=read.query)
            if len(rows) == 1:
                return CatalogTurn(
                    consumed=True,
                    results=[("show_page", render_page(rows[0]))],
                    sticky_page_id=str(rows[0].id),
                )
            if not rows:
                return CatalogTurn(
                    consumed=True,
                    results=[("catalog", f"Ingen sida matchar «{read.query}».")],
                )
            return CatalogTurn(
                consumed=True,
                results=[("list_pages", render_page_list(rows, host=host, query=read.query))],
            )
        if read.kind == "show_page":
            return CatalogTurn(
                consumed=True,
                results=[("catalog", "Vilken sida? Säg sidnummer eller titel.")],
            )
        rows = wp.list_pages(per_page=20)
    except Exception as exc:
        return CatalogTurn(
            consumed=True,
            results=[("catalog", f"Sidlistan svarade inte: {_public_api_failure(exc)}")],
        )
    sticky = str(rows[0].id) if len(rows) == 1 else None
    return CatalogTurn(
        consumed=True,
        results=[("list_pages", render_page_list(rows, host=host))],
        sticky_page_id=sticky,
    )


def render_product_list(
    rows: list[dict[str, Any]], *, host: str, query: str | None = None
) -> str:
    if not rows:
        if query:
            return f"Inga produkter matchar «{query}» på {host}."
        return f"Woo {host} svarade med en tom produktlista."
    head = f"Produkter på {host}"
    if query:
        head += f" som matchar «{query}»"
    lines = [f"{head} ({len(rows)}):"]
    for row in rows[:20]:
        lines.append(_product_line(row))
    lines.append("Säg ett produktnummer för mer data, eller säg vilket fält som ska ändras.")
    return "\n".join(lines)


def render_product(
    product: dict[str, Any], *, variations: list[dict[str, Any]] | None = None
) -> str:
    name = _plain_title(product.get("name"))
    pid = product.get("id")
    lines = [f"Produkt {pid} | {name}"]
    lines.append(
        " | ".join(
            [
                f"sku {_or_dash(product.get('sku'))}",
                f"typ {_or_dash(product.get('type'))}",
                f"status {_or_dash(product.get('status'))}",
                f"pris {_or_dash(product.get('price') or product.get('regular_price'))}",
                f"lager {_or_dash(product.get('stock_quantity'))} "
                f"{product.get('stock_status') or ''}".rstrip(),
            ]
        )
    )
    excerpt = _excerpt(product.get("short_description") or product.get("description"))
    if excerpt:
        lines.append(excerpt)
    if variations:
        lines.append(f"Varianter ({len(variations)}):")
        for row in variations[:8]:
            lines.append(_product_line(row))
    lines.append(
        "Säg till exempel «ändra namn till …», «sätt pris till …» eller "
        "«sätt lager till …». Inget sparas förrän du trycker Ja."
    )
    return "\n".join(lines)


def render_page_list(rows: list[Any], *, host: str, query: str | None = None) -> str:
    if not rows:
        if query:
            return f"Inga sidor matchar «{query}» på {host}."
        return f"WordPress {host} svarade med en tom sidlista."
    head = f"Sidor på {host}"
    if query:
        head += f" som matchar «{query}»"
    lines = [f"{head} ({len(rows)}):"]
    for row in rows[:20]:
        lines.append(
            f"- {row.id} | {_plain_title(row.title)} | {row.status} | {row.link or ''}".rstrip()
        )
    lines.append("Säg ett sidnummer, eller «ändra sidan <titel> text till …».")
    return "\n".join(lines)


def render_page(page: Any) -> str:
    raw = page.raw if isinstance(page.raw, dict) else {}
    excerpt = _excerpt(raw.get("content") or raw.get("excerpt"))
    lines = [f"Sida {page.id} | {_plain_title(page.title)} | {page.status}"]
    if page.link:
        lines.append(str(page.link))
    if excerpt:
        lines.append(excerpt)
    lines.append(
        "Säg «ändra titel till …» eller «ändra text till …». "
        "Inget sparas förrän du trycker Ja."
    )
    return "\n".join(lines)


def _product_line(row: dict[str, Any]) -> str:
    return (
        f"- {row.get('id')} | {_plain_title(row.get('name') or row.get('sku') or '')} | "
        f"sku {_or_dash(row.get('sku'))} | "
        f"pris {_or_dash(row.get('price') or row.get('regular_price'))} | "
        f"lager {_or_dash(row.get('stock_quantity'))} | "
        f"{row.get('stock_status') or row.get('status') or ''}"
    ).rstrip()


def _resolve_product(woo: Any, write: CatalogWrite) -> tuple[dict[str, Any] | None, str | None]:
    if write.target_id:
        try:
            return woo.get_product(write.target_id), None
        except Exception as exc:
            return None, f"Produkten svarade inte: {_public_api_failure(exc)}"
    try:
        rows = woo.list_products(search=write.query, per_page=10, status="any")
    except Exception as exc:
        return None, f"Produktlistan svarade inte: {_public_api_failure(exc)}"
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"Ingen produkt matchar «{write.query}»."
    lines = [f"Flera produkter matchar «{write.query}». Säg produktnumret:"]
    for row in rows[:8]:
        lines.append(f"- {row.get('id')} | {_plain_title(row.get('name'))}")
    return None, "\n".join(lines)


def _resolve_page(wp: Any, write: CatalogWrite) -> tuple[Any, str | None]:
    if write.target_id:
        try:
            return wp.get_page(write.target_id), None
        except Exception as exc:
            return None, f"Sidan svarade inte: {_public_api_failure(exc)}"
    try:
        rows = wp.list_pages(per_page=10, search=write.query)
    except Exception as exc:
        return None, f"Sidlistan svarade inte: {_public_api_failure(exc)}"
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"Ingen sida matchar «{write.query}»."
    lines = [f"Flera sidor matchar «{write.query}». Säg sidnumret:"]
    for row in rows[:8]:
        lines.append(f"- {row.id} | {_plain_title(row.title)}")
    return None, "\n".join(lines)


def _safe_variations(woo: Any, product_id: str, product: dict[str, Any]) -> list[dict[str, Any]]:
    if str(product.get("type") or "") != "variable":
        return []
    try:
        rows = woo.list_product_variations(product_id)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _write_kind(
    text: str,
    token: str,
    sticky_product_id: str | None,
    sticky_page_id: str | None,
) -> str | None:
    page_noun = bool(_PAGE_NOUN_RE.search(text))
    product_noun = bool(_PRODUCT_NOUN_RE.search(text))
    product_fields = {
        "pris",
        "priset",
        "price",
        "lager",
        "lagersaldo",
        "lagret",
        "stock",
        "lagerstatus",
        "kort beskrivning",
        "short description",
        "produkttext",
        "beskrivning",
        "beskrivningen",
        "namn",
        "name",
    }
    if token in product_fields and not (page_noun and not product_noun):
        return "product_update"
    if page_noun and not product_noun:
        return "page_update"
    if product_noun and not page_noun:
        return "product_update"
    if token in {"titel", "title", "text", "texten", "content", "innehåll", "innehall"}:
        if sticky_page_id and not sticky_product_id:
            return "page_update"
        if sticky_product_id:
            return "product_update"
        return "page_update" if token in {"titel", "title"} else None
    if token == "status":
        return "page_update" if page_noun or sticky_page_id else "product_update"
    return None


def _write_target(text: str, kind: str) -> tuple[str | None, str | None]:
    if kind == "product_update":
        id_match = _PRODUCT_ID_RE.search(text)
        if id_match:
            return id_match.group(1), None
        query_match = _PRODUCT_QUERY_RE.search(text)
        if query_match:
            query = query_match.group(1).strip(" #")
            if query and not re.fullmatch(r"\d{1,12}", query):
                return None, query[:60]
        return None, None
    id_match = _PAGE_ID_RE.search(text)
    if id_match:
        return id_match.group(1), None
    query_match = _PAGE_QUERY_RE.search(text)
    if query_match:
        query = query_match.group(1).strip(" #")
        if query and not re.fullmatch(r"\d{1,12}", query):
            return None, query[:60]
    return None, None


def _coerce_field(kind: str, token: str, value: str) -> tuple[dict[str, Any], str, str]:
    key = _storage_key(kind, token)
    if kind == "page_update" and key in {"regular_price", "stock_quantity", "stock_status"}:
        raise SecurityError("Pris och lager ändras på produkten.")
    if key == "regular_price":
        price = _parse_price(value)
        return {"regular_price": price}, "pris", price
    if key == "stock_quantity":
        mapped = _STOCK_STATUS.get(value.lower())
        if mapped:
            return {"stock_status": mapped}, "lagerstatus", _STOCK_LABEL[mapped]
        if not re.fullmatch(r"\d{1,6}", value):
            raise SecurityError("Ogiltigt lager")
        return {"stock_quantity": int(value)}, "lager", value
    if key == "stock_status":
        mapped = _STOCK_STATUS.get(value.lower())
        if not mapped:
            raise SecurityError("Ogiltig lagerstatus")
        return {"stock_status": mapped}, "lagerstatus", _STOCK_LABEL[mapped]
    if key == "status":
        mapped = _PAGE_STATUS.get(value.lower())
        if not mapped:
            raise SecurityError("Ogiltig status")
        return {"status": mapped}, "status", mapped
    if key == "name":
        cleaned = sanitize_text(value, max_len=200)
        return {"name": cleaned}, "namn", _preview(cleaned)
    if key == "title":
        cleaned = sanitize_text(value, max_len=300)
        return {"title": cleaned}, "titel", _preview(cleaned)
    if key == "short_description":
        cleaned = sanitize_text(value, max_len=1000)
        return {"short_description": cleaned}, "kort beskrivning", _preview(cleaned)
    if key == "content":
        cleaned = sanitize_text(value, max_len=8000)
        return {"content": cleaned}, "text", _preview(cleaned)
    cleaned = sanitize_text(value, max_len=8000)
    return {"description": cleaned}, "beskrivning", _preview(cleaned)


def _storage_key(kind: str, token: str) -> str:
    if token in {"pris", "priset", "price"}:
        return "regular_price"
    if token in {"lager", "lagersaldo", "lagret", "stock"}:
        return "stock_quantity"
    if token == "lagerstatus":
        return "stock_status"
    if token in {"kort beskrivning", "short description"}:
        return "short_description"
    if token in {"titel", "title"}:
        return "title" if kind == "page_update" else "name"
    if token in {"text", "texten", "content", "innehåll", "innehall", "innehållet", "innehallet"}:
        return "content" if kind == "page_update" else "description"
    if token in {"beskrivning", "beskrivningen", "produkttext"}:
        return "content" if kind == "page_update" else "description"
    if token == "status":
        return "status"
    return "name"


def _revalidate_product_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if key == "regular_price":
            clean["regular_price"] = _parse_price(str(value))
        elif key == "stock_quantity":
            if not re.fullmatch(r"\d{1,6}", str(value)):
                raise SecurityError("Ogiltigt lager")
            clean["stock_quantity"] = int(value)
        elif key == "stock_status":
            if str(value) not in _STOCK_LABEL:
                raise SecurityError("Ogiltig lagerstatus")
            clean["stock_status"] = str(value)
        elif key == "status":
            if str(value) not in {"draft", "pending", "private", "publish"}:
                raise SecurityError("Ogiltig status")
            clean["status"] = str(value)
        elif key == "name":
            clean["name"] = sanitize_text(str(value), max_len=200)
        elif key == "short_description":
            clean["short_description"] = sanitize_text(str(value), max_len=1000)
        elif key == "description":
            clean["description"] = sanitize_text(str(value), max_len=8000)
    if not clean:
        raise SecurityError("No allowed product fields")
    return clean


def _revalidate_page_fields(fields: dict[str, Any]) -> dict[str, Any]:
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
    if not clean:
        raise SecurityError("No page fields to update")
    return clean


def _parse_price(value: str) -> str:
    raw = value.strip().lower()
    raw = re.sub(r"\b(?:kr|nok|sek|dkk|eur|€)\b", "", raw)
    raw = raw.replace(" ", "").replace("\u00a0", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    if not re.fullmatch(r"\d{1,8}(\.\d{1,2})?", raw):
        raise SecurityError("Ogiltigt pris")
    return raw


def _product_search(text: str) -> str | None:
    match = _PRODUCT_SEARCH_RE.search(text)
    if not match:
        return None
    query = _strip_value(match.group(1) or match.group(2) or "")
    if not query or re.fullmatch(r"#?\d{1,12}", query):
        return None
    return query[:60]


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


def _plain_title(value: Any) -> str:
    return html.unescape(str(value or "")).strip()


def _or_dash(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _coerce_error(exc: SecurityError) -> str:
    message = str(exc)
    if "pris" in message.lower() or "price" in message.lower():
        return "Priset behöver vara ett tal, till exempel 199 eller 1 299,00."
    if "lager" in message.lower() or "stock" in message.lower():
        return "Lagret behöver vara ett antal eller i lager / slut / restorder."
    if "status" in message.lower():
        return "Status kan vara utkast, publicerad, privat eller väntande."
    if "Pris och lager" in message:
        return message
    if "max length" in message.lower():
        return "Texten är för lång för att sparas i chatten."
    return "Värdet kunde inte tolkas."


def _public_api_failure(exc: Exception) -> str:
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
    public = _public_api_failure(exc)
    if public.startswith("HTTP") or public == "nätverksfel":
        return f"Skrivningen svarade inte: {public}"
    return "Värdet kunde inte sparas."
