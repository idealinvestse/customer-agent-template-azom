#!/usr/bin/env python3
"""Fetch pinned WordPress and PHP onto the machine that runs this script.

The download is not a sandbox. Nothing here binds a port, starts php -S,
or sets AZOM_WP_SANDBOX. Linux and macOS stop before a download because
PHP publishes no official binary for those systems.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    skills = root / "skills"
    if str(skills) not in sys.path:
        sys.path.insert(0, str(skills))
    from ecom_ops.integrations.wp_runtime_fetch import main

    raise SystemExit(main())
