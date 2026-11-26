"""Compatibility facade for the opt-in local WordPress + PHP runtime.

The implementation lives in ``ecom_ops.integrations.wp.local_runtime`` and
``ecom_ops.integrations.wp.sandbox_sqlite``; this module re-exports the public
API and documented monkeypatch targets.
"""

from __future__ import annotations

import os  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401

from ecom_ops.integrations.wp.local_runtime import (
    _PHP_SYS_VERSION_RE,
    _PLUGIN_ENTRY_RE,
    _PLUGIN_MAIN_RE,
    _STARTUP_TIMEOUT_S,
    LocalWordPressSandbox,
    _build_php_command,
    _copy_component_tree,
    _detect_plugin_main_file,
    _php_escape,
    _render_db_dropin,
    _render_egress_guard,
    _render_router,
    _render_wp_config,
    _write_minimal_synthetic_theme,
    make_local_sandbox_gate,
    resolve_local_runtime,
    runtime_dir_from_env,
)
from ecom_ops.integrations.wp.process_env import (
    ALLOWED_OS_ENV as _ALLOWED_OS_ENV,
)
from ecom_ops.integrations.wp.process_env import (
    FORBIDDEN_ENV_PREFIXES as _FORBIDDEN_PREFIXES,
)
from ecom_ops.integrations.wp.process_env import (
    PROBE_ENV_KEYS as _PROBE_ENV_KEYS,
)
from ecom_ops.integrations.wp.process_env import (
    WP_VERSION_RE as _WP_VERSION_RE,
)
from ecom_ops.integrations.wp.process_env import (
    build_php_child_env,
    probe_subprocess_env,
    terminate_process_tree,
)
from ecom_ops.integrations.wp.process_env import (
    pick_loopback_port as _pick_loopback_port,
)
from ecom_ops.integrations.wp.sandbox_sqlite import (
    _init_sqlite_db,
    _php_serialize_list,
    init_sqlite_db,
    php_serialize_list,
)
from ecom_ops.integrations.wp_runtime_fetch import RuntimeFetchError, check_runtime, default_dest

__all__ = [
    "LocalWordPressSandbox",
    "RuntimeFetchError",
    "_ALLOWED_OS_ENV",
    "_FORBIDDEN_PREFIXES",
    "_PHP_SYS_VERSION_RE",
    "_PLUGIN_ENTRY_RE",
    "_PLUGIN_MAIN_RE",
    "_PROBE_ENV_KEYS",
    "_STARTUP_TIMEOUT_S",
    "_WP_VERSION_RE",
    "_build_php_command",
    "_copy_component_tree",
    "_detect_plugin_main_file",
    "_init_sqlite_db",
    "_php_escape",
    "_php_serialize_list",
    "_pick_loopback_port",
    "_render_db_dropin",
    "_render_egress_guard",
    "_render_router",
    "_render_wp_config",
    "_write_minimal_synthetic_theme",
    "build_php_child_env",
    "check_runtime",
    "default_dest",
    "init_sqlite_db",
    "make_local_sandbox_gate",
    "os",
    "php_serialize_list",
    "probe_subprocess_env",
    "resolve_local_runtime",
    "runtime_dir_from_env",
    "shutil",
    "subprocess",
    "terminate_process_tree",
]
