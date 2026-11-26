"""Compatibility facade for the pinned WordPress + PHP runtime fetcher."""

from __future__ import annotations

import platform
import subprocess
import sys

from ecom_ops.integrations.wp.runtime_fetch import (
    _PROBE_ENV,
    _REDIRECTS,
    _VERSION_FILE,
    ALLOWED_HOSTS,
    MAX_ARCHIVE_BYTES,
    MAX_MEMBERS,
    MAX_REDIRECTS,
    MAX_UNCOMPRESSED,
    PHP_VERSION,
    PHP_WINDOWS,
    PROFILE,
    PROFILE_VERSION,
    USER_AGENT,
    WORDPRESS_SHA1,
    WORDPRESS_URL,
    WORDPRESS_VERSION,
    RuntimeFetchError,
    _open_pinned,
    _RefuseRedirect,
    assert_pinned_url,
    check_runtime,
    default_dest,
    download,
    extract_zip,
    install,
    main,
    probe_php,
    resolve_php_from_manifest,
    resolve_runtime_manifest,
    select_pins,
)

__all__ = [
    "ALLOWED_HOSTS",
    "MAX_ARCHIVE_BYTES",
    "MAX_MEMBERS",
    "MAX_REDIRECTS",
    "MAX_UNCOMPRESSED",
    "PHP_VERSION",
    "PHP_WINDOWS",
    "PROFILE",
    "PROFILE_VERSION",
    "USER_AGENT",
    "WORDPRESS_SHA1",
    "WORDPRESS_URL",
    "WORDPRESS_VERSION",
    "RuntimeFetchError",
    "_PROBE_ENV",
    "_REDIRECTS",
    "_RefuseRedirect",
    "_VERSION_FILE",
    "_open_pinned",
    "assert_pinned_url",
    "check_runtime",
    "default_dest",
    "download",
    "extract_zip",
    "install",
    "main",
    "platform",
    "probe_php",
    "resolve_php_from_manifest",
    "resolve_runtime_manifest",
    "select_pins",
    "subprocess",
    "sys",
]


if __name__ == "__main__":
    raise SystemExit(main())
