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
