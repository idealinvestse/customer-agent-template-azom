"""Live overlay preview orchestration, baseline reuse, and artifact resolution."""

from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
from ecom_ops.wp_ops.file_paths import PathRejected, TransportError, resolve_file
from ecom_ops.wp_ops.models import WpOpsResult
from ecom_ops.wp_ops.review.preview.diff_overlay import apply_overlay
from ecom_ops.wp_ops.review.preview.shell import compose_standalone_page
from ecom_ops.wp_ops.review.store import (
    _ARTIFACT_PNG,
    _BASELINE_REUSE_TTL_S,
    _PNG_MAGIC,
    _fail,
    _ok,
    _resolve_parent_theme,
)
from ecom_ops.wp_ops.review.workspace_io import (
    PLAN_ID,
    ReviewError,
    iter_files,
    workspace_root,
    write_manifest,
)

_FACADE_MODULE = "ecom_ops.wp_ops.review_plan"


def _call_with_supported_kwargs(
    fn: Callable[..., Any],
    *args: Any,
    optional_kwargs: dict[str, Any] | None = None,
    **required_kwargs: Any,
) -> Any:
    """Invoke ``fn`` passing only those ``optional_kwargs`` accepted by its signature."""
    kwargs = dict(required_kwargs)
    if optional_kwargs:
        try:
            params = inspect.signature(fn).parameters
            accepts_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
            for key, value in optional_kwargs.items():
                if accepts_var_kw or key in params:
                    kwargs[key] = value
        except (TypeError, ValueError):
            pass
    return fn(*args, **kwargs)


def load_workspace_json(
    directory: Path,
    filename: str,
    *,
    max_bytes: int = 65536,
) -> dict[str, Any] | None:
    """Safely read one bounded JSON dict inside ``directory`` (refusing symlinks & escapes)."""
    raw_file = directory / filename
    if raw_file.is_symlink() or not raw_file.is_file():
        return None
    try:
        resolved_dir = directory.resolve()
        resolved_file = raw_file.resolve()
        if resolved_file.parent != resolved_dir or resolved_file.is_symlink():
            return None
        raw = resolved_file.read_bytes()
        if len(raw) > max_bytes or b"\x00" in raw:
            return None
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _load_product_edits(work_dir: Path) -> dict[str, Any] | None:
    return load_workspace_json(work_dir, "product_edits.json")


def _load_page_draft(work_dir: Path) -> dict[str, Any] | None:
    return load_workspace_json(work_dir, "page_draft.json")


def collect_extra_css_files(
    work_dir: Path,
    base_dir: Path,
    *,
    include_style_css: bool = False,
) -> list[tuple[str, str]]:
    """Collect modified or newly created ``.css`` files in ``work/`` (bounded to 10 files / 128KB)."""
    out: list[tuple[str, str]] = []
    total_bytes = 0
    for rel, after_bytes in iter_files(work_dir):
        if not rel.lower().endswith(".css"):
            continue
        if not include_style_css and rel == "style.css":
            continue
        base_file = base_dir / rel
        if base_file.is_file() and not base_file.is_symlink():
            try:
                if base_file.read_bytes() == after_bytes:
                    continue
            except OSError:
                pass
        if len(after_bytes) > 65536 or b"\x00" in after_bytes:
            continue
        if total_bytes + len(after_bytes) > 131072 or len(out) >= 10:
            break
        total_bytes += len(after_bytes)
        out.append((rel, after_bytes.decode("utf-8", errors="replace")))
    return out


def safe_preview_html_path(data_dir: Path, site_id: str, plan_id: str) -> Path | None:
    """Resolve ``artifacts/preview.html`` strictly inside the plan workspace (refusing symlinks)."""
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return None
    if root.is_symlink() or not root.is_dir():
        return None
    raw_art = root / "artifacts"
    if raw_art.is_symlink() or not raw_art.is_dir():
        return None
    art_dir = raw_art.resolve()
    if art_dir.parent != root.resolve():
        return None
    raw_file = art_dir / "preview.html"
    if raw_file.is_symlink():
        return None
    candidate = raw_file.resolve()
    if candidate.parent != art_dir or candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        if candidate.stat().st_size > 5 * 1024 * 1024:
            return None
    except OSError:
        return None
    return candidate


