"""Opt-in WordPress runtime. The default suite does not execute it.

Set AZOM_WP_RUNTIME_E2E=1 only when a pinned local PHP binary and a pinned
WordPress fixture are already present. This file does not pull an image,
open a port, or treat a skip as a rendered theme.
"""

from __future__ import annotations

import os
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AZOM_WP_RUNTIME_E2E") != "1",
    reason=(
        "AZOM_WP_RUNTIME_E2E is unset; no WordPress container or browser run is claimed"
    ),
)


def test_local_wordpress_render_is_opt_in() -> None:
    assert shutil.which("php"), "PHP binary missing"
    pytest.fail(
        "No pinned local WordPress runtime is wired. "
        "Do not treat this failure as a rendered theme."
    )
