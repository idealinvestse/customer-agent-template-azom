"""HITL staging approval, draft page push, and fail-closed production refusal.

Production deploy always refuses with ``BackupNotVerified``.
``push_page_draft`` returns ``dict[str, Any]`` and creates WordPress pages strictly
with ``status='draft'`` after PII and XSS checks.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.config import rest_reads_allowed
from ecom_ops.wp_ops.file_paths import PathRejected, TransportError, resolve_file
from ecom_ops.wp_ops.file_policy import is_safe_pure_css_file, production_deploy_block
from ecom_ops.wp_ops.file_transport import LocalTree
from ecom_ops.wp_ops.html_sanitize import sanitize_page_draft_html
from ecom_ops.wp_ops.models import WpOpsResult
from ecom_ops.wp_ops.pii import contains_real_pii
from ecom_ops.wp_ops.review.preview_coordinator import _load_page_draft
from ecom_ops.wp_ops.review.store import (
    _SANDBOX_ONLY_FILES,
    _STAGE_OK,
    _UNREACHABLE,
    _diff_ops,
    _fail,
    _ok,
    _sha,
)
from ecom_ops.wp_ops.review.workspace_io import (
    ReviewError,
    workspace_root,
    write_manifest,
)


def approve_staging(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    *,
    actor: str,
    base_sha256: str,
    work_sha256: str,
) -> WpOpsResult:
    """Apply a verified, low-risk, hash-pinned review plan to a local mock staging tree."""
    denied = plans_obj._need(
        actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_staging"
    )
    if denied is not None:
        return denied
    with plans_obj._locks(site_id, plan_id):
        plan = plans_obj._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        blocked = plans_obj._gate(plan, base_sha256, work_sha256)
        if blocked is not None:
            return blocked
        if plan.get("risk") == "high":
            return _fail("HighRiskBlocked", "Hög risk skrivs inte.", plan_id=plan_id, site_id=site_id)
        site, missing = plans_obj._site(site_id)
        if missing is not None:
            return missing
        assert site is not None
        sites = plans_obj._sites()
        if site.environment != "staging":
            if any(item.environment == "staging" for item in sites):
                return _fail("ProductionWriteDisabled", "Produktion skrivs inte.", plan_id=plan_id)
            return _fail("StagingUnconfigured", "Ingen staging-sajt är registrerad.", plan_id=plan_id)
        transport = plans_obj._transport(site)
        if not isinstance(transport, LocalTree):
            return _fail(
                "DeployTransportUnavailable",
                "Transporten tar inte emot skrivning.",
                plan_id=plan_id,
            )
        if plan.get("status") not in _STAGE_OK:
            return _fail(
                "TestsNotPassed",
                "Kontrollerna är inte godkända.",
                plan_id=plan_id,
                status=plan.get("status"),
            )
        if plan.get("status") != "tests_passed":
            return _fail(
                "EvidenceMissing",
                "Äldre stagingflagga öppnar inte grinden.",
                plan_id=plan_id,
                status=plan.get("status"),
            )
        evidence = plans_obj._require_current_evidence(plan)
        if evidence is not None:
            return evidence
        root = workspace_root(plans_obj.data_dir, site_id, plan_id)
        all_diff_ops = _diff_ops(root)
        if not all_diff_ops:
            return _fail("NoChange", "Arbetsytan matchar basen.", plan_id=plan_id)
        diff_ops = [entry for entry in all_diff_ops if entry[1] not in _SANDBOX_ONLY_FILES]
        if not diff_ops:
            return _fail(
                "SandboxBlueprintOnly",
                "Planen innehåller endast sandbox-utkast (page_draft.json / product_edits.json) "
                "och inga deployerbara tema-/plugin-filer.",
                plan_id=plan_id,
            )
        new_component = plan.get("theme_relationship") in {"no_child", "site_plugin", "mu_plugin"}
        actions: list[tuple[str, str, str, bytes | None]] = []
        notes: list[dict[str, Any]] = []
        before_files: dict[str, bytes] = {}
        after_files: dict[str, bytes] = {}
        for op, relative, data, in_base in diff_ops:
            try:
                absolute = resolve_file(
                    site.files_root, str(plan.get("kind")), str(plan.get("slug")), relative
                )
            except (PathRejected, ValueError):
                return _fail("PathRejected", "Sökvägen avvisades.", plan_id=plan_id)
            if op == "delete":
                try:
                    local_target = transport._local_checked(absolute)
                except (PathRejected, ValueError):
                    return _fail("PathRejected", "Sökvägen avvisades.", plan_id=plan_id)
                if local_target.is_dir():
                    return _fail("PathRejected", "Katalog kan inte raderas.", plan_id=plan_id)
                if local_target.is_file():
                    before_files[relative] = local_target.read_bytes()
                actions.append(("delete", absolute, relative, None))
                notes.append({"path": relative, "op": "deleted", "bytes": 0, "sha256": None})
                continue

            assert data is not None
            exists_on_staging = True
            existing_bytes: bytes | None = None
            try:
                existing_bytes = transport.read_bytes(absolute)
            except TransportError as exc:
                if exc.error_class == "FileMissing":
                    exists_on_staging = False
                else:
                    return _fail("FileMissing", "Filen finns inte på mockträdet.", plan_id=plan_id)
            except ValueError:
                return _fail("FileMissing", "Filen finns inte på mockträdet.", plan_id=plan_id)

            if exists_on_staging:
                if existing_bytes is not None:
                    before_files[relative] = existing_bytes
                after_files[relative] = data
                actions.append(("write", absolute, relative, data))
                notes.append({"path": relative, "op": "updated", "bytes": len(data), "sha256": _sha(data)})
            elif in_base and not new_component:
                return _fail("FileMissing", "Filen finns inte på mockträdet.", plan_id=plan_id)
            else:
                after_files[relative] = data
                actions.append(("create", absolute, relative, data))
                notes.append({"path": relative, "op": "created", "bytes": len(data), "sha256": _sha(data)})

        applied_actions: list[tuple[str, str, str]] = []
        try:
            for action, absolute, relative, data in actions:
                if action == "write":
                    assert data is not None
                    transport.write_bytes(absolute, data)
                elif action == "create":
                    assert data is not None
                    transport.create_bytes(absolute, data, overwrite=False)
                elif action == "delete":
                    local_target = transport._local_checked(absolute)
                    if local_target.is_file():
                        backup = local_target.with_name(local_target.name + ".azom-bak")
                        backup.write_bytes(local_target.read_bytes())
                        local_target.unlink()
                applied_actions.append((action, absolute, relative))
        except Exception as exc:
            # Atomic rollback of partial local staging writes on error.
            for prev_action, prev_abs, prev_rel in reversed(applied_actions):
                try:
                    if prev_action in {"write", "delete"} and prev_rel in before_files:
                        transport.create_bytes(prev_abs, before_files[prev_rel], overwrite=True)
                    elif prev_action == "create":
                        target_p = transport._local_checked(prev_abs)
                        if target_p.is_file():
                            target_p.unlink()
                except Exception:
                    pass
            return _fail("StagingWriteFailed", f"Staging-skrivning avbröts: {type(exc).__name__}", plan_id=plan_id)

        plan["status"] = "staged"
        plan["label"] = "mock"
        plans_obj._replace(plan)
        from ecom_ops.wp_ops.change_ledger import safe_record_change

        safe_record_change(
            data_dir=plans_obj.data_dir,
            site_id=site_id,
            environment=site.environment,
            category="mu_plugin_deploy" if plan.get("kind") == "mu_plugin" else "workspace_deploy",
            transport="local_mock",
            actor=actor,
            summary=str(plan.get("summary") or f"Staging {plan_id} ({plan.get('kind')}/{plan.get('slug')})"),
            target_ref=f"{plan_id}:{plan.get('kind')}/{plan.get('slug')}",
            file_entries=notes,
            before_files=before_files,
            after_files=after_files,
            metadata={
                "plan_id": plan_id,
                "kind": plan.get("kind"),
                "slug": plan.get("slug"),
                "base_sha256": base_sha256,
                "work_sha256": work_sha256,
            },
        )
    plans_obj._audit(actor, plan, "wp_review_staging", True, files=notes, label="mock")
    return _ok(
        "Mockträdet uppdaterades.",
        plan_id=plan_id,
        site_id=site_id,
        status="staged",
        executed=True,
        label="mock",
        bytes_copied=False,
        backup_offsite=False,
    )


def push_page_draft(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    *,
    actor: str,
    base_sha256: str,
    work_sha256: str,
    client: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """Push ``work/page_draft.json`` to WordPress strictly as ``status='draft'`` (Oscar-only HITL)."""
    from ecom_ops.integrations import wp_client_from_env
    from ecom_ops.integrations.wp_live_preview import _STATIC_ASSET_HOSTS, _allowed_site_hosts

    def _err(error_class: str, message: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error_class": error_class,
            "message": message,
            "plan_id": plan_id,
            "site_id": site_id,
            "executed": False,
            "live_calls": False,
            **extra,
        }

    denied = plans_obj._need(
        actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_push_draft"
    )
    if denied is not None:
        return _err(
            str((denied.data or {}).get("error_class") or "AccessDenied"),
            denied.message or "Endast Oscar (full_admin) får skicka WordPress-utkast.",
        )

    if (
        not isinstance(base_sha256, str)
        or not base_sha256.strip()
        or not isinstance(work_sha256, str)
        or not work_sha256.strip()
    ):
        return _err("DigestMismatch", "Saknar låsta revision-hashar (base_sha256/work_sha256).")

    with plans_obj._locks(site_id, plan_id):
        plan = plans_obj._row(site_id, plan_id)
        if plan is None:
            return _err("UnknownPlan", "Okänd plan.")
        blocked = plans_obj._gate(plan, base_sha256, work_sha256)
        if blocked is not None:
            return _err(
                str((blocked.data or {}).get("error_class") or "StalePlan"),
                blocked.message,
            )
        existing_dp = plan.get("draft_page")
        if isinstance(existing_dp, dict) and existing_dp.get("id") and not force:
            return _err(
                "DraftAlreadyPushed",
                f"Sidutkast #{existing_dp.get('id')} har redan skickats för den här planen.",
                draft_page=existing_dp,
            )
        site, missing = plans_obj._site(site_id)
        if missing is not None or site is None:
            return _err("UnknownSite", "Okänd webbplats.")
        try:
            root = workspace_root(plans_obj.data_dir, site_id, plan_id)
        except ReviewError as exc:
            return _err(exc.error_class, exc.error_class)

        draft = _load_page_draft(root / "work")
        if draft is None:
            return _err("DraftMissing", "Arbetsytan saknar work/page_draft.json.")

        title = " ".join(str(draft.get("title") or "").split()).strip()[:200]
        slug = str(draft.get("slug") or "").strip().strip("/").lower()
        raw_body = str(draft.get("body_html") or "")
        page_css = str(draft.get("page_css") or "")
        synthetic_ctx = (
            draft.get("synthetic_context") if isinstance(draft.get("synthetic_context"), dict) else {}
        )

        if not title or not raw_body.strip():
            return _err("InvalidDraft", "page_draft.json saknar titel eller innehåll.")
        if page_css.strip() and not is_safe_pure_css_file("page-draft.css", page_css):
            return _err("UnsafeCssRefused", "page_css innehåller otillåten CSS.")

        parsed_site = urlparse(str(site.canonical_url or ""))
        site_host = (parsed_site.hostname or "").strip().lower().rstrip(".")
        allowed_hosts = (
            (_allowed_site_hosts(site_host) | _STATIC_ASSET_HOSTS)
            if site_host
            else _STATIC_ASSET_HOSTS
        )
        sanitized_body = sanitize_page_draft_html(raw_body, allowed_hosts=allowed_hosts)
        if not sanitized_body.strip():
            return _err("InvalidDraft", "Sanerat sidutkast är tomt.")

        if any(
            contains_real_pii(field)
            for field in (title, raw_body, sanitized_body, page_css, synthetic_ctx)
        ):
            plans_obj._audit(actor, plan, "wp_review_push_draft", False, error_class="RealPiiRefused")
            return _err(
                "RealPiiRefused",
                "Sidutkastet innehåller riktig kunddata/PII och nekas innan publicering till WordPress.",
            )

        safe_css = re.sub(r"</style", r"<\/style", page_css.strip(), flags=re.IGNORECASE)
        style_block = f"<style>\n{safe_css}\n</style>\n" if safe_css else ""
        gutenberg_html = f"<!-- wp:html -->\n{style_block}{sanitized_body}\n<!-- /wp:html -->"

        use_mock = not rest_reads_allowed()
        live_calls = False
        if client is not None:
            wp_client = client
        else:
            try:
                if use_mock:
                    wp_client = wp_client_from_env(
                        base_url=str(site.canonical_url or "https://mock.local"),
                        use_mock=True,
                    )
                else:
                    from ecom_ops.integrations.wp_component_reads import wp_client_for_site

                    wp_client = wp_client_for_site(site, fallback_to_env=False)
                    if wp_client is None:
                        wp_client = wp_client_from_env(
                            base_url=str(site.canonical_url or "https://mock.local"),
                            use_mock=False,
                        )
                    live_calls = True
            except Exception as exc:
                plans_obj._audit(
                    actor, plan, "wp_review_push_draft", False, error_class="WpClientUnavailable"
                )
                return _err(
                    "WpClientUnavailable",
                    f"WordPress REST-klient kunde inte initieras: {type(exc).__name__}",
                )

        try:
            created = wp_client.create_page(
                title=title,
                content=gutenberg_html,
                slug=slug,
                status="draft",
            )
        except ValueError as exc:
            plans_obj._audit(
                actor, plan, "wp_review_push_draft", False, error_class="DraftPushRejected"
            )
            return _err("DraftPushRejected", str(exc))
        except Exception as exc:
            plans_obj._audit(
                actor, plan, "wp_review_push_draft", False, error_class="DraftPushFailed"
            )
            return _err(
                "DraftPushFailed",
                f"Kunde inte skapa utkast i WordPress ({type(exc).__name__}).",
            )

        draft_page = {
            "id": int(created.get("id") or 0),
            "slug": str(created.get("slug") or slug),
            "title": str(created.get("title") or title),
            "status": "draft",
            "link": str(created.get("link") or ""),
            "edit_url": str(created.get("edit_url") or ""),
            "pushed_at": plans_obj._now().isoformat(),
            "pushed_by": actor,
        }
        plan["draft_page"] = draft_page
        plans_obj._replace(plan)
        write_manifest(root, plan)
        from ecom_ops.wp_ops.change_ledger import safe_record_change

        safe_record_change(
            data_dir=plans_obj.data_dir,
            site_id=site_id,
            environment=site.environment,
            category="page_draft",
            transport="wp_rest",
            actor=actor,
            summary=f"Sidutkast '{draft_page['title']}' (slug={draft_page['slug']})",
            target_ref=f"page_draft:{draft_page['id']}",
            before_data={"exists": False, "id": draft_page["id"]},
            after_data=dict(draft_page),
            metadata={"plan_id": plan_id, "item_id": str(draft_page["id"])},
        )

    plans_obj._audit(
        actor,
        plan,
        "wp_review_push_draft",
        True,
        draft_page_id=draft_page["id"],
        slug=draft_page["slug"],
        draft_status="draft",
        live_calls=live_calls,
    )
    return {
        "ok": True,
        "plan_id": plan_id,
        "site_id": site_id,
        "status": "draft_pushed",
        "message": "Sidutkast skapat i WordPress (status='draft').",
        "draft_page": draft_page,
        "executed": True,
        "live_calls": live_calls,
    }


def request_production(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    *,
    actor: str,
    base_sha256: str,
    work_sha256: str,
) -> WpOpsResult:
    """Record a production review transition and refuse deploy fail-closed (BackupNotVerified)."""
    denied = plans_obj._need(
        actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_production"
    )
    if denied is not None:
        return denied
    with plans_obj._locks(site_id, plan_id):
        plan = plans_obj._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        blocked = plans_obj._gate(plan, base_sha256, work_sha256)
        if blocked is not None:
            return blocked
        if plan.get("risk") == "high":
            return _fail("HighRiskBlocked", "Hög risk går inte till produktion.", plan_id=plan_id)
        plan["status"] = "production_review"
        for name in _UNREACHABLE:
            plan.pop(name, None)
        plans_obj._replace(plan)
    block = production_deploy_block()
    plans_obj._audit(actor, plan, "wp_review_production", False, error_class="BackupNotVerified")
    return WpOpsResult(
        False,
        "BackupNotVerified",
        {**block, "status": "production_review", "plan_id": plan_id, "site_id": site_id},
    )


__all__ = [
    "approve_staging",
    "push_page_draft",
    "request_production",
]