def resolve_safe_preview_html_path(data_dir: Path, site_id: str, plan_id: str) -> Path | None:
    """Lookup ``_safe_preview_html_path`` through ``review_plan`` facade so spies/patches work."""
    facade = sys.modules.get(_FACADE_MODULE)
    if facade is not None:
        fn = getattr(facade, "_safe_preview_html_path", None)
        if callable(fn):
            return fn(data_dir, site_id, plan_id)
    return safe_preview_html_path(data_dir, site_id, plan_id)


def list_artifact_pngs(artifact_dir: Path) -> list[str]:
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        return []
    resolved_dir = artifact_dir.resolve()
    names: list[str] = []
    try:
        entries = list(artifact_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not _ARTIFACT_PNG.fullmatch(entry.name) or entry.is_symlink():
            continue
        resolved = entry.resolve()
        if resolved.parent != resolved_dir or resolved.is_symlink() or not resolved.is_file():
            continue
        try:
            with resolved.open("rb") as fh:
                if fh.read(len(_PNG_MAGIC)) == _PNG_MAGIC:
                    names.append(entry.name)
        except OSError:
            continue
    return sorted(names)


def read_preview_meta(data_dir: Path, site_id: str, plan_id: str) -> dict[str, Any]:
    """Read bounded non-secret preview metadata from ``artifacts/preview.json``."""
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return {}
    parsed = load_workspace_json(root / "artifacts", "preview.json", max_bytes=512 * 1024)
    if parsed is None:
        return {}
    preview_kind = str(parsed.get("preview_kind") or "")[:40]
    synthetic_data = parsed.get("synthetic_data") is True or preview_kind == "standalone_page"
    return {
        "preview_kind": preview_kind,
        "path": str(parsed.get("path") or "")[:200],
        "focus_target": str(parsed.get("focus_target") or "")[:160],
        "focus_matched": parsed.get("focus_matched") is True,
        "synthetic_data": synthetic_data,
    }


def read_page_draft_meta(data_dir: Path, site_id: str, plan_id: str) -> dict[str, Any]:
    """Read bounded summary metadata from ``work/page_draft.json``."""
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return {}
    parsed = load_workspace_json(root / "work", "page_draft.json", max_bytes=256 * 1024)
    if parsed is None:
        return {}
    synth_ctx = parsed.get("synthetic_context")
    synth_flag = parsed.get("synthetic_data")
    if synth_flag is None and isinstance(synth_ctx, dict):
        synth_flag = synth_ctx.get("synthetic_data")
    shell_raw = str(parsed.get("shell_path") or "/").strip() or "/"
    return {
        "slug": str(parsed.get("slug") or "").strip()[:120],
        "title": str(parsed.get("title") or "").strip()[:160],
        "page_type": str(parsed.get("page_type") or "").strip()[:60],
        "shell_path": shell_raw[:200],
        "template_file": str(parsed.get("template_file") or "").strip()[:160],
        "synthetic_data": synth_flag is not False,
    }


def reusable_live_baseline_dir(
    plans_obj: Any,
    site_id: str,
    plan: dict[str, Any],
    *,
    clean_path: str,
    full_page: bool,
) -> Path | None:
    """Find a recent artifact dir (same plan or same variant_group) with matching live-*.png."""
    now_ts = plans_obj._now().timestamp()
    candidates: list[str] = [str(plan.get("plan_id") or "")]
    vg = str(plan.get("variant_group") or "").strip()
    if vg:
        for row in plans_obj._load():
            if (
                row.get("site_id") == site_id
                and str(row.get("variant_group") or "") == vg
                and row.get("plan_id") not in candidates
            ):
                candidates.append(str(row.get("plan_id") or ""))
    for cand_id in candidates:
        if not PLAN_ID.fullmatch(cand_id):
            continue
        try:
            art = workspace_root(plans_obj.data_dir, site_id, cand_id) / "artifacts"
        except ReviewError:
            continue
        pv_json = art / "preview.json"
        ld = art / "live-desktop.png"
        lm = art / "live-mobile.png"
        if (
            pv_json.is_symlink()
            or not pv_json.is_file()
            or ld.is_symlink()
            or not ld.is_file()
            or lm.is_symlink()
            or not lm.is_file()
        ):
            continue
        try:
            age = now_ts - ld.stat().st_mtime
            if age < -5.0 or age > _BASELINE_REUSE_TTL_S:
                continue
            meta = json.loads(pv_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if (
            isinstance(meta, dict)
            and str(meta.get("path") or "") == clean_path
            and bool(meta.get("full_page")) is bool(full_page)
        ):
            return art
    return None


def _collect_work_changes(
    plans_obj: Any,
    site: Any,
    plan: dict[str, Any],
    root: Path,
) -> list[tuple[str, str, str]]:
    work_dir = root / "work"
    base_dir = root / "base"
    origin_dir = root / "origin"
    parent_slug: str | None = None
    parent_checked = False
    transport: Any = None
    changes: list[tuple[str, str, str]] = []
    for rel, after_bytes in iter_files(work_dir):
        base_file = base_dir / rel
        origin_file = origin_dir / rel
        if base_file.is_file() and not base_file.is_symlink():
            before_bytes = base_file.read_bytes()
        elif origin_file.is_file() and not origin_file.is_symlink():
            before_bytes = origin_file.read_bytes()
        elif plan.get("kind") == "theme" and site.files_root:
            if not parent_checked:
                parent_checked = True
                parent_slug = _resolve_parent_theme(root, str(plan.get("slug") or ""), None)
                if parent_slug:
                    transport = plans_obj._transport(site)
            if not parent_slug or transport is None:
                continue
            try:
                abs_path = resolve_file(site.files_root, "theme", parent_slug, rel)
                raw_parent = transport.read_bytes(abs_path)
            except (PathRejected, TransportError, OSError):
                continue
            if len(raw_parent) > 65536 or b"\x00" in raw_parent:
                continue
            before_bytes = raw_parent
        else:
            continue
        if before_bytes == after_bytes:
            continue
        changes.append(
            (
                rel,
                before_bytes.decode("utf-8", errors="replace"),
                after_bytes.decode("utf-8", errors="replace"),
            )
        )
    return changes


def build_overlay_html(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    *,
    actor: str,
    path: str = "/",
    use_cache: bool = True,
) -> tuple[dict[str, Any] | None, WpOpsResult | None]:
    """Build in-memory overlay HTML for an editable or superseded workspace without writing prod."""
    denied = plans_obj._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_live_preview")
    if denied is not None:
        return None, denied

    from ecom_ops.integrations.wp_live_preview import (
        _STATIC_ASSET_HOSTS,
        _allowed_site_hosts,
        fetch_live_html,
        live_preview_enabled,
        validate_preview_path,
    )

    if not live_preview_enabled():
        return None, _fail("LivePreviewDisabled", "Live-förhandsvisning är inte aktiverad.")
    plan = plans_obj._row(site_id, plan_id)
    if plan is None:
        return None, _fail("UnknownPlan", "Okänd plan.")
    blocked = plans_obj._fresh(plan, allow_superseded_read=True)
    if blocked is not None:
        return None, blocked
    site, missing = plans_obj._site(site_id)
    if missing is not None or site is None:
        return None, (missing if missing is not None else _fail("UnknownSite", "Okänd webbplats."))

    fetch_opt = {"use_cache": True} if use_cache else None
    try:
        root = workspace_root(plans_obj.data_dir, site_id, plan_id)
        work_dir = root / "work"
        base_dir = root / "base"
        product_edits = _load_product_edits(work_dir)
        page_draft = _load_page_draft(work_dir)
        raw_path_clean = "" if path is None else str(path).strip()

        use_standalone = False
        if page_draft is not None:
            draft_slug = str(page_draft.get("slug") or "").strip("/")
            draft_shell = str(page_draft.get("shell_path") or "/").strip() or "/"
            draft_candidates = {"", "/", draft_shell, f"/{draft_slug}", f"/{draft_slug}/"}
            if raw_path_clean in draft_candidates:
                use_standalone = True

        if use_standalone and page_draft is not None:
            shell_path = validate_preview_path(str(page_draft.get("shell_path") or "/"))
            fetched = _call_with_supported_kwargs(
                fetch_live_html, site, shell_path, optional_kwargs=fetch_opt
            )
            extra_css = collect_extra_css_files(work_dir, base_dir, include_style_css=True)
            site_host = (urlparse(str(site.canonical_url or "")).hostname or "").lower()
            allowed_hosts = _allowed_site_hosts(site_host) | _STATIC_ASSET_HOSTS
            overlay = compose_standalone_page(
                str(fetched.get("html") or ""),
                title=str(page_draft.get("title") or "Ny sida"),
                body_html=str(page_draft.get("body_html") or ""),
                page_css=str(page_draft.get("page_css") or ""),
                extra_css_files=extra_css,
                allowed_hosts=allowed_hosts,
                synthetic_banner="Förhandsvisning av ny sida i sandlåda med syntetisk exempeldata (inga ändringar på live)",
            )
            return (
                {
                    "site": site,
                    "plan": plan,
                    "root": root,
                    "clean_path": shell_path,
                    "fetched": fetched,
                    "overlay": overlay,
                    "mode": "standalone_page",
                    "page_draft": page_draft,
                    "shell_path": shell_path,
                },
                None,
            )

        effective_path = path
        if effective_path is None or str(effective_path).strip() in {"", "/"}:
            if (
                product_edits
                and isinstance(product_edits.get("product_path"), str)
                and str(product_edits.get("product_path")).strip()
            ):
                effective_path = str(product_edits["product_path"]).strip()
            elif isinstance(plan.get("preview_path"), str) and str(plan.get("preview_path")).strip():
                effective_path = str(plan["preview_path"]).strip()
        clean_path = validate_preview_path(effective_path)
        fetched = _call_with_supported_kwargs(
            fetch_live_html, site, clean_path, optional_kwargs=fetch_opt
        )
    except ValueError:
        return None, _fail("HtmlTooLarge", "Sidan överskrider storleksgränsen.")
    except ReviewError as exc:
        return None, _fail(exc.error_class, exc.error_class)

    changes = _collect_work_changes(plans_obj, site, plan, root)
    child_css: str | None = None
    css_work = work_dir / "style.css"
    css_base = base_dir / "style.css"
    if css_work.is_file() and not css_work.is_symlink():
        css_text = css_work.read_text(encoding="utf-8", errors="replace")
        base_css_text = (
            css_base.read_text(encoding="utf-8", errors="replace")
            if css_base.is_file() and not css_base.is_symlink()
            else ""
        )
        if plan.get("theme_relationship") in {"no_child", "child"} and (
            css_text != base_css_text or len(base_css_text) < 200
        ):
            child_css = css_text

    extra_css_files = collect_extra_css_files(work_dir, base_dir, include_style_css=False)
    try:
        overlay = apply_overlay(
            str(fetched.get("html") or ""),
            changes,
            child_css=child_css,
            extra_css_files=extra_css_files,
            product_edits=product_edits,
        )
    except ValueError:
        return None, _fail("HtmlTooLarge", "Sidan överskrider storleksgränsen.")

    return (
        {
            "site": site,
            "plan": plan,
            "root": root,
            "clean_path": clean_path,
            "fetched": fetched,
            "overlay": overlay,
            "mode": "live_overlay",
            "page_draft": None,
        },
        None,
    )


def _prepare_artifact_dir_and_baseline(
    plans_obj: Any,
    site_id: str,
    plan: dict[str, Any],
    artifact_dir: Path,
    *,
    clean_path: str,
    clean_full_page: bool,
    reuse_live_baseline: bool,
    is_standalone: bool,
) -> bool:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    reused_baseline = False
    baseline_dir: Path | None = None
    if reuse_live_baseline and not is_standalone:
        baseline_dir = plans_obj._reusable_live_baseline_dir(
            site_id,
            plan,
            clean_path=clean_path,
            full_page=clean_full_page,
        )
    saved_live_bytes: dict[str, bytes] = {}
    if baseline_dir is not None:
        try:
            ld_bytes = (baseline_dir / "live-desktop.png").read_bytes()
            lm_bytes = (baseline_dir / "live-mobile.png").read_bytes()
            if ld_bytes[:8] == _PNG_MAGIC and lm_bytes[:8] == _PNG_MAGIC:
                saved_live_bytes["live-desktop.png"] = ld_bytes
                saved_live_bytes["live-mobile.png"] = lm_bytes
                reused_baseline = True
        except OSError:
            saved_live_bytes = {}
            reused_baseline = False

    for stale in list(artifact_dir.iterdir()):
        if (
            stale.name.startswith(("live-", "preview-", "change-", "focus-", "compare-"))
            and stale.suffix.lower() == ".png"
        ):
            try:
                if stale.is_file() or stale.is_symlink():
                    stale.unlink()
            except OSError:
                pass

    if reused_baseline:
        for name, raw_png in saved_live_bytes.items():
            (artifact_dir / name).write_bytes(raw_png)
    return reused_baseline


def _write_standalone_meta(artifact_dir: Path, draft_summary: dict[str, Any]) -> None:
    pv_json_path = artifact_dir / "preview.json"
    try:
        existing_meta = (
            json.loads(pv_json_path.read_text(encoding="utf-8"))
            if pv_json_path.is_file() and not pv_json_path.is_symlink()
            else {}
        )
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        existing_meta["preview_kind"] = "standalone_page"
        existing_meta["synthetic_data"] = True
        existing_meta["page_draft"] = draft_summary
        pv_json_path.write_text(
            json.dumps(existing_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, UnicodeError):
        pass


def _maybe_build_before_after_composite(artifact_dir: Path, plan_id: str) -> None:
    live_shot = artifact_dir / "live-desktop.png"
    after_shot: Path | None = None
    for cand_name in ("preview-desktop.png", "focus-desktop.png", "change-desktop.png"):
        cand_path = artifact_dir / cand_name
        if cand_path.is_file() and not cand_path.is_symlink():
            after_shot = cand_path
            break
    if live_shot.is_file() and not live_shot.is_symlink() and after_shot is not None:
        try:
            from ecom_ops.integrations.wp_live_preview import build_comparison_composite

            build_comparison_composite(
                [("Före (Live)", live_shot), (f"Efter ({plan_id})", after_shot)],
                artifact_dir / "compare-desktop.png",
            )
        except Exception:
            pass


def live_preview(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    *,
    actor: str,
    path: str = "/",
    focus_section: str | None = None,
    focus_selector: str | None = None,
    focus_text: str | None = None,
    full_page: bool = False,
    reuse_live_baseline: bool = True,
) -> WpOpsResult:
    """Render a read-only live HTML overlay preview for a workspace without writing prod."""
    from ecom_ops.bot.dashboard_links import sandbox_preview_url
    from ecom_ops.integrations.wp_live_preview import (
        build_interactive_sandbox_html,
        capture_preview,
        validate_focus_options,
    )

    try:
        clean_section, clean_selector, clean_text, clean_full_page = validate_focus_options(
            focus_section=focus_section,
            focus_selector=focus_selector,
            focus_text=focus_text,
            full_page=full_page,
        )
    except ReviewError as exc:
        return _fail(exc.error_class, exc.error_class)

    built, err = plans_obj.build_overlay_html(
        site_id,
        plan_id,
        actor=actor,
        path=path,
        use_cache=bool(reuse_live_baseline),
    )
    if err is not None or built is None:
        return err if err is not None else _fail("LivePreviewFailed", "Förhandsvisningen misslyckades.")

    site = built["site"]
    plan = built["plan"]
    root = built["root"]
    clean_path = built["clean_path"]
    fetched = built["fetched"]
    overlay = built["overlay"]
    is_standalone = built.get("mode") == "standalone_page"
    page_draft = (
        built.get("page_draft")
        if is_standalone and isinstance(built.get("page_draft"), dict)
        else None
    )
    draft_summary: dict[str, Any] | None = None
    if page_draft is not None:
        draft_summary = {
            "slug": str(page_draft.get("slug") or ""),
            "title": str(page_draft.get("title") or ""),
            "page_type": str(page_draft.get("page_type") or "custom"),
            "shell_path": str(page_draft.get("shell_path") or "/"),
            "template_file": str(page_draft.get("template_file") or ""),
            "synthetic_data": True,
        }
        if not clean_selector and isinstance(page_draft.get("focus_selector"), str):
            raw_ds = page_draft["focus_selector"].strip()
            if raw_ds:
                try:
                    _, clean_selector, _, _ = validate_focus_options(focus_selector=raw_ds)
                except ReviewError:
                    pass

    artifact_dir = root / "artifacts"
    reused_baseline = _prepare_artifact_dir_and_baseline(
        plans_obj,
        site_id,
        plan,
        artifact_dir,
        clean_path=clean_path,
        clean_full_page=clean_full_page,
        reuse_live_baseline=reuse_live_baseline,
        is_standalone=is_standalone,
    )

    revisions_arg: list[tuple[str, str]] = (
        [("preview", overlay.html)]
        if (reused_baseline or is_standalone)
        else [("live", str(fetched.get("html") or "")), ("preview", overlay.html)]
    )
    capture_optional: dict[str, Any] = {}
    if is_standalone:
        capture_optional["preview_kind_override"] = "standalone_page"
        if draft_summary is not None:
            capture_optional["extra_preview_meta"] = {
                "synthetic_data": True,
                "page_draft": draft_summary,
            }
    elif reused_baseline:
        capture_optional["preview_kind_override"] = "live_overlay"

    try:
        rendered = _call_with_supported_kwargs(
            capture_preview,
            site,
            optional_kwargs=capture_optional,
            path=clean_path,
            artifact_dir=artifact_dir,
            revisions=revisions_arg,
            highlight_token=overlay.highlight_token,
            focus_section=clean_section,
            focus_selector=clean_selector,
            focus_text=clean_text,
            full_page=clean_full_page,
            target_url=str(fetched.get("final_url") or "") or None,
        )
    except ReviewError as exc:
        return _fail(exc.error_class, exc.error_class)

    if is_standalone and draft_summary is not None:
        _write_standalone_meta(artifact_dir, draft_summary)
    if not is_standalone:
        _maybe_build_before_after_composite(artifact_dir, plan_id)

    preferred_order = (
        (
            "compare-desktop.png",
            "preview-desktop.png",
            "preview-mobile.png",
            "focus-desktop.png",
            "focus-mobile.png",
            "change-desktop.png",
            "change-mobile.png",
        )
        if is_standalone
        else (
            "compare-desktop.png",
            "change-desktop.png",
            "change-mobile.png",
            "focus-desktop.png",
            "focus-mobile.png",
            "live-desktop.png",
            "preview-desktop.png",
            "live-mobile.png",
            "preview-mobile.png",
        )
    )
    available = set(list_artifact_pngs(artifact_dir))
    ordered_shots = [name for name in preferred_order if name in available]
    counts = {
        kind_name: sum(1 for h in overlay.hunks if h.kind == kind_name)
        for kind_name in ("applied", "unmatched", "php_skipped", "insert_unanchored")
    }
    secrets_list: list[str] = []
    try:
        bound_cred = bind_credential(site)
        if bound_cred.present and bound_cred.value:
            secrets_list.append(bound_cred.value)
    except Exception:
        pass

    display_path = (
        f"/{draft_summary['slug'].strip('/')}/"
        if (is_standalone and draft_summary and draft_summary.get("slug"))
        else clean_path
    )
    sb_optional = {"standalone_page": True, "synthetic_data": True} if is_standalone else None
    interactive_html = _call_with_supported_kwargs(
        build_interactive_sandbox_html,
        overlay.html,
        optional_kwargs=sb_optional,
        site_canonical_url=str(site.canonical_url or ""),
        site_id=site_id,
        plan_id=plan_id,
        path=display_path,
        highlight_token=overlay.highlight_token,
        focus_text=clean_text,
        focus_selector=clean_selector,
    )
    if secrets_list:
        scrubbed_html = scrub_secrets(interactive_html, secrets_list)
        if isinstance(scrubbed_html, str):
            interactive_html = scrubbed_html
    preview_html_path = artifact_dir / "preview.html"
    if preview_html_path.is_symlink():
        preview_html_path.unlink()
    preview_html_path.write_text(interactive_html, encoding="utf-8")

    if display_path or clean_selector or (is_standalone and draft_summary is not None):
        with plans_obj._locks(site_id, plan_id):
            fresh = plans_obj._row(site_id, plan_id)
            if fresh is not None and plans_obj._fresh(fresh, allow_superseded_read=True) is None:
                meta_changed = False
                if display_path and str(fresh.get("preview_path") or "") != display_path:
                    fresh["preview_path"] = display_path
                    meta_changed = True
                if clean_selector and str(fresh.get("focus_selector") or "") != clean_selector:
                    fresh["focus_selector"] = clean_selector
                    meta_changed = True
                if is_standalone and draft_summary is not None and fresh.get("page_draft") != draft_summary:
                    fresh["page_draft"] = draft_summary
                    meta_changed = True
                if meta_changed:
                    plans_obj._replace(fresh)
                    write_manifest(root, fresh)
                plan = fresh

    preview_route_path = f"/wp/{site_id}/reviews/{plan_id}/preview"
    preview_public_url = sandbox_preview_url(site_id, plan_id)
    hunk_excerpts = [
        scrub_secrets(
            {
                "path": h.path,
                "kind": h.kind,
                "old_excerpt": h.old_excerpt[:160],
                "new_excerpt": h.new_excerpt[:160],
            },
            secrets_list,
        )
        for h in overlay.hunks[:5]
    ]
    safe_token = scrub_secrets(overlay.highlight_token, secrets_list)
    safe_blocked = scrub_secrets(rendered.get("blocked_counts") or {}, secrets_list)
    safe_focus_target = scrub_secrets(rendered.get("focus_target"), secrets_list)
    raw_vh = rendered.get("visual_health")
    safe_visual_health = scrub_secrets(raw_vh, secrets_list) if isinstance(raw_vh, dict) else None
    focus_matched = bool(rendered.get("focus_matched"))

    plans_obj._audit(
        actor,
        plan,
        "wp_review_live_preview",
        True,
        live_calls=True,
        writes=False,
        path=display_path,
        applied=counts["applied"],
        unmatched=counts["unmatched"],
        php_skipped=counts["php_skipped"],
        css_bytes=overlay.injected_css_bytes,
        screenshots=len(ordered_shots),
        focus_matched=focus_matched,
        full_page=clean_full_page,
        preview_html_ready=True,
        reused_baseline=reused_baseline,
        standalone_page=is_standalone,
    )
    return _ok(
        "Förhandsvisning av ny sida skapad i sandlådan."
        if is_standalone
        else "Live-förhandsvisning skapad.",
        plan_id=plan_id,
        site_id=site_id,
        risk=str(plan.get("risk") or "low"),
        path=display_path,
        preview_path=preview_route_path,
        preview_url=preview_public_url,
        preview_html_ready=True,
        reused_live_baseline=reused_baseline,
        screenshots=ordered_shots,
        screenshot_paths=[str(artifact_dir / name) for name in ordered_shots],
        focus_matched=focus_matched,
        focus_target=safe_focus_target,
        full_page=clean_full_page,
        visual_health=safe_visual_health,
        synthetic_data=is_standalone,
        page_draft=draft_summary,
        overlay={
            "counts": counts,
            "injected_css_bytes": overlay.injected_css_bytes,
            "highlight_token": safe_token,
            "hunks": hunk_excerpts,
        },
        blocked_counts=safe_blocked,
        preview_kind="standalone_page" if is_standalone else "live_overlay",
        live_calls=True,
        note=(
            "Sandlådeförhandsvisning av ny sida inbäddad i sajtens riktiga skal (header/footer/CSS) "
            "med syntetisk exempeldata — inga ändringar på staging eller live."
            if is_standalone
            else "Förhandsvisning = live-HTML från sajten (GET, utan cookies) + statiska "
            "mallfragment/CSS från arbetsytan. PHP-logik körs inte."
        ),
    )


def build_variant_comparison(
    data_dir: Path,
    site_id: str,
    plan_ids: list[str] | tuple[str, ...],
) -> Path | None:
    """Build a side-by-side ``compare-desktop.png`` across 2–4 previewed variants."""
    from ecom_ops.integrations.wp_live_preview import build_comparison_composite

    unique_ids: list[str] = []
    for pid in plan_ids:
        clean_pid = str(pid or "").strip()
        if clean_pid and PLAN_ID.fullmatch(clean_pid) and clean_pid not in unique_ids:
            unique_ids.append(clean_pid)
    if len(unique_ids) < 2:
        return None

    panels: list[tuple[str, Path]] = []
    latest_artifact_dir: Path | None = None
    for idx, pid in enumerate(unique_ids[:4], start=1):
        try:
            root = workspace_root(data_dir, site_id, pid)
        except ReviewError:
            continue
        art_dir = root / "artifacts"
        if not art_dir.is_dir():
            continue
        chosen: Path | None = None
        for cand in ("focus-desktop.png", "change-desktop.png", "preview-desktop.png", "work-desktop.png"):
            p = art_dir / cand
            if p.is_file() and not p.is_symlink():
                chosen = p
                break
        if chosen is not None:
            panels.append((f"Förslag {idx} ({pid})", chosen))
            latest_artifact_dir = art_dir
    if len(panels) < 2 or latest_artifact_dir is None:
        return None
    return build_comparison_composite(panels, latest_artifact_dir / "compare-desktop.png")


def artifact_file(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    name: str,
    *,
    actor: str,
) -> tuple[Path | None, WpOpsResult | None]:
    denied = plans_obj._need(
        actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_artifact"
    )
    if denied is not None:
        return None, denied
    if not PLAN_ID.fullmatch(plan_id) or not _ARTIFACT_PNG.fullmatch(name):
        return None, _fail("ArtifactNotFound", "Okänd artefakt.")
    plan = plans_obj._row(site_id, plan_id)
    if plan is None:
        return None, _fail("UnknownPlan", "Okänd plan.")
    try:
        art_dir = (workspace_root(plans_obj.data_dir, site_id, plan_id) / "artifacts").resolve()
    except ReviewError:
        return None, _fail("ArtifactNotFound", "Okänd artefakt.")
    raw_candidate = art_dir / name
    if raw_candidate.is_symlink():
        return None, _fail("ArtifactNotFound", "Okänd artefakt.")
    candidate = raw_candidate.resolve()
    if candidate.parent != art_dir or candidate.is_symlink() or not candidate.is_file():
        return None, _fail("ArtifactNotFound", "Okänd artefakt.")
    try:
        with candidate.open("rb") as fh:
            magic = fh.read(len(_PNG_MAGIC))
    except OSError:
        return None, _fail("ArtifactNotFound", "Okänd artefakt.")
    if magic != _PNG_MAGIC:
        return None, _fail("ArtifactNotFound", "Okänd artefakt.")
    return candidate, None


__all__ = [
    "_call_with_supported_kwargs",
    "_load_page_draft",
    "_load_product_edits",
    "artifact_file",
    "build_overlay_html",
    "build_variant_comparison",
    "collect_extra_css_files",
    "list_artifact_pngs",
    "live_preview",
    "load_workspace_json",
    "read_page_draft_meta",
    "read_preview_meta",
    "resolve_safe_preview_html_path",
    "reusable_live_baseline_dir",
    "safe_preview_html_path",
]
