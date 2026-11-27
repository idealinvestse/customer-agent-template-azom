"""Pure-Python static security and quality analyzer for WordPress/WooCommerce code.

Works offline with AZOM_WP_SANDBOX=0 and never invokes PHP, shell, or network IO.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ecom_ops.wp_ops.credentials import scrub_compact
from ecom_ops.wp_ops.source_scan import (
    iter_source_files,
    line_number,
    read_call_args,
    strip_comments_and_find_strings,
)

_PHP_EXTENSIONS = frozenset({".php", ".phtml", ".inc"})
_TEMPLATE_PREFIXES = ("templates/", "woocommerce/", "template-parts/", "parts/")

_SUPERGLOBAL_RE = re.compile(r"\$_(?:GET|POST|REQUEST|COOKIE|SERVER)\b")
_OUTPUT_STMT_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_>-])\b(?:echo|print|printf)\b|<\?=)"
)
_SAFE_CALL_RE = re.compile(
    r"(?:esc_(?:html|attr|url|textarea|js|xml)(?:_e|_x)?|absint|intval|floatval|"
    r"wp_kses(?:_post|_data)?|wp_json_encode|sanitize_(?:text_field|key|email|title)|"
    r"number_format|isset|empty)\s*\("
)
_SAFE_CAST_RE = re.compile(r"\(\s*(?:int|integer|float|double|bool|boolean)\s*\)\s*$")

_WPDB_CALL_RE = re.compile(
    r"\$wpdb\s*->\s*(query|get_results|get_var|get_row|get_col)\s*\("
)
_SQL_VAR_ASSIGN_RE = re.compile(
    r"(\$[A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);"
)
_UNSAFE_INTERP_RE = re.compile(
    r"(?:"
    r"\"[^\"]*(?:\{\$?(?!wpdb->)[A-Za-z_][A-Za-z0-9_]*|\$(?!wpdb->)[A-Za-z_][A-Za-z0-9_]*)[^\"]*\""
    r"|\.\s*\$"
    r"|\$\s*\.\s*"
    r")"
)

_DANGEROUS_EXEC_RE = re.compile(
    r"(?<![A-Za-z0-9_>-])\b"
    r"(eval|assert|shell_exec|exec|system|passthru|proc_open|popen|unserialize)\s*\("
)
_ALLOWED_CLASSES_FALSE_RE = re.compile(
    r"allowed_classes['\"]?\s*=>\s*false\b", re.I
)

_WP_AJAX_RE = re.compile(r"""['"]wp_ajax_(nopriv_)?([A-Za-z0-9_-]+)['"]""")
_POST_VAR_RE = re.compile(r"\$_POST\b")
_NONCE_CHECK_RE = re.compile(
    r"\b(?:wp_verify_nonce|check_ajax_referer|check_admin_referer)\s*\("
)
_CAPABILITY_CHECK_RE = re.compile(r"\bcurrent_user_can\s*\(")
_ABSPATH_RE = re.compile(r"\b(?:ABSPATH|WP_UNINSTALL_PLUGIN|WPINC)\b")
_MU_LIFECYCLE_HOOK_RE = re.compile(
    r"(?<![A-Za-z0-9_>-])\b(register_(?:activation|deactivation|uninstall)_hook)\s*\("
)
_WOO_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_>-])\b(WC|wc_get_order|wc_get_orders|wc_get_product|wc_get_products|"
    r"wc_add_notice|wc_get_cart_url)\s*\("
)
_WOO_GUARD_RE = re.compile(
    r"(?:class_exists\s*\(\s*['\"]\\?WooCommerce['\"]|"
    r"function_exists\s*\(\s*['\"](?:WC|wc_get_order|wc_get_orders|wc_get_product|wc_get_products|wc_add_notice|wc_get_cart_url)['\"]|"
    r"['\"](?:woocommerce_loaded|before_woocommerce_init|woocommerce_init)['\"]|"
    r"Requires\s+Plugins\s*:[^\r\n]*\bwoocommerce\b)",
    re.I,
)
_WOO_ORDER_TOUCH_RE = re.compile(
    r"(?<![A-Za-z0-9_>-])\b(?:wc_get_order|wc_get_orders|WC_Order|woocommerce_order_|woocommerce_new_order)\b"
)
_HPOS_DECL_RE = re.compile(r"declare_compatibility\s*\([^)]*custom_order_tables", re.S)

