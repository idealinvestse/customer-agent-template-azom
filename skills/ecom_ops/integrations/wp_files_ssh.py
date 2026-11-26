"""Compatibility facade for read-only, site-scoped SFTP file transport.

The implementation lives in ``ecom_ops.integrations.wp.sftp_transport``; this
module re-exports the public API and documented monkeypatch targets.
"""

from __future__ import annotations

import socket  # noqa: F401

import paramiko  # noqa: F401

from ecom_ops.integrations.wp.sftp_transport import (
    _CHANNEL_TIMEOUT,
    _CONNECT_TIMEOUT,
    _ENTRY,
    _EXTENSIONS,
    _HOST,
    _MAX_BYTES,
    _MAX_DIRECTORY_ENTRIES,
    _SEGMENT,
    _SENSITIVE_NAME,
    _SLUG,
    SshFileTransport,
    _is_missing_error,
    build_file_transport,
    host_allowed,
)
from ecom_ops.wp_ops.config import ssh_files_enabled

__all__ = [
    "SshFileTransport",
    "_CHANNEL_TIMEOUT",
    "_CONNECT_TIMEOUT",
    "_ENTRY",
    "_EXTENSIONS",
    "_HOST",
    "_MAX_BYTES",
    "_MAX_DIRECTORY_ENTRIES",
    "_SEGMENT",
    "_SENSITIVE_NAME",
    "_SLUG",
    "_is_missing_error",
    "build_file_transport",
    "host_allowed",
    "paramiko",
    "socket",
    "ssh_files_enabled",
]
