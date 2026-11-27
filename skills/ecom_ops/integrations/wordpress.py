"""WordPress REST API client (/wp-json/wp/v2/) using Application Passwords.

V2.1 (see docs/WOO_WORDPRESS.md — WordPress client):
- Read + write for posts, pages, media, users, comments, settings.
- Themes are list-only. Plugins are list plus active/inactive. No install,
  update, delete, or theme switch.
- Auth: HTTP Basic with WordPress username + Application Password
  (``WP_APP_PASSWORD``). Application Passwords are supported by WordPress
  core 5.6+ and are exposed on azom.no via ``/wp-admin/authorize-application.php``.
- Reuses the same transport abstraction as WooCommerceClient
  (``RequestsTransport`` with session reuse + retry/backoff, or
  ``InMemoryWpTransport`` for tests).
- Multi-site: ``client_from_env(domain=...)`` resolves base URL via
  ``woo_base_url_for_domain`` (shared convention with Woo).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from ecom_ops.integrations._http_transport import HttpTransport, RequestsTransport
from ecom_ops.security import SecurityError, get_env, sanitize_text

_PAGE_STATUSES = frozenset({"draft", "publish", "private", "pending"})
_PLUGIN_STATUSES = frozenset({"active", "inactive"})
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?(?:\.php)?$")

# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WpPost:
    id: int
    type: str  # "post" | "page" | custom
    title: str
    status: str
    link: str | None = None
    raw: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


WpHttpTransport = HttpTransport


class InMemoryWpTransport:
    """Test double for WordPress REST without network."""

    requires_auth: bool = False

    def __init__(self) -> None:
        self.posts: dict[int, dict[str, Any]] = {
            1: {
                "id": 1,
                "type": "post",
                "title": {"rendered": "Hej från Azom"},
                "status": "publish",
                "link": "https://azom.no/hej-azom",
                "content": {"rendered": "<p>Välkommen</p>"},
                "author": 1,
            },
            2: {
                "id": 2,
                "type": "page",
                "title": {"rendered": "Om oss"},
                "status": "publish",
                "link": "https://azom.no/om-oss",
                "content": {"rendered": "<p>Azom AB</p>"},
                "author": 1,
            },
        }
        self.media: dict[int, dict[str, Any]] = {
            10: {
                "id": 10,
                "source_url": "https://azom.no/wp-content/uploads/logo.png",
                "mime_type": "image/png",
                "title": {"rendered": "logo"},
                "alt_text": "",
                "caption": {"rendered": ""},
                "description": {"rendered": ""},
                "post": 2,
                "media_details": {
                    "width": 512,
                    "height": 128,
                    "filesize": 24576,
                    "file": "2026/07/logo.png",
                },
            }
        }
        self.users: dict[int, dict[str, Any]] = {
            1: {"id": 1, "name": "admin", "slug": "admin"},
        }
        self.comments: dict[int, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {
            "title": "Azom",
            "description": "Nordisk e-handel",
            "language": "nb",
        }
        self.themes: dict[str, dict[str, Any]] = {
            "twentytwentyfive": {
                "stylesheet": "twentytwentyfive",
                "name": "Twenty Twenty-Five",
                "status": "active",
                "version": "1.0",
            },
            "twentytwentyfour": {
                "stylesheet": "twentytwentyfour",
                "name": "Twenty Twenty-Four",
                "status": "inactive",
                "version": "1.3",
            },
        }
        self.templates: list[dict[str, Any]] = []
        self.plugins: dict[str, dict[str, Any]] = {
            "woocommerce/woocommerce": {
                "plugin": "woocommerce/woocommerce",
                "name": "WooCommerce",
                "status": "active",
                "version": "9.0",
            },
            "akismet/akismet": {
                "plugin": "akismet/akismet",
                "name": "Akismet",
                "status": "inactive",
                "version": "5.3",
            },
        }
        self.calls: list[tuple[str, str, dict | None]] = []
        self._next_id = 100

    def request(
        self,
        method: str,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        self.calls.append((method, url, json))
        # Handle /wp-json/ root discovery (no /wp/v2/ in URL)
        if "/wp-json/wp/v2" not in url:
            if url.rstrip("/").endswith("/wp-json") or url.rstrip("/").endswith("/wp-json/"):
                return {
                    "name": "Azom Mock",
                    "namespaces": ["wp/v2", "wc/v3", "wc/store"],
                    "authentication": {"application-passwords": {"endpoints": {}}},
                }
            raise SecurityError(f"Unhandled WP mock URL: {method} {url}")
        path = url.split("/wp-json/wp/v2", 1)[-1].split("?")[0].strip("/")
        parts = [p for p in path.split("/") if p]

        # /posts, /pages, /media, /users, /comments, /settings
        if not parts:
            raise SecurityError(f"Unhandled WP mock URL: {method} {url}")

        collection = parts[0]
        # Settings is a singleton
        if collection == "settings":
            if method.upper() == "GET":
                return self.settings
            if method.upper() in {"POST", "PUT", "PATCH"}:
                self.settings.update(json or {})
                return self.settings
            raise SecurityError(f"Unhandled WP settings: {method}")

        if collection == "themes":
            return self._themes(method, parts)
        if collection == "plugins":
            return self._plugins(method, parts, json)
        if collection == "templates":
            if method.upper() == "GET":
                return [dict(row) for row in self.templates]
            raise SecurityError(f"Unhandled WP templates: {method}")

        store = self._store_for(collection)
        if store is None:
            raise SecurityError(f"Unhandled WP collection: {collection}")

        # Collection-level
        if len(parts) == 1:
            return self._handle_collection(method.upper(), collection, store, json=json, params=params)

        # Item-level
        if len(parts) == 3 and parts[2] == "revisions" and collection in {"posts", "pages"}:
            return self._handle_revisions(
                method.upper(), collection, store, parts[1], params=params
            )
        return self._handle_item(method.upper(), collection, store, parts[1], json=json, params=params)

    def _handle_revisions(
        self,
        verb: str,
        collection: str,
        store: dict[int, dict[str, Any]],
        item_id: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if verb != "GET":
            raise SecurityError(f"Unhandled WP revisions op: {verb}")
        iid = int(item_id)
        if iid not in store:
            raise SecurityError(f"WP API error 404: {collection}/{item_id}")
        row = store[iid]
        rows = [
            {
                "id": iid * 100 + 1,
                "parent": iid,
                "author": row.get("author", 1),
                "modified_gmt": row.get("modified_gmt", "2026-11-27T00:00:00"),
                "title": row.get("title", {"rendered": ""}),
                "content": row.get("content", {"rendered": ""}),
            }
        ]
        query = params or {}
        page = max(1, int(query.get("page") or 1))
        per_page = max(1, int(query.get("per_page") or 20))
        start = (page - 1) * per_page
        return rows[start : start + per_page]

    def _handle_collection(
        self,
        verb: str,
        collection: str,
        store: dict[int, dict[str, Any]],
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if verb == "GET":
            rows = list(store.values())
            if collection == "posts":
                rows = [r for r in rows if r.get("type") == "post"]
            elif collection == "pages":
                rows = [r for r in rows if r.get("type") == "page"]
            query = params or {}
            search = str(query.get("search") or "").lower()
            if search:
                rows = [
                    r
                    for r in rows
                    if search in str(r.get("title", {}).get("rendered", "")).lower()
                    or search in str(r.get("name", "")).lower()
                ]
            per_page = int(query.get("per_page") or 10)
            page = int(query.get("page") or 1)
            start = (page - 1) * per_page
            return rows[start : start + per_page]
        if verb == "POST":
            row = dict(json or {})
            row["id"] = self._next_id
            self._next_id += 1
            if collection == "posts":
                row.setdefault("type", "post")
            elif collection == "pages":
                row.setdefault("type", "page")
            else:
                row.setdefault("type", collection.rstrip("s"))
            row.setdefault("status", "draft")
            if "title" in row and not isinstance(row["title"], dict):
                row["title"] = {"rendered": str(row["title"])}
            elif "title" not in row:
                row["title"] = {"rendered": ""}
            if collection == "pages":
                slug_val = str(row.get("slug") or "").strip().lower()
                if not slug_val:
                    slug_val = f"page-{row['id']}"
                row["slug"] = slug_val
                row.setdefault("link", f"https://mock.local/{slug_val}/")
            store[row["id"]] = row
            return row
        raise SecurityError(f"Unhandled WP collection op: {verb}")

    def _handle_item(
        self,
        verb: str,
        collection: str,
        store: dict[int, dict[str, Any]],
        item_id: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            iid = int(item_id)
        except ValueError:
            raise SecurityError(f"WP API error 404: invalid id {item_id}")
        if verb == "GET":
            if iid not in store:
                raise SecurityError(f"WP API error 404: {collection}/{item_id}")
            row = store[iid]
            content = row.get("content") if isinstance(row, dict) else None
            editing = str((params or {}).get("context") or "") == "edit"
            if isinstance(content, dict) and "raw" in content and not editing:
                hidden = dict(content)
                hidden.pop("raw", None)
                return {**row, "content": hidden}
            return row
        if verb in {"POST", "PUT", "PATCH"}:
            if iid not in store:
                raise SecurityError(f"WP API error 404: {collection}/{item_id}")
            store[iid] = {**store[iid], **(json or {})}
            if json and "title" in json and not isinstance(store[iid]["title"], dict):
                store[iid]["title"] = {"rendered": str(json["title"])}
            if collection == "media" and json:
                for rich_field in ("caption", "description"):
                    if rich_field in json and not isinstance(store[iid][rich_field], dict):
                        store[iid][rich_field] = {"rendered": str(json[rich_field])}
            if collection in {"posts", "pages"} and json and "excerpt" in json and not isinstance(store[iid]["excerpt"], dict):
                store[iid]["excerpt"] = {"rendered": str(json["excerpt"])}
            return store[iid]
        if verb == "DELETE":
            removed = store.pop(iid, None)
            if removed:
                return {"deleted": True, "id": iid}
            raise SecurityError(f"WP API error 404: {collection}/{item_id}")
        raise SecurityError(f"Unhandled WP item op: {verb}")

    def _themes(self, method: str, parts: list[str]) -> Any:
        if method.upper() != "GET" or len(parts) != 1:
            raise SecurityError(f"Unhandled WP themes: {method}")
        return [dict(row) for row in self.themes.values()]

    def _plugins(self, method: str, parts: list[str], body: dict[str, Any] | None) -> Any:
        verb = method.upper()
        if len(parts) == 1:
            if verb == "GET":
                return [dict(row) for row in self.plugins.values()]
            raise SecurityError(f"Unhandled WP plugins: {verb}")
        plugin_id = unquote("/".join(parts[1:]))
        row = self.plugins.get(plugin_id)
        if row is None:
            raise SecurityError(f"WP API error 404: plugins/{plugin_id}")
        if verb == "GET":
            return dict(row)
        if verb in {"POST", "PUT", "PATCH"}:
            status = str((body or {}).get("status") or "").strip().lower()
            if status not in _PLUGIN_STATUSES:
                raise SecurityError("Invalid plugin status")
            if set(body or {}) - {"status"}:
                raise SecurityError("Unhandled WP plugin field")
            updated = {**row, "status": status}
            self.plugins[plugin_id] = updated
            return dict(updated)
        raise SecurityError(f"Unhandled WP plugins: {verb}")

    def _store_for(self, collection: str) -> dict[int, dict[str, Any]] | None:
        # posts and pages share self.posts (distinguished by "type" field)
        if collection in ("posts", "pages"):
            return self.posts
        if collection == "media":
            return self.media
        if collection == "users":
            return self.users
        if collection == "comments":
            return self.comments
        return None


def _make_live_transport(timeout: float) -> RequestsTransport:
    return RequestsTransport(default_timeout=timeout, service_name="WordPress")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class WordPressClient:
    """WordPress REST API client (/wp-json/wp/v2/).

    Auth: HTTP Basic with WordPress username + Application Password.
    Application Passwords are created via ``/wp-admin/authorize-application.php``
    on the WordPress site (core 5.6+).
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str | None = None,
        app_password: str | None = None,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
        domain: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.domain = domain
        dom_tag = str(domain or "").strip().lower().lstrip(".").rsplit(".", 1)[-1].upper()
        dom_user = (
            (get_env(f"WP_USERNAME_{dom_tag}", "") or get_env(f"WORDPRESS_USERNAME_{dom_tag}", ""))
            if dom_tag
            else ""
        )
        dom_pass = (
            (
                get_env(f"WP_APP_PASSWORD_{dom_tag}", "")
                or get_env(f"WORDPRESS_APP_PASSWORD_{dom_tag}", "")
            )
            if dom_tag
            else ""
        )
        self.username = (
            username or dom_user or get_env("WP_USERNAME", "") or get_env("WORDPRESS_USERNAME", "")
        )
        self.app_password = (
            app_password
            or dom_pass
            or get_env("WP_APP_PASSWORD", "")
            or get_env("WORDPRESS_APP_PASSWORD", "")
        )
        self.transport = transport or _make_live_transport(timeout)
        self.timeout = float(timeout)
        if getattr(self.transport, "requires_auth", True):
            if not self.username or not self.app_password:
                raise SecurityError(
                    "WP_USERNAME and WP_APP_PASSWORD are required for WordPress REST"
                )

    def _auth(self) -> tuple[str, str] | None:
        if not getattr(self.transport, "requires_auth", True):
            return None
        return (self.username, self.app_password)

    def _url(self, path: str) -> str:
        from urllib.parse import urljoin

        return urljoin(self.base_url + "/", path.lstrip("/"))

    # --- posts ----------------------------------------------------------- #

    def list_posts(
        self,
        *,
        per_page: int = 10,
        page: int = 1,
        search: str | None = None,
        status: str = "publish",
    ) -> list[WpPost]:
        params: dict[str, Any] = {
            "per_page": max(1, min(int(per_page), 100)),
            "page": max(1, int(page)),
            "status": status,
        }
        if search:
            params["search"] = search
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wp/v2/posts"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return self._to_posts(data) if isinstance(data, list) else []

    def get_post(self, post_id: str | int, *, context: str | None = None) -> WpPost:
        pid = _validate_int_id(post_id)
        params = {"context": "edit"} if context == "edit" else None
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wp/v2/posts/{pid}"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return self._to_post(data)

    def create_post(
        self,
        *,
        title: str,
        content: str = "",
        status: str = "draft",
        excerpt: str | None = None,
    ) -> WpPost:
        payload: dict[str, Any] = {
            "title": sanitize_text(title, max_len=300),
            "content": content,
            "status": status,
        }
        if excerpt:
            payload["excerpt"] = sanitize_text(excerpt, max_len=600)
        data = self.transport.request(
            "POST",
            self._url("/wp-json/wp/v2/posts"),
            auth=self._auth(),
            json=payload,
            timeout=self.timeout,
        )
        return self._to_post(data)

    def _update_entry(
        self,
        collection: str,
        label: str,
        entry_id: str | int,
        *,
        title: str | None = None,
        content: str | None = None,
        status: str | None = None,
        excerpt: str | None = None,
        slug: str | None = None,
        allow_empty: bool = False,
        max_content_len: int = 8000,
    ) -> WpPost:
        eid = _validate_int_id(entry_id)
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = sanitize_text(title, max_len=300, allow_empty=allow_empty)
        if content is not None:
            payload["content"] = sanitize_text(
                content, max_len=max_content_len, allow_empty=allow_empty
            )
        if excerpt is not None:
            payload["excerpt"] = sanitize_text(excerpt, max_len=600, allow_empty=allow_empty)
        if slug is not None:
            clean_slug = re.sub(r"[^a-z0-9-]+", "-", str(slug).strip().lower()).strip("-")[:120]
            if not clean_slug and not allow_empty:
                raise SecurityError(f"Invalid {label} slug: {slug}")
            payload["slug"] = clean_slug
        if status is not None:
            normalized = str(status).strip().lower()
            if normalized not in _PAGE_STATUSES:
                raise SecurityError(f"Invalid {label} status: {status}")
            payload["status"] = normalized
        if not payload:
            raise SecurityError(f"No {label} fields to update")
        data = self.transport.request(
            "POST",
            self._url(f"/wp-json/wp/v2/{collection}/{eid}"),
            auth=self._auth(),
            json=payload,
            timeout=self.timeout,
        )
        return self._to_post(data)

    def update_post(
        self,
        post_id: str | int,
        *,
        title: str | None = None,
        content: str | None = None,
        status: str | None = None,
        excerpt: str | None = None,
        slug: str | None = None,
        allow_empty: bool = False,
        max_content_len: int = 8000,
    ) -> WpPost:
        return self._update_entry(
            "posts",
            "post",
            post_id,
            title=title,
            content=content,
            status=status,
            excerpt=excerpt,
            slug=slug,
            allow_empty=allow_empty,
            max_content_len=max_content_len,
        )

    def delete_post(self, post_id: str | int) -> dict[str, Any]:
        pid = _validate_int_id(post_id)
        data = self.transport.request(
            "DELETE",
            self._url(f"/wp-json/wp/v2/posts/{pid}"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    # --- pages ------------------------------------------------------------ #

    def list_pages(
        self,
        *,
        per_page: int = 10,
        page: int = 1,
        search: str | None = None,
        status: str | None = None,
    ) -> list[WpPost]:
        params: dict[str, Any] = {
            "per_page": max(1, min(int(per_page), 100)),
            "page": max(1, int(page)),
        }
        if search:
            params["search"] = search
        if status:
            params["status"] = status
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wp/v2/pages"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return self._to_posts(data) if isinstance(data, list) else []

    def list_revisions(
        self,
        collection: str,
        item_id: str | int,
        *,
        per_page: int = 20,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """List WordPress revisions for a post or page."""
        col = str(collection or "").strip().lower()
        if col not in {"posts", "pages"}:
            raise SecurityError(f"Unsupported revisions collection: {collection}")
        iid = _validate_int_id(item_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wp/v2/{col}/{iid}/revisions"),
            auth=self._auth(),
            params={
                "per_page": max(1, min(int(per_page), 100)),
                "page": max(1, int(page)),
                "context": "edit",
            },
            timeout=self.timeout,
        )
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def get_page(self, page_id: str | int, *, context: str | None = None) -> WpPost:
        pid = _validate_int_id(page_id)
        params = {"context": "edit"} if context == "edit" else None
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wp/v2/pages/{pid}"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return self._to_post(data)

    def create_page(
        self,
        *,
        title: str,
        content: str,
        slug: str = "",
        status: str = "draft",
        domain: str | None = None,
    ) -> dict[str, Any]:
        """Create a WordPress page strictly as a draft (``status='draft'`` only — never auto-published)."""
        normalized_status = str(status or "").strip().lower()
        if normalized_status != "draft":
            raise ValueError("Only status='draft' is allowed")
        clean_title = sanitize_text(str(title or ""), max_len=200).strip()
        if not clean_title:
            raise ValueError("Page title is required")
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Invalid page content")
        if len(content.encode("utf-8")) > 131_072:
            raise ValueError("Page content exceeds 128 KiB limit")
        clean_slug = re.sub(r"[^a-z0-9-]+", "-", str(slug or "").strip().lower()).strip("-")[:80]

        target_base = self.base_url
        if domain and getattr(self.transport, "requires_auth", True):
            from urllib.parse import urlparse

            from ecom_ops.config import woo_base_url_for_domain

            resolved = woo_base_url_for_domain(domain)
            if resolved:
                base_host = (urlparse(self.base_url).hostname or "").strip().lower()
                resolved_host = (urlparse(resolved).hostname or "").strip().lower()
                if base_host and resolved_host and base_host != resolved_host:
                    raise ValueError(
                        "Cross-host domain override is not permitted with existing credentials"
                    )
                target_base = resolved.rstrip("/")

        payload: dict[str, Any] = {
            "title": clean_title,
            "content": content,
            "status": "draft",
        }
        if clean_slug:
            payload["slug"] = clean_slug

        endpoint = f"{target_base.rstrip('/')}/wp-json/wp/v2/pages"
        data = self.transport.request(
            "POST",
            endpoint,
            auth=self._auth(),
            json=payload,
            timeout=self.timeout,
        )
        row = data if isinstance(data, dict) else {}
        page_id = int(row.get("id") or 0)
        raw_title = row.get("title")
        if isinstance(raw_title, dict):
            rendered_title = str(raw_title.get("rendered") or clean_title)
        else:
            rendered_title = str(raw_title or clean_title)
        res_slug = str(row.get("slug") or clean_slug or (f"page-{page_id}" if page_id else ""))
        res_link = str(row.get("link") or (f"{target_base.rstrip('/')}/?page_id={page_id}" if page_id else ""))
        edit_url = (
            f"{target_base.rstrip('/')}/wp-admin/post.php?post={page_id}&action=edit"
            if page_id > 0
            else ""
        )
        return {
            "id": page_id,
            "slug": res_slug,
            "title": rendered_title,
            "status": "draft",
            "link": res_link,
            "edit_url": edit_url,
        }

    def update_page(
        self,
        page_id: str | int,
        *,
        title: str | None = None,
        content: str | None = None,
        status: str | None = None,
        excerpt: str | None = None,
        slug: str | None = None,
        allow_empty: bool = False,
        max_content_len: int = 8000,
    ) -> WpPost:
        """Update one page over the REST API. Does not delete or create."""
        return self._update_entry(
            "pages",
            "page",
            page_id,
            title=title,
            content=content,
            status=status,
            excerpt=excerpt,
            slug=slug,
            allow_empty=allow_empty,
            max_content_len=max_content_len,
        )

    # --- media                                                            #

    def list_media(
        self, *, per_page: int = 10, page: int = 1
    ) -> list[dict[str, Any]]:
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wp/v2/media"),
            auth=self._auth(),
            params={
                "per_page": max(1, min(int(per_page), 100)),
                "page": max(1, int(page)),
            },
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    def get_media(
        self,
        media_id: str | int,
        *,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one media attachment by id."""
        mid = _validate_int_id(media_id)
        params = {"context": "edit"} if context == "edit" else None
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wp/v2/media/{mid}"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    def update_media(
        self,
        media_id: str | int,
        *,
        alt_text: str | None = None,
        title: str | None = None,
        caption: str | None = None,
        description: str | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """Update media metadata (alt_text, title, caption, description)."""
        mid = _validate_int_id(media_id)
        payload: dict[str, Any] = {}
        if alt_text is not None:
            payload["alt_text"] = sanitize_text(str(alt_text), max_len=300, allow_empty=True)
        if title is not None:
            payload["title"] = sanitize_text(str(title), max_len=300, allow_empty=allow_empty)
        if caption is not None:
            payload["caption"] = sanitize_text(str(caption), max_len=1000, allow_empty=True)
        if description is not None:
            payload["description"] = sanitize_text(str(description), max_len=4000, allow_empty=True)
        if not payload:
            raise SecurityError("No media fields to update")
        data = self.transport.request(
            "POST",
            self._url(f"/wp-json/wp/v2/media/{mid}"),
            auth=self._auth(),
            json=payload,
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    # --- users ------------------------------------------------------------ #

    def list_users(
        self, *, per_page: int = 10, page: int = 1
    ) -> list[dict[str, Any]]:
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wp/v2/users"),
            auth=self._auth(),
            params={
                "per_page": max(1, min(int(per_page), 100)),
                "page": max(1, int(page)),
            },
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    # --- comments --------------------------------------------------------- #

    def list_comments(
        self, *, per_page: int = 10, page: int = 1, post: str | int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "per_page": max(1, min(int(per_page), 100)),
            "page": max(1, int(page)),
        }
        if post is not None:
            params["post"] = post
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wp/v2/comments"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    # --- settings --------------------------------------------------------- #

    def get_settings(self) -> dict[str, Any]:
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wp/v2/settings"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    def update_settings(self, **fields: Any) -> dict[str, Any]:
        data = self.transport.request(
            "POST",
            self._url("/wp-json/wp/v2/settings"),
            auth=self._auth(),
            json=fields,
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    def update_site_identity(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update the site title or tagline. Other settings stay untouched."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = sanitize_text(title, max_len=200)
        if description is not None:
            payload["description"] = sanitize_text(description, max_len=300)
        if not payload:
            raise SecurityError("No site fields to update")
        return self.update_settings(**payload)

    # --- themes and plugins (no install, update, or delete) -------------- #

    def _list_dicts(self, path: str) -> list[dict[str, Any]]:
        data = self.transport.request(
            "GET",
            self._url(path),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def list_themes(self) -> list[dict[str, Any]]:
        return self._list_dicts("/wp-json/wp/v2/themes")

    def list_templates(self) -> list[dict[str, Any]]:
        return self._list_dicts("/wp-json/wp/v2/templates")

    def list_plugins(self) -> list[dict[str, Any]]:
        return self._list_dicts("/wp-json/wp/v2/plugins")

    def set_plugin_status(self, plugin: str, status: str) -> dict[str, Any]:
        """Turn one existing plugin on or off. Does not install, update, or delete."""
        plugin_id = _validate_plugin_id(plugin)
        normalized = str(status).strip().lower()
        if normalized not in _PLUGIN_STATUSES:
            raise SecurityError(f"Invalid plugin status: {status}")
        encoded = quote(plugin_id, safe="")
        try:
            data = self.transport.request(
                "POST",
                self._url(f"/wp-json/wp/v2/plugins/{encoded}"),
                auth=self._auth(),
                json={"status": normalized},
                timeout=self.timeout,
            )
        except SecurityError as exc:
            # Apache AllowEncodedSlashes=Off rejects %2F with 404 before WP runs.
            status_m = re.search(r"\b(?:API\s+error|HTTP|status)\s+(\d{3})\b", str(exc))
            if "/" in plugin_id and status_m and status_m.group(1) == "404":
                unencoded_path = "/".join(quote(seg, safe="") for seg in plugin_id.split("/"))
                data = self.transport.request(
                    "POST",
                    self._url(f"/wp-json/wp/v2/plugins/{unencoded_path}"),
                    auth=self._auth(),
                    json={"status": normalized},
                    timeout=self.timeout,
                )
            else:
                raise
        return data if isinstance(data, dict) else {}

    # --- discovery -------------------------------------------------------- #

    def discover_namespaces(self) -> list[str]:
        """List REST namespaces exposed by the site (GET /wp-json/)."""
        data = self.transport.request(
            "GET",
            self._url("/wp-json/"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        if isinstance(data, dict):
            ns = data.get("namespaces")
            if isinstance(ns, list):
                return [str(n) for n in ns]
        return []

    # --- converters ------------------------------------------------------- #

    @staticmethod
    def _to_post(data: dict[str, Any]) -> WpPost:
        title = data.get("title")
        if isinstance(title, dict):
            title = title.get("raw") or title.get("rendered") or ""
        return WpPost(
            id=int(data.get("id") or 0),
            type=str(data.get("type") or "post"),
            title=str(title or ""),
            status=str(data.get("status") or ""),
            link=data.get("link"),
            raw=data,
        )

    @classmethod
    def _to_posts(cls, data: list[dict[str, Any]]) -> list[WpPost]:
        return [cls._to_post(row) for row in data if isinstance(row, dict)]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _validate_plugin_id(value: str) -> str:
    plugin_id = str(value or "").strip()
    if not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise SecurityError(f"Invalid plugin id: {value}")
    return plugin_id


def _validate_int_id(value: str | int) -> int:
    try:
        iid = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SecurityError(f"Invalid WordPress id: {value}") from exc
    if iid <= 0:
        raise SecurityError(f"Invalid WordPress id: {value}")
    return iid


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def wp_client_from_env(
    *,
    base_url: str | None = None,
    use_mock: bool | None = None,
    domain: str | None = None,
    timeout: float = 30.0,
) -> WordPressClient:
    """Build a WordPressClient from env.

    Multi-site: when ``domain`` is given, base URL is resolved via
    ``woo_base_url_for_domain`` (shared convention with Woo). Falls back to
    ``WP_BASE_URL`` / ``WOO_BASE_URL`` / ``https://localhost``.
    """
    mock = use_mock
    if mock is None:
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    if mock:
        return WordPressClient(
            base_url=base_url or "https://mock.local",
            transport=InMemoryWpTransport(),
            timeout=timeout,
            domain=domain,
        )
    if base_url is None:
        if domain:
            from ecom_ops.config import _normalize_market_domain, woo_base_url_for_domain

            dom_tag = _normalize_market_domain(domain).upper()
            dom_url = get_env(f"WP_BASE_URL_{dom_tag}", "") if dom_tag else ""
            base_url = dom_url.rstrip("/") if dom_url else woo_base_url_for_domain(domain)
        else:
            base_url = (
                get_env("WP_BASE_URL", "")
                or get_env("WOO_BASE_URL", "")
                or "https://localhost"
            )
    return WordPressClient(
        base_url=base_url,
        timeout=timeout,
        domain=domain,
    )
