"""WordPress REST API client (/wp-json/wp/v2/) using Application Passwords.

V2.1 (see docs/solutions/2026-07-17-woo-wordpress-capacity-review.md §P1.3):
- Read + write for posts, pages, media, users, comments, settings.
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
from dataclasses import dataclass
from typing import Any, Protocol

from ecom_ops.security import SecurityError, get_env, sanitize_text

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


class WpHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any: ...


class InMemoryWpTransport:
    """Test double for WordPress REST without network."""

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

        store = self._store_for(collection)
        if store is None:
            raise SecurityError(f"Unhandled WP collection: {collection}")

        # Collection-level
        if len(parts) == 1:
            if method.upper() == "GET":
                rows = list(store.values())
                # Filter by type for posts/pages collections
                if collection == "posts":
                    rows = [r for r in rows if r.get("type") == "post"]
                elif collection == "pages":
                    rows = [r for r in rows if r.get("type") == "page"]
                params = params or {}
                search = str(params.get("search") or "").lower()
                if search:
                    rows = [
                        r
                        for r in rows
                        if search in str(r.get("title", {}).get("rendered", "")).lower()
                        or search in str(r.get("name", "")).lower()
                    ]
                per_page = int(params.get("per_page") or 10)
                page = int(params.get("page") or 1)
                start = (page - 1) * per_page
                return rows[start : start + per_page]
            if method.upper() == "POST":
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
                store[row["id"]] = row
                return row
            raise SecurityError(f"Unhandled WP collection op: {method}")

        # Item-level
        item_id = parts[1]
        try:
            iid = int(item_id)
        except ValueError:
            raise SecurityError(f"WP API error 404: invalid id {item_id}")
        if method.upper() == "GET":
            if iid not in store:
                raise SecurityError(f"WP API error 404: {collection}/{item_id}")
            return store[iid]
        if method.upper() in {"POST", "PUT", "PATCH"}:
            if iid not in store:
                raise SecurityError(f"WP API error 404: {collection}/{item_id}")
            store[iid] = {**store[iid], **(json or {})}
            if "title" in json and not isinstance(store[iid]["title"], dict):
                store[iid]["title"] = {"rendered": str(json["title"])}
            return store[iid]
        if method.upper() == "DELETE":
            removed = store.pop(iid, None)
            if removed:
                return {"deleted": True, "id": iid}
            raise SecurityError(f"WP API error 404: {collection}/{item_id}")
        raise SecurityError(f"Unhandled WP item op: {method}")

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


# Reuse the robust RequestsTransport from woocommerce.py for live HTTP
def _make_live_transport(timeout: float):
    from ecom_ops.integrations.woocommerce import RequestsTransport

    return RequestsTransport(default_timeout=timeout)


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
        transport: WpHttpTransport | None = None,
        timeout: float = 30.0,
        domain: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.domain = domain
        self.username = username or get_env("WP_USERNAME", "") or get_env("WORDPRESS_USERNAME", "")
        self.app_password = app_password or get_env("WP_APP_PASSWORD", "")
        self.transport = transport or _make_live_transport(timeout)
        self.timeout = float(timeout)
        if not isinstance(self.transport, InMemoryWpTransport):
            if not self.username or not self.app_password:
                raise SecurityError(
                    "WP_USERNAME and WP_APP_PASSWORD are required for WordPress REST"
                )

    def _auth(self) -> tuple[str, str] | None:
        if isinstance(self.transport, InMemoryWpTransport):
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

    def get_post(self, post_id: str | int) -> WpPost:
        pid = _validate_int_id(post_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wp/v2/posts/{pid}"),
            auth=self._auth(),
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

    def update_post(
        self,
        post_id: str | int,
        *,
        title: str | None = None,
        content: str | None = None,
        status: str | None = None,
    ) -> WpPost:
        pid = _validate_int_id(post_id)
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = sanitize_text(title, max_len=300)
        if content is not None:
            payload["content"] = content
        if status is not None:
            payload["status"] = status
        data = self.transport.request(
            "POST",
            self._url(f"/wp-json/wp/v2/posts/{pid}"),
            auth=self._auth(),
            json=payload,
            timeout=self.timeout,
        )
        return self._to_post(data)

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
        self, *, per_page: int = 10, page: int = 1, search: str | None = None
    ) -> list[WpPost]:
        params: dict[str, Any] = {
            "per_page": max(1, min(int(per_page), 100)),
            "page": max(1, int(page)),
        }
        if search:
            params["search"] = search
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wp/v2/pages"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return self._to_posts(data) if isinstance(data, list) else []

    def get_page(self, page_id: str | int) -> WpPost:
        pid = _validate_int_id(page_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wp/v2/pages/{pid}"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return self._to_post(data)

    # --- media ------------------------------------------------------------ #

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
            title = title.get("rendered") or ""
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
            from ecom_ops.config import woo_base_url_for_domain

            base_url = woo_base_url_for_domain(domain)
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
