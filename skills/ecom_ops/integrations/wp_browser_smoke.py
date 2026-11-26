"""Compatibility facade for loopback headless browser smoke and vertical renders.

The implementation lives in ``ecom_ops.integrations.wp.sandbox_smoke`` and
``ecom_ops.integrations.wp.cdp_browser``; this module re-exports the public API
and documented monkeypatch targets.
"""

from __future__ import annotations

from ecom_ops.integrations.wp.cdp_browser import (
    DEFAULT_VIEWPORTS as VIEWPORTS,
)
from ecom_ops.integrations.wp.cdp_browser import (
    PNG_MAGIC as _PNG_MAGIC,
)
from ecom_ops.integrations.wp.cdp_browser import (
    WP_FATAL_MARKERS as _WP_FATAL_MARKERS,
)
from ecom_ops.integrations.wp.cdp_browser import (
    LoopbackCdpClient as _LoopbackCdpClient,
)
from ecom_ops.integrations.wp.cdp_browser import (
    assert_loopback_http_url,
    find_browser_binary,
)
from ecom_ops.integrations.wp.cdp_browser import (
    is_loopback_or_inline_url as _is_loopback_or_inline_url,
)
from ecom_ops.integrations.wp.cdp_browser import (
    png_dimensions as _png_dimensions,
)
from ecom_ops.integrations.wp.cdp_browser import (
    wait_cdp_page_ws as _wait_cdp_page_ws,
)
from ecom_ops.integrations.wp.process_env import (
    build_php_child_env,
    terminate_process_tree,
)
from ecom_ops.integrations.wp.process_env import (
    pick_loopback_port as _pick_loopback_port,
)
from ecom_ops.integrations.wp.process_env import (
    utc_now_iso as _now,
)
from ecom_ops.integrations.wp.sandbox_smoke import (
    _build_binding,
    _classify_theme_render,
    _probe_loopback_http,
    _text_files,
    _write_egress_probe,
    run_browser_render,
    run_vertical_plugin_smoke,
    run_vertical_theme_render,
)
from ecom_ops.integrations.wp_local_runtime import (
    LocalWordPressSandbox,
    make_local_sandbox_gate,
    resolve_local_runtime,
    runtime_dir_from_env,
)
from ecom_ops.integrations.wp_review_process import run_php_lint

__all__ = [
    "LocalWordPressSandbox",
    "VIEWPORTS",
    "_LoopbackCdpClient",
    "_PNG_MAGIC",
    "_WP_FATAL_MARKERS",
    "_build_binding",
    "_classify_theme_render",
    "_is_loopback_or_inline_url",
    "_now",
    "_pick_loopback_port",
    "_png_dimensions",
    "_probe_loopback_http",
    "_text_files",
    "_wait_cdp_page_ws",
    "_write_egress_probe",
    "assert_loopback_http_url",
    "build_php_child_env",
    "find_browser_binary",
    "make_local_sandbox_gate",
    "resolve_local_runtime",
    "run_browser_render",
    "run_php_lint",
    "run_vertical_plugin_smoke",
    "run_vertical_theme_render",
    "runtime_dir_from_env",
    "terminate_process_tree",
]