_DEPRECATED_WOO_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?<![A-Za-z0-9_>-])\bwoocommerce_get_template\s*\("),
        "Deprecated WooCommerce function woocommerce_get_template(); use wc_get_template().",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_>-])\bwoocommerce_get_template_part\s*\("),
        "Deprecated WooCommerce function woocommerce_get_template_part(); use wc_get_template_part().",
    ),
    (
        re.compile(r"->\s*get_cart_url\s*\("),
        "Deprecated WooCommerce method get_cart_url(); use wc_get_cart_url().",
    ),
    (
        re.compile(r"->\s*get_checkout_url\s*\("),
        "Deprecated WooCommerce method get_checkout_url(); use wc_get_checkout_url().",
    ),
    (
        re.compile(r"->\s*get_gallery_attachment_ids\s*\("),
        "Deprecated WooCommerce method get_gallery_attachment_ids(); use get_gallery_image_ids().",
    ),
    (
        re.compile(r"->\s*get_related\s*\("),
        "Deprecated WooCommerce method get_related(); use wc_get_related_products().",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_>-])\bwoocommerce_price\s*\("),
        "Deprecated WooCommerce function woocommerce_price(); use wc_price().",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_>-])\bwoocommerce_clean\s*\("),
        "Deprecated WooCommerce function woocommerce_clean(); use wc_clean().",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_>-])\bwoocommerce_(?:add|update)_order_item_meta\s*\("),
        "Deprecated WooCommerce order item meta helper; use wc_add_order_item_meta() / CRUD.",
    ),
)

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_SECRET_LINE_HINT_RE = re.compile(
    r"(?i)\b(?:secret|password|passwd|passphrase|token|api[_-]?key|private[_-]?key|"
    r"consumer[_-]?(?:key|secret)|client[_-]?secret|access[_-]?key|signing[_-]?key)\b"
)


def _scrub_excerpt(text: str) -> str:
    cleaned = scrub_compact(text, max_len=120)
    if _SECRET_LINE_HINT_RE.search(cleaned) and ("=" in cleaned or ":" in cleaned):
        cleaned = re.sub(r"(['\"])(?:\\.|(?!\1).){4,}\1", r"\1***\1", cleaned)
    if len(cleaned) > 120:
        return cleaned[:117] + "..."
    return cleaned


def _excerpt_at(raw_lines: list[str], line_no: int) -> str:
    if 1 <= line_no <= len(raw_lines):
        return _scrub_excerpt(raw_lines[line_no - 1])
    return ""


def _finding(
    severity: str,
    rule: str,
    path: str,
    line: int,
    message: str,
    excerpt: str,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "rule": rule,
        "path": path,
        "line": max(1, int(line)),
        "message": message,
        "excerpt": _scrub_excerpt(excerpt),
    }


def _has_enclosing_safe_wrapper(prefix: str) -> bool:
    if _SAFE_CAST_RE.search(prefix):
        return True
    for match in _SAFE_CALL_RE.finditer(prefix):
        tail = prefix[match.end() - 1 :]
        depth = 0
        for ch in tail:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth <= 0:
                    break
        if depth > 0:
            return True
    return False


