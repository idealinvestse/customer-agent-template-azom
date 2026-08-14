"""Promote staged FAQ article drafts into config/faq/ (Oscar + --apply)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ecom_ops.faq.rbac_ingest import require_faq_promote
from ecom_ops.faq.staging import articles_staging_dir
from ecom_ops.faq.store import _parse_article
from ecom_ops.rbac import AccessDenied, Actor


def _config_faq_dir(market: str) -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    root = Path(override) if override else Path(__file__).resolve().parents[3] / "config"
    return root / "faq" / market.strip().lower()


@dataclass
class PromoteResult:
    ok: bool
    message: str
    dry_run: bool = True
    dest: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "dry_run": self.dry_run,
            "dest": self.dest,
            "details": self.details or {},
        }


def _load_staging_article(path: Path) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return None
    return raw


def promote_article(
    article_id: str,
    *,
    market: str | None = None,
    apply: bool = False,
    force: bool = False,
    actor: Actor | str | None = None,
) -> PromoteResult:
    """Copy staging draft to config/faq/{market}/ after validate.

    Without ``apply`` returns dry-run diff summary. Never auto-publishes to WP.
    Ingest kill-switch does not block promote (Oscar may finish a reviewed draft).
    Existing dest files require ``force=True`` (dry-run and apply).
    """
    try:
        actor_obj = require_faq_promote(actor)
    except AccessDenied as exc:
        return PromoteResult(ok=False, message=str(exc), dry_run=not apply)

    aid = (article_id or "").strip()
    if not aid:
        return PromoteResult(ok=False, message="Missing article id", dry_run=not apply)

    mkt = (market or aid.split("-", 1)[0]).strip().lower()
    if mkt not in {"se", "no", "dk"}:
        return PromoteResult(
            ok=False, message=f"Unsupported market: {mkt}", dry_run=not apply
        )

    staging = articles_staging_dir(mkt)
    src = staging / f"{aid}.yaml"
    if not src.is_file():
        # allow id without path match via scan
        matches = list(staging.glob(f"{aid}.y*ml"))
        if not matches:
            return PromoteResult(
                ok=False,
                message=f"Staging draft not found: {aid}",
                dry_run=not apply,
            )
        src = matches[0]

    article = _load_staging_article(src)
    if not article:
        return PromoteResult(
            ok=False, message=f"Invalid staging YAML: {src}", dry_run=not apply
        )

    # HITL: promote never marks customer-facing; Oscar edits YAML later.
    article["customer_safe"] = False
    article["needs_review"] = True
    article["market"] = mkt

    parsed, issues = _parse_article(
        article, source_path=str(src), path_market=mkt
    )
    errors = [i.message for i in issues if i.level == "error"]
    if parsed is None or errors:
        return PromoteResult(
            ok=False,
            message="Validation failed: " + "; ".join(errors or ["invalid article"]),
            dry_run=not apply,
            details={"issues": [i.message for i in issues]},
        )

    dest_dir = _config_faq_dir(mkt)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"ingest_{aid}.yaml"
    existing = ""
    if dest.is_file():
        existing = dest.read_text(encoding="utf-8")
    if existing and not force:
        return PromoteResult(
            ok=False,
            message=(
                f"Destination exists ({dest}); pass --force to overwrite. "
                "Promote does not check the ingest kill-switch."
            ),
            dry_run=not apply,
            dest=str(dest),
            details={
                "actor": actor_obj.name,
                "overwrite_requires_force": True,
            },
        )
    new_body = yaml.safe_dump([article], allow_unicode=True, sort_keys=False)
    diff_note = (
        f"would write {dest} ({len(new_body)} bytes"
        + ("; overwrite existing" if existing else "; new file")
        + ")"
    )
    if not apply:
        return PromoteResult(
            ok=True,
            message=f"Dry-run promote {aid}: {diff_note}",
            dry_run=True,
            dest=str(dest),
            details={
                "actor": actor_obj.name,
                "customer_safe": article.get("customer_safe"),
                "needs_review": article.get("needs_review"),
            },
        )

    dest.write_text(new_body, encoding="utf-8")
    return PromoteResult(
        ok=True,
        message=f"Promoted {aid} → {dest}",
        dry_run=False,
        dest=str(dest),
        details={"actor": actor_obj.name},
    )
