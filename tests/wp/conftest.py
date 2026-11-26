"""Non-autouse pytest fixtures for WordPress ops tests under tests/wp/.

Safety flags (such as ``mock="1"`` and ``files_ssh="0"``) are intentionally
required at call sites as explicit fail-closed documentation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_WP_TESTS_DIR = str(Path(__file__).resolve().parent)
if _WP_TESTS_DIR not in sys.path:
    sys.path.insert(0, _WP_TESTS_DIR)

from _helpers import (  # noqa: E402
    CONFIG_DIR,
    MOCK_REGISTRY,
    dashboard_auth_headers,
    load_dashboard_app,
    seed_mock_child_theme,
)

from ecom_ops.integrations.wp_live_preview import clear_live_html_cache  # noqa: E402
from ecom_ops.model_router import reset_model_router  # noqa: E402
from ecom_ops.rbac import clear_rbac_cache  # noqa: E402
from ecom_ops.wp_ops.agent_tools import WpAgentTools  # noqa: E402
from ecom_ops.wp_ops.service import WpOpsService  # noqa: E402


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(path))
    return path


@pytest.fixture
def no_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    reset_model_router()


@pytest.fixture
def actor_maps(monkeypatch: pytest.MonkeyPatch):
    def _configure(
        *,
        telegram: dict[int, str] | None = None,
        messenger: dict[str, str] | None = None,
    ) -> None:
        if telegram is not None:
            monkeypatch.setenv(
                "TELEGRAM_ALLOWED_CHAT_IDS",
                ",".join(str(cid) for cid in telegram),
            )
            monkeypatch.setenv(
                "TELEGRAM_ACTOR_MAP",
                ",".join(f"{cid}:{actor}" for cid, actor in telegram.items()),
            )
        if messenger is not None:
            monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", ",".join(messenger))
            monkeypatch.setenv(
                "MESSENGER_ACTOR_MAP",
                ",".join(f"{psid}:{actor}" for psid, actor in messenger.items()),
            )

    return _configure


@pytest.fixture
def wp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Configure WP ops env vars with explicit, non-defaulted safety flags."""

    def _configure(
        *,
        mock: str,
        files_ssh: str,
        sandbox: str | None = None,
        live_preview: str | None = None,
        data_dir: Path | None = None,
        config_dir: Path | None = None,
        disable_grok: bool = True,
        clear_live_cache: bool = False,
        reset_router: bool = False,
        extra: dict[str, str | None] | None = None,
    ) -> Path:
        root_dir = data_dir or tmp_path
        monkeypatch.setenv("AZOM_USE_MOCK", mock)
        monkeypatch.setenv("AZOM_WP_FILES_SSH", files_ssh)
        if sandbox is not None:
            monkeypatch.setenv("AZOM_WP_SANDBOX", sandbox)
        if live_preview is not None:
            monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", live_preview)
        if disable_grok:
            monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
            monkeypatch.setenv("XAI_GROK_BIN", "")
        monkeypatch.setenv("AZOM_CONFIG_DIR", str(config_dir or CONFIG_DIR))
        monkeypatch.setenv("AZOM_DATA_DIR", str(root_dir))
        monkeypatch.setenv("AZOM_AUDIT_PATH", str(root_dir / "audit.jsonl"))
        if extra:
            for key, value in extra.items():
                if value is None:
                    monkeypatch.delenv(key, raising=False)
                else:
                    monkeypatch.setenv(key, value)
        clear_rbac_cache()
        if clear_live_cache:
            clear_live_html_cache()
        if reset_router:
            reset_model_router()
        return root_dir

    return _configure


@pytest.fixture
def mock_child_theme(tmp_path: Path):
    def _seed(
        *,
        data_dir: Path | None = None,
        site_id: str = "mock-staging",
        slug: str = "azom-child",
        style_css: str | None = None,
        functions_php: str = "<?php\n// child theme\n",
    ) -> Path:
        return seed_mock_child_theme(
            data_dir or tmp_path,
            site_id=site_id,
            slug=slug,
            style_css=style_css,
            functions_php=functions_php,
        )

    return _seed


@pytest.fixture
def mock_wp_service(tmp_path: Path, wp_env):
    def _build(
        *,
        mock: str,
        files_ssh: str,
        sandbox: str | None = None,
        live_preview: str | None = None,
        registry_path: Path | None = None,
        data_dir: Path | None = None,
        disable_grok: bool = True,
        clear_live_cache: bool = False,
        reset_router: bool = False,
        extra: dict[str, str | None] | None = None,
        **service_kwargs: Any,
    ) -> WpOpsService:
        root_dir = wp_env(
            mock=mock,
            files_ssh=files_ssh,
            sandbox=sandbox,
            live_preview=live_preview,
            data_dir=data_dir or tmp_path,
            disable_grok=disable_grok,
            clear_live_cache=clear_live_cache,
            reset_router=reset_router,
            extra=extra,
        )
        return WpOpsService(
            registry_path=registry_path or MOCK_REGISTRY,
            data_dir=root_dir,
            **service_kwargs,
        )

    return _build


@pytest.fixture
def wp_service_tools(mock_wp_service, mock_child_theme):
    def _build(
        *,
        mock: str,
        files_ssh: str,
        sandbox: str | None = None,
        live_preview: str | None = None,
        registry_path: Path | None = None,
        data_dir: Path | None = None,
        with_child_theme: bool = False,
        style_css: str | None = None,
        functions_php: str = "<?php\n// child theme\n",
        disable_grok: bool = True,
        clear_live_cache: bool = False,
        reset_router: bool = False,
        extra: dict[str, str | None] | None = None,
        **service_kwargs: Any,
    ) -> tuple[WpOpsService, WpAgentTools]:
        service = mock_wp_service(
            mock=mock,
            files_ssh=files_ssh,
            sandbox=sandbox,
            live_preview=live_preview,
            registry_path=registry_path,
            data_dir=data_dir,
            disable_grok=disable_grok,
            clear_live_cache=clear_live_cache,
            reset_router=reset_router,
            extra=extra,
            **service_kwargs,
        )
        if with_child_theme:
            mock_child_theme(
                data_dir=service.data_dir,
                style_css=style_css,
                functions_php=functions_php,
            )
        return service, WpAgentTools(service)

    return _build


@pytest.fixture
def dashboard_app():
    """Function-scoped dashboard app loader (reloads wp_routes cleanly)."""

    def _load(*, testing: bool = True):
        app = load_dashboard_app()
        if testing:
            app.config["TESTING"] = True
        return app

    return _load


@pytest.fixture
def dashboard_auth():
    return dashboard_auth_headers