def _check_unescaped_superglobals(
    path: str,
    stripped: str,
    raw_lines: list[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen_lines: set[int] = set()
    for sg_match in _SUPERGLOBAL_RE.finditer(stripped):
        pos = sg_match.start()
        line_no = line_number(stripped, pos)
        if line_no in seen_lines:
            continue
        stmt_start = max(
            stripped.rfind(";", 0, pos) + 1,
            stripped.rfind("<?php", 0, pos),
            stripped.rfind("<?=", 0, pos),
        )
        prefix = stripped[stmt_start:pos]
        if not _OUTPUT_STMT_RE.search(prefix):
            continue
        if _has_enclosing_safe_wrapper(prefix):
            continue
        seen_lines.add(line_no)
        findings.append(
            _finding(
                "high",
                "unescaped_superglobal",
                path,
                line_no,
                f"Outputting {sg_match.group(0)} without escaping/sanitizing helper (esc_html/absint/wp_kses).",
                _excerpt_at(raw_lines, line_no),
            )
        )
    return findings


def _check_unsafe_sql(
    path: str,
    stripped: str,
    in_string: list[bool],
    raw_lines: list[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    prepared_vars: set[str] = set()
    unsafe_vars: set[str] = set()
    for assign in _SQL_VAR_ASSIGN_RE.finditer(stripped):
        if in_string[assign.start()]:
            continue
        var_name = assign.group(1)
        rhs = assign.group(2)
        if "->prepare(" in rhs or "->prepare (" in rhs:
            prepared_vars.add(var_name)
            unsafe_vars.discard(var_name)
        elif _UNSAFE_INTERP_RE.search(rhs):
            unsafe_vars.add(var_name)

    for match in _WPDB_CALL_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        open_paren = stripped.find("(", match.end() - 1)
        args = read_call_args(stripped, open_paren, limit=500)
        if not args:
            continue
        first_arg = args[0]
        if "->prepare(" in first_arg or "->prepare (" in first_arg:
            continue
        is_unsafe = bool(_UNSAFE_INTERP_RE.search(first_arg))
        if not is_unsafe and re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", first_arg):
            if first_arg in unsafe_vars and first_arg not in prepared_vars:
                is_unsafe = True
        if is_unsafe:
            line_no = line_number(stripped, pos)
            findings.append(
                _finding(
                    "high",
                    "unsafe_sql",
                    path,
                    line_no,
                    f"$wpdb->{match.group(1)}() appears to interpolate variables without $wpdb->prepare().",
                    _excerpt_at(raw_lines, line_no),
                )
            )
    return findings


def _check_dangerous_exec(
    path: str,
    stripped: str,
    in_string: list[bool],
    raw_lines: list[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for match in _DANGEROUS_EXEC_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        fn_name = match.group(1).lower()
        if fn_name == "unserialize":
            call_slice = stripped[pos : min(len(stripped), pos + 240)]
            if _ALLOWED_CLASSES_FALSE_RE.search(call_slice):
                continue
            msg = "Unguarded unserialize() without ['allowed_classes' => false]."
        else:
            msg = f"Dangerous code/command execution primitive {fn_name}() detected."
        line_no = line_number(stripped, pos)
        findings.append(
            _finding(
                "high",
                "dangerous_exec",
                path,
                line_no,
                msg,
                _excerpt_at(raw_lines, line_no),
            )
        )
    return findings


def _check_ajax_and_post_guards(
    path: str,
    stripped: str,
    raw_lines: list[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    has_nonce = bool(_NONCE_CHECK_RE.search(stripped))
    has_cap = bool(_CAPABILITY_CHECK_RE.search(stripped))

    ajax_matches = list(_WP_AJAX_RE.finditer(stripped))
    if ajax_matches:
        has_priv_ajax = any(m.group(1) is None for m in ajax_matches)
        has_nopriv_ajax = any(m.group(1) is not None for m in ajax_matches)
        first_match = ajax_matches[0]
        line_no = line_number(stripped, first_match.start())
        if not has_nonce:
            findings.append(
                _finding(
                    "high",
                    "ajax_missing_guard",
                    path,
                    line_no,
                    "AJAX action handler missing nonce check (wp_verify_nonce / check_ajax_referer).",
                    _excerpt_at(raw_lines, line_no),
                )
            )
        elif has_priv_ajax and not has_nopriv_ajax and not has_cap:
            findings.append(
                _finding(
                    "medium",
                    "ajax_missing_guard",
                    path,
                    line_no,
                    "Authenticated wp_ajax_ handler missing current_user_can() capability check.",
                    _excerpt_at(raw_lines, line_no),
                )
            )
        return findings

    post_match = _POST_VAR_RE.search(stripped)
    if post_match and not has_nonce:
        line_no = line_number(stripped, post_match.start())
        findings.append(
            _finding(
                "high",
                "ajax_missing_guard",
                path,
                line_no,
                "$_POST handler missing nonce verification (wp_verify_nonce / check_admin_referer).",
                _excerpt_at(raw_lines, line_no),
            )
        )
    return findings


def _check_missing_abspath(
    path: str,
    stripped: str,
    raw_lines: list[str],
) -> list[dict[str, Any]]:
    lower = path.lower()
    if any(lower.startswith(prefix) or f"/{prefix}" in lower for prefix in _TEMPLATE_PREFIXES):
        return []
    if "<?php" not in stripped and "<?=" not in stripped:
        return []
    if _ABSPATH_RE.search(stripped):
        return []
    return [
        _finding(
            "low",
            "missing_abspath",
            path,
            1,
            "Standalone PHP file lacks defined('ABSPATH') direct-access guard.",
            _excerpt_at(raw_lines, 1),
        )
    ]


def _check_deprecated_woo(
    path: str,
    stripped: str,
    in_string: list[bool],
    raw_lines: list[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for pattern, message in _DEPRECATED_WOO_RULES:
        for match in pattern.finditer(stripped):
            pos = match.start()
            if in_string[pos]:
                continue
            line_no = line_number(stripped, pos)
            findings.append(
                _finding(
                    "medium",
                    "deprecated_woo",
                    path,
                    line_no,
                    message,
                    _excerpt_at(raw_lines, line_no),
                )
            )
    return findings


def _check_json_file(path: str, content: str) -> list[dict[str, Any]]:
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        lines = content.splitlines()
        line_no = max(1, exc.lineno)
        return [
            _finding(
                "high",
                "invalid_json",
                path,
                line_no,
                f"Invalid JSON: {exc.msg}",
                _excerpt_at(lines, line_no),
            )
        ]
    return []


def _is_mu_plugin_file(path: str, raw_content: str, component_kind: str | None) -> bool:
    if component_kind == "mu_plugin":
        return True
    lower = path.lower().replace("\\", "/")
    base = lower.rsplit("/", 1)[-1]
    if "mu-plugins/" in lower or base.startswith(("mu-", "mu_")):
        return True
    return "muplugins_loaded" in raw_content or "must-use" in raw_content.lower()


def _check_mu_activation_hooks(
    path: str,
    stripped: str,
    in_string: list[bool],
    raw_lines: list[str],
    raw_content: str,
    component_kind: str | None,
) -> list[dict[str, Any]]:
    if not _is_mu_plugin_file(path, raw_content, component_kind):
        return []
    findings: list[dict[str, Any]] = []
    for match in _MU_LIFECYCLE_HOOK_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        line_no = line_number(stripped, pos)
        findings.append(
            _finding(
                "medium",
                "mu_activation_hook_noop",
                path,
                line_no,
                f"{match.group(1)}() never fires in MU-plugins; use muplugins_loaded or versioned options instead.",
                _excerpt_at(raw_lines, line_no),
            )
        )
    return findings


def _check_unguarded_woo_calls(
    path: str,
    stripped: str,
    in_string: list[bool],
    raw_lines: list[str],
    raw_content: str,
) -> list[dict[str, Any]]:
    lower = path.lower().replace("\\", "/")
    if any(lower.startswith(prefix) or f"/{prefix}" in lower for prefix in _TEMPLATE_PREFIXES):
        return []
    if _WOO_GUARD_RE.search(raw_content):
        return []
    for match in _WOO_CALL_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        line_no = line_number(stripped, pos)
        return [
            _finding(
                "medium",
                "unguarded_woo_call",
                path,
                line_no,
                f"Unguarded WooCommerce call {match.group(1)}() without class_exists('WooCommerce') or function_exists() guard.",
                _excerpt_at(raw_lines, line_no),
            )
        ]
    return []


def _check_missing_hpos_declaration(
    path: str,
    stripped: str,
    in_string: list[bool],
    raw_lines: list[str],
    *,
    bundle_has_hpos: bool,
    component_kind: str | None,
) -> list[dict[str, Any]]:
    if bundle_has_hpos or component_kind == "theme":
        return []
    lower = path.lower().replace("\\", "/")
    if lower == "functions.php" or any(
        lower.startswith(prefix) or f"/{prefix}" in lower for prefix in _TEMPLATE_PREFIXES
    ):
        return []
    for match in _WOO_ORDER_TOUCH_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        line_no = line_number(stripped, pos)
        return [
            _finding(
                "medium",
                "missing_hpos_declaration",
                path,
                line_no,
                "Plugin interacts with WooCommerce orders without declaring HPOS (custom_order_tables) compatibility.",
                _excerpt_at(raw_lines, line_no),
            )
        ]
    return []


def run_static_checks(
    files: dict[str, str] | list[tuple[str, str]],
    max_findings: int = 40,
    *,
    component_kind: str | None = None,
) -> dict[str, Any]:
    """Run pure-Python security and WordPress/WooCommerce quality checks."""
    normalized_files = iter_source_files(files)
    all_findings: list[dict[str, Any]] = []
    bundle_has_hpos = any(
        bool(_HPOS_DECL_RE.search(content))
        for path, content in normalized_files
        if any(path.lower().endswith(ext) for ext in _PHP_EXTENSIONS)
    )

    for path, content in normalized_files:
        lower = path.lower()
        if lower.endswith(".json"):
            all_findings.extend(_check_json_file(path, content))
            continue
        if not any(lower.endswith(ext) for ext in _PHP_EXTENSIONS):
            continue

        raw_lines = content.splitlines()
        stripped, in_string, _depths = strip_comments_and_find_strings(
            content, allow_hash=True
        )
        all_findings.extend(_check_unescaped_superglobals(path, stripped, raw_lines))
        all_findings.extend(_check_unsafe_sql(path, stripped, in_string, raw_lines))
        all_findings.extend(_check_dangerous_exec(path, stripped, in_string, raw_lines))
        all_findings.extend(_check_ajax_and_post_guards(path, stripped, raw_lines))
        all_findings.extend(_check_deprecated_woo(path, stripped, in_string, raw_lines))
        all_findings.extend(
            _check_mu_activation_hooks(path, stripped, in_string, raw_lines, content, component_kind)
        )
        all_findings.extend(
            _check_unguarded_woo_calls(path, stripped, in_string, raw_lines, content)
        )
        all_findings.extend(
            _check_missing_hpos_declaration(
                path,
                stripped,
                in_string,
                raw_lines,
                bundle_has_hpos=bundle_has_hpos,
                component_kind=component_kind,
            )
        )
        all_findings.extend(_check_missing_abspath(path, stripped, raw_lines))

    all_findings.sort(
        key=lambda item: (
            _SEVERITY_ORDER.get(item["severity"], 9),
            item["path"],
            int(item["line"]),
            item["rule"],
        )
    )

    counts = {"high": 0, "medium": 0, "low": 0}
    for item in all_findings:
        sev = item["severity"]
        if sev in counts:
            counts[sev] += 1

    cap = max(0, min(int(max_findings), 500))
    returned = all_findings[:cap]
    truncated = len(all_findings) > len(returned)
    ok = counts["high"] == 0 and counts["medium"] == 0

    total = len(all_findings)
    if total == 0:
        summary = f"Static checks passed: 0 findings across {len(normalized_files)} file(s)."
    else:
        summary = (
            f"Static checks found {total} issue(s) across {len(normalized_files)} file(s) "
            f"(high={counts['high']}, medium={counts['medium']}, low={counts['low']})."
        )

    return {
        "ok": ok,
        "summary": summary,
        "counts": counts,
        "findings": returned,
        "truncated": truncated,
    }
