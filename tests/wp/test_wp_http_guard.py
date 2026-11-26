"""Unit tests for integrations/wp/ HTTP guards, ProxyHandler({}), redirects, process_env, and PNG composite."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from _helpers import make_solid_png, site_row

from ecom_ops.integrations import wp as integrations_wp
from ecom_ops.integrations import wp_live_preview, wp_org_client, wp_runtime_fetch
from ecom_ops.integrations.wp import http_guard, png_composite, process_env
from ecom_ops.integrations.wp.http_guard import (
    AllowlistedSiteRedirectHandler,
    fetch_site_rest_json,
)
from ecom_ops.integrations.wp.runtime_fetch import _RefuseRedirect
from ecom_ops.wp_ops.models import parse_site


class _FakeHttpResponse:
    def __init__(self, body: bytes, *, final_url: str, status: int = 200) -> None:
        self._buf = io.BytesIO(body)
        self._final_url = final_url
        self.status = status

    def geturl(self) -> str:
        return self._final_url

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_integrations_wp_init_does_not_eagerly_import_wordpress_client_or_paramiko() -> None:
    assert hasattr(integrations_wp, "__doc__")
    assert not hasattr(integrations_wp, "WordPressClient")
    assert not hasattr(integrations_wp, "paramiko")
    assert "ecom_ops.integrations.wordpress" not in integrations_wp.__dict__


def test_http_guard_fetch_live_html_and_rest_json_use_proxy_handler_and_host_pinned_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = parse_site(site_row("azom-no", canonical_url="https://azom.no/"))
    seen_handlers: list[tuple[object, ...]] = []
    seen_urls: list[str] = []

    class _FakeOpener:
        def __init__(self, body: bytes, final_url: str) -> None:
            self._body = body
            self._final_url = final_url

        def open(self, req: urllib.request.Request, timeout: float = 10.0) -> _FakeHttpResponse:
            del timeout
            seen_urls.append(req.full_url)
            return _FakeHttpResponse(self._body, final_url=self._final_url)

    monkeypatch.setattr(wp_live_preview, "_host_resolves_public", lambda _h: True)
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: (
            seen_handlers.append(handlers)
            or _FakeOpener(b"<html><body>ok</body></html>", "https://azom.no/")
        ),
    )

    fetched = wp_live_preview.fetch_live_html(site, "/")
    assert fetched["status"] == 200
    assert seen_urls == ["https://azom.no/"]
    assert len(seen_handlers) == 1
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in seen_handlers[0])
    redirect_handlers = [h for h in seen_handlers[0] if isinstance(h, AllowlistedSiteRedirectHandler)]
    assert len(redirect_handlers) == 1
    assert redirect_handlers[0]._allow_wp_json is False

    # fetch_site_rest_json also installs ProxyHandler({}) + AllowlistedSiteRedirectHandler(allow_wp_json=True)
    seen_handlers.clear()
    seen_urls.clear()
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: (
            seen_handlers.append(handlers)
            or _FakeOpener(json.dumps([{"id": 1}]).encode("utf-8"), "https://azom.no/wp-json/wc/store/v1/products")
        ),
    )
    payload = fetch_site_rest_json(site, "/wp-json/wc/store/v1/products", params={"per_page": 5})
    assert payload == [{"id": 1}]
    assert seen_urls == ["https://azom.no/wp-json/wc/store/v1/products?per_page=5"]
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in seen_handlers[0])
    rest_redirects = [h for h in seen_handlers[0] if isinstance(h, AllowlistedSiteRedirectHandler)]
    assert len(rest_redirects) == 1
    assert rest_redirects[0]._allow_wp_json is True

    # Redirect handler refuses foreign host, non-HTTPS, and blocked WP paths
    req = urllib.request.Request("https://azom.no/wp-json/wc/store/v1/products")
    for bad_target in (
        "https://evil.example/wp-json/wc/store/v1/products",
        "http://azom.no/wp-json/wc/store/v1/products",
        "https://azom.no/wp-admin/",
        "https://azom.no/kasse/",
    ):
        with pytest.raises(urllib.error.HTTPError):
            rest_redirects[0].redirect_request(req, None, 302, "Found", {}, bad_target)

    # Disallowed DNS resolution fails closed on fetch_site_rest_json
    monkeypatch.setattr(wp_live_preview, "_host_resolves_public", lambda _h: False)
    assert fetch_site_rest_json(site, "/wp-json/wc/store/v1/products") is None


def test_wp_org_client_uses_proxy_handler_and_host_pinned_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WPORG_INTEL", "1")
    seen_handlers: list[tuple[object, ...]] = []

    class _FakeOpener:
        def __init__(self, body: bytes, final_url: str) -> None:
            self._body = body
            self._final_url = final_url

        def open(self, req: urllib.request.Request, timeout: float = 5.0) -> _FakeHttpResponse:
            del req, timeout
            return _FakeHttpResponse(self._body, final_url=self._final_url)

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: (
            seen_handlers.append(handlers)
            or _FakeOpener(
                json.dumps({"version": "9.1.0"}).encode("utf-8"),
                "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information",
            )
        ),
    )
    out = wp_org_client.fetch_wporg_json(
        "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information"
    )
    assert out == {"version": "9.1.0"}
    assert len(seen_handlers) == 1
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in seen_handlers[0])
    redirect_handlers = [h for h in seen_handlers[0] if isinstance(h, AllowlistedSiteRedirectHandler)]
    assert len(redirect_handlers) == 1
    assert redirect_handlers[0]._allowed_hosts == frozenset({"api.wordpress.org"})

    # Redirect off api.wordpress.org is refused
    req = urllib.request.Request("https://api.wordpress.org/plugins/info/1.2/")
    with pytest.raises(urllib.error.HTTPError):
        redirect_handlers[0].redirect_request(req, None, 302, "Found", {}, "https://evil.example/payload")

    # Final URL escaping api.wordpress.org is also refused
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: _FakeOpener(b'{"version":"1.0"}', "https://evil.example/hijack"),
    )
    with pytest.raises(wp_org_client.WpOrgClientError):
        wp_org_client.fetch_wporg_json(
            "https://api.wordpress.org/plugins/info/1.2/?action=plugin_information"
        )

    # Userinfo and non-443 port tricks are refused before any opener call
    for bad_url in (
        "https://api.wordpress.org@evil.example/api",
        "https://user:pass@api.wordpress.org/plugins/info/1.2/",
        "https://api.wordpress.org:8443/plugins/info/1.2/",
    ):
        with pytest.raises(ValueError):
            wp_org_client.fetch_wporg_json(bad_url)


def test_wp_runtime_fetch_open_pinned_installs_empty_proxy_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_handlers: list[tuple[object, ...]] = []

    class _FakeOpener:
        def open(self, req: urllib.request.Request, timeout: float = 120) -> _FakeHttpResponse:
            del timeout
            return _FakeHttpResponse(b"ok", final_url=req.full_url)

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: seen_handlers.append(handlers) or _FakeOpener(),
    )
    resp = wp_runtime_fetch._open_pinned(wp_runtime_fetch.WORDPRESS_URL)
    assert resp.status == 200
    assert len(seen_handlers) == 1
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in seen_handlers[0])
    assert any(isinstance(h, _RefuseRedirect) for h in seen_handlers[0])


def test_process_env_and_png_composite_reexports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", "must-never-leak")
    child_env = process_env.build_php_child_env()
    assert child_env["AZOM_REVIEW_SYNTHETIC"] == "1"
    assert "WP_SECRET_MOCK_SHOP" not in child_env

    probe_env = process_env.probe_subprocess_env()
    assert "WP_SECRET_MOCK_SHOP" not in probe_env

    port = process_env.pick_loopback_port()
    assert 1024 <= port <= 65535
    assert "T" in process_env.utc_now_iso()

    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    p1.write_bytes(make_solid_png(12, 10, (10, 20, 30)))
    p2.write_bytes(make_solid_png(12, 10, (40, 50, 60)))
    out = wp_live_preview.build_comparison_composite([("Fore", p1), ("Efter", p2)], tmp_path / "cmp.png")
    assert out == tmp_path / "cmp.png"
    dims = png_composite.png_dimensions(out.read_bytes())
    assert dims == (26, 48)
    assert wp_live_preview.BLOCKED_WP_PATHS is http_guard.BLOCKED_WP_PATHS
    assert wp_runtime_fetch.sys is sys
