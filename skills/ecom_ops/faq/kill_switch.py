"""FAQ publish kill-switch (fail-closed when env set)."""

from __future__ import annotations

import os

from ecom_ops.faq.config import load_faq_config


def faq_publish_killed() -> bool:
    cfg = load_faq_config()
    env = cfg.publish_kill_env or "AZOM_FAQ_PUBLISH_KILL"
    return os.environ.get(env, "").strip() in {"1", "true", "yes", "on"}
