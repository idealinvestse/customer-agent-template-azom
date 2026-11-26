"""Tests for V2.1 WordPress REST client."""

from __future__ import annotations

import pytest

from ecom_ops.integrations.wordpress import (
    InMemoryWpTransport,
    SecurityError,
    WordPressClient,
    WpPost,
    wp_client_from_env,
)


@pytest.fixture
def wp():
    return WordPressClient(
        base_url="https://mock.local",
        transport=InMemoryWpTransport(),
    )


# --------------------------------------------------------------------------- #
# Posts
# --------------------------------------------------------------------------- #


def test_list_posts(wp):
    posts = wp.list_posts()
    assert len(posts) == 1
    assert isinstance(posts[0], WpPost)
    assert posts[0].id == 1
    assert posts[0].type == "post"
    assert posts[0].status == "publish"
    assert posts[0].link == "https://azom.no/hej-azom"


def test_get_post(wp):
    post = wp.get_post(1)
    assert post.id == 1
    assert post.type == "post"


def test_get_post_not_found(wp):
    with pytest.raises(SecurityError, match="404"):
        wp.get_post(999)


def test_create_post(wp):
    post = wp.create_post(title="New blog post", content="body text", status="draft")
    assert post.title == "New blog post"
    assert post.status == "draft"


def test_update_post(wp):
    post = wp.create_post(title="Draft", content="x", status="draft")
    updated = wp.update_post(post.id, title="Published title", status="publish")
    assert updated.title == "Published title"
    assert updated.status == "publish"


def test_delete_post(wp):
    post = wp.create_post(title="To delete", content="x")
    result = wp.delete_post(post.id)
    assert result.get("deleted") is True


def test_list_posts_search(wp):
    wp.create_post(title="SEO guide", content="x")
    results = wp.list_posts(search="SEO")
    assert any(p.title == "SEO guide" for p in results)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


def test_list_pages(wp):
    pages = wp.list_pages()
    assert len(pages) == 1
    assert pages[0].type == "page"


def test_get_page(wp):
    page = wp.get_page(2)
    assert page.id == 2
    assert page.type == "page"


def test_update_page_title_and_content(wp):
    updated = wp.update_page(2, title="Om Azom", content="<p>Hej</p>")
    assert updated.title == "Om Azom"
    assert updated.raw["content"] == "<p>Hej</p>"
    again = wp.get_page(2)
    assert again.title == "Om Azom"
    assert again.raw["content"] == "<p>Hej</p>"


def test_update_page_rejects_trash(wp):
    with pytest.raises(SecurityError, match="Invalid page status"):
        wp.update_page(2, status="trash")


# --------------------------------------------------------------------------- #
# Media / Users / Comments / Settings
# --------------------------------------------------------------------------- #


def test_list_media(wp):
    media = wp.list_media()
    assert len(media) == 1
    assert media[0]["mime_type"] == "image/png"


def test_list_users(wp):
    users = wp.list_users()
    assert len(users) == 1
    assert users[0]["name"] == "admin"


def test_list_comments_empty(wp):
    comments = wp.list_comments()
    assert comments == []


def test_get_settings(wp):
    settings = wp.get_settings()
    assert settings["title"] == "Azom"
    assert settings["language"] == "nb"


def test_update_settings(wp):
    result = wp.update_settings(description="Updated description")
    assert result["description"] == "Updated description"
    # Verify persistence
    assert wp.get_settings()["description"] == "Updated description"


def test_update_post_rejects_trash(wp):
    with pytest.raises(SecurityError, match="Invalid post status"):
        wp.update_post(1, status="trash")


def test_update_site_identity_keeps_other_settings(wp):
    updated = wp.update_site_identity(title="Azom Norge", description="Butik")
    assert updated["title"] == "Azom Norge"
    assert updated["description"] == "Butik"
    assert updated["language"] == "nb"
    with pytest.raises(SecurityError, match="No site fields"):
        wp.update_site_identity()


def test_list_themes_marks_the_active_one(wp):
    rows = wp.list_themes()
    active = [row for row in rows if row["status"] == "active"]
    assert len(active) == 1
    assert active[0]["stylesheet"] == "twentytwentyfive"
    with pytest.raises(SecurityError, match="themes"):
        wp.transport.request("POST", "https://mock.local/wp-json/wp/v2/themes", json={})


def test_set_plugin_status_encodes_and_refuses_install(wp):
    updated = wp.set_plugin_status("akismet/akismet", "active")
    assert updated["status"] == "active"
    method, url, body = wp.transport.calls[-1]
    assert method == "POST"
    assert url.endswith("/wp-json/wp/v2/plugins/akismet%2Fakismet")
    assert body == {"status": "active"}
    assert "version" not in body
    with pytest.raises(SecurityError, match="Invalid plugin id"):
        wp.set_plugin_status("../akismet", "inactive")
    with pytest.raises(SecurityError, match="Invalid plugin status"):
        wp.set_plugin_status("akismet/akismet", "network-active")
    with pytest.raises(SecurityError, match="plugins"):
        wp.transport.request("DELETE", "https://mock.local/wp-json/wp/v2/plugins/akismet%2Fakismet")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_discover_namespaces(wp):
    # InMemoryWpTransport doesn't implement /wp-json/ root; returns empty
    ns = wp.discover_namespaces()
    assert isinstance(ns, list)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_invalid_post_id(wp):
    with pytest.raises(SecurityError, match="Invalid WordPress id"):
        wp.get_post("not-a-number")


def test_negative_post_id(wp):
    with pytest.raises(SecurityError, match="Invalid WordPress id"):
        wp.get_post(-1)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def test_wp_client_from_env_mock():
    c = wp_client_from_env(use_mock=True)
    assert isinstance(c.transport, InMemoryWpTransport)
    assert c.base_url == "https://mock.local"


