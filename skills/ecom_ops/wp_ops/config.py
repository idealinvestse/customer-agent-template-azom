"""Centralized fail-closed environment configuration for WordPress site operations.

Per ``docs/WORDPRESS_OPS.md``, an unset or blank ``AZOM_USE_MOCK`` is treated as
mock mode (``is_mock_mode() -> True``). Live REST reads, live SFTP file reads,
and live WordPress.org metadata fetches stay disabled unless ``AZOM_USE_MOCK``
is explicitly turned off (``0``, ``false``, ``no``, ``off``) and the respective
opt-in flag is enabled.
"""

from __future__ import annotations

import os
from pathlib import Path

from ecom_ops.runtime_env import env_flag

_MOCK_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def is_mock_mode() -> bool:
    """Return True unless ``AZOM_USE_MOCK`` is explicitly disabled."""
    raw = os.environ.get("AZOM_USE_MOCK")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in _MOCK_FALSE_VALUES


def rest_reads_allowed() -> bool:
    """Live content reads only when mock mode is explicitly off."""
    return not is_mock_mode()


def ssh_files_enabled() -> bool:
    """Live read-only SFTP file reads only when mock is explicitly off and flag is set."""
    return env_flag("AZOM_WP_FILES_SSH") and not is_mock_mode()


def wporg_intel_live_enabled() -> bool:
    """Live api.wordpress.org metadata only when mock is off and ``AZOM_WPORG_INTEL=1``."""
    return (not is_mock_mode()) and os.environ.get("AZOM_WPORG_INTEL", "").strip() == "1"


def data_dir_path(data_dir: Path | None = None) -> Path:
    """Resolve ``AZOM_DATA_DIR`` (or explicit override), defaulting to ``.azom-data``."""
    if data_dir is not None:
        return Path(data_dir)
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def config_dir_path() -> Path:
    """Resolve ``AZOM_CONFIG_DIR`` or the repository ``config/`` directory."""
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"