def test_wp_client_from_env_mock_with_domain():
    c = wp_client_from_env(use_mock=True, domain="no")
    assert c.domain == "no"


def test_wp_client_from_env_live_requires_credentials(monkeypatch):
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.delenv("WP_USERNAME", raising=False)
    monkeypatch.delenv("WP_APP_PASSWORD", raising=False)
    with pytest.raises(SecurityError, match="WP_USERNAME and WP_APP_PASSWORD"):
        wp_client_from_env(use_mock=False)


def test_wp_client_from_env_live_with_credentials(monkeypatch):
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.setenv("WP_USERNAME", "admin")
    monkeypatch.setenv("WP_APP_PASSWORD", "xxxx xxxx xxxx xxxx xxxx xxxx")
    monkeypatch.setenv("WOO_BASE_URL", "https://azom.no")
    c = wp_client_from_env(use_mock=False)
    assert c.username == "admin"
    assert c.base_url == "https://azom.no"


def test_wp_client_from_env_live_with_domain(monkeypatch):
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.setenv("WP_USERNAME", "admin")
    monkeypatch.setenv("WP_APP_PASSWORD", "xxxx xxxx xxxx xxxx xxxx xxxx")
    monkeypatch.delenv("WOO_BASE_URL", raising=False)
    monkeypatch.setenv("WOO_BASE_URL_NO", "https://azom.no")
    c = wp_client_from_env(use_mock=False, domain="no")
    assert c.base_url == "https://azom.no"
    assert c.domain == "no"


# --------------------------------------------------------------------------- #
# Shared HttpTransport protocol, RequestsTransport service_name & wp_client_for_site
# --------------------------------------------------------------------------- #


class _CustomDoubleTransport:
    """Custom HttpTransport test double (not a subclass of InMemoryWpTransport)."""

    def __init__(self, *, requires_auth: bool = False) -> None:
        self.requires_auth = requires_auth
        self.calls: list[tuple[str, str, tuple[str, str] | None]] = []

    def request(self, method, url, *, auth=None, json=None, params=None, timeout=30):
        self.calls.append((method, url, auth))
        return [{"stylesheet": "custom-theme", "status": "active"}]


def test_wordpress_client_accepts_custom_transport_with_requires_auth_false(monkeypatch):
    monkeypatch.delenv("WP_USERNAME", raising=False)
    monkeypatch.delenv("WORDPRESS_USERNAME", raising=False)
    monkeypatch.delenv("WP_APP_PASSWORD", raising=False)
    double = _CustomDoubleTransport(requires_auth=False)
    client = WordPressClient(base_url="https://mock.local", transport=double)
    assert client.list_themes() == [{"stylesheet": "custom-theme", "status": "active"}]
    assert double.calls[-1][2] is None


def test_wordpress_client_requires_creds_when_custom_transport_requires_auth(monkeypatch):
    monkeypatch.delenv("WP_USERNAME", raising=False)
    monkeypatch.delenv("WORDPRESS_USERNAME", raising=False)
    monkeypatch.delenv("WP_APP_PASSWORD", raising=False)
    with pytest.raises(SecurityError, match="WP_USERNAME and WP_APP_PASSWORD"):
        WordPressClient(
            base_url="https://example.test",
            transport=_CustomDoubleTransport(requires_auth=True),
        )


def test_requests_transport_uses_service_name_in_errors():
    from ecom_ops.integrations._http_transport import RequestsTransport

    class _Resp:
        def __init__(self, status_code: int, text: str = ""):
            self.status_code = status_code
            self.text = text
            self.content = text.encode("utf-8")
            self.headers: dict[str, str] = {}

        def json(self):
            raise ValueError("bad json")

    class _Sess:
        def __init__(self, resp):
            self._resp = resp

        def request(self, *args, **kwargs):
            return self._resp

    wp_404 = RequestsTransport(
        session=_Sess(_Resp(404, "missing")), max_retries=0, service_name="WordPress"
    )
    with pytest.raises(SecurityError, match=r"WordPress API error 404"):
        wp_404.request("GET", "https://example.test/wp-json/wp/v2/posts")

    wp_bad_json = RequestsTransport(
        session=_Sess(_Resp(200, "not-json")), max_retries=0, service_name="WordPress"
    )
    with pytest.raises(SecurityError, match=r"WordPress API returned non-JSON"):
        wp_bad_json.request("GET", "https://example.test/wp-json/wp/v2/posts")


def test_wp_client_for_site_binds_per_site_secret_and_respects_fallback(monkeypatch):
    from ecom_ops.integrations.wp_component_reads import wp_client_for_site
    from ecom_ops.wp_ops.registry import load_sites, site_by_id

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    site = site_by_id(load_sites(), "mock-shop")
    assert site is not None
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", "site_reader:app-pass-123")
    bound = wp_client_for_site(site, fallback_to_env=False)
    assert bound is not None
    assert bound.base_url == "https://mock-shop.example.test"
    assert bound.username == "site_reader"
    assert bound.app_password == "app-pass-123"

    monkeypatch.delenv("WP_SECRET_MOCK_SHOP", raising=False)
    assert wp_client_for_site(site, fallback_to_env=False) is None

    monkeypatch.setenv("WP_USERNAME", "shared_user")
    monkeypatch.setenv("WP_APP_PASSWORD", "shared-pass")
    monkeypatch.setenv("WP_BASE_URL", "https://fallback.example.test")
    fallback = wp_client_for_site(site, fallback_to_env=True)
    assert fallback is not None
    assert fallback.base_url == "https://fallback.example.test"
    assert fallback.username == "shared_user"
