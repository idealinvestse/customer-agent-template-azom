"""WooCommerce template override version auditing for WordPress themes.

Pure-Python inspection of ``woocommerce/...`` override files against WooCommerce
8.x/9.x core template version baselines or caller-supplied reference versions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ecom_ops.wp_ops.code_index import _iter_files

_VERSION_TAG_RE = re.compile(r"(?im)@version\s*[:=]?\s*v?(\d+\.\d+(?:\.\d+)*)")
_SEMVER_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(?:[-+][A-Za-z0-9_.-]+)?\s*$")
_CRITICAL_PREFIXES = ("cart/", "checkout/", "emails/", "order/")
_CRITICAL_KEYWORDS = ("cart", "checkout", "payment", "thankyou", "email")

# Baseline core @version tags for common WooCommerce 9.x templates.
CORE_TEMPLATE_VERSIONS: dict[str, str] = {
    "archive-product.php": "8.6.0",
    "single-product.php": "1.6.4",
    "content-product.php": "9.4.0",
    "content-single-product.php": "3.6.0",
    "taxonomy-product-cat.php": "4.7.0",
    "taxonomy-product-tag.php": "4.7.0",
    "global/wrapper-start.php": "3.3.0",
    "global/wrapper-end.php": "3.3.0",
    "global/breadcrumb.php": "2.3.0",
    "global/quantity-input.php": "9.4.0",
    "loop/loop-start.php": "3.3.0",
    "loop/loop-end.php": "2.0.0",
    "loop/add-to-cart.php": "9.2.0",
    "loop/orderby.php": "3.6.0",
    "loop/pagination.php": "9.3.0",
    "loop/price.php": "1.6.4",
    "single-product/add-to-cart/simple.php": "7.0.1",
    "single-product/add-to-cart/variable.php": "6.1.0",
    "single-product/add-to-cart/variation-add-to-cart-button.php": "7.0.1",
    "single-product/price.php": "3.0.0",
    "single-product/stock.php": "3.0.0",
    "single-product/product-image.php": "9.0.0",
    "single-product/tabs/tabs.php": "3.8.0",
    "cart/cart.php": "7.9.0",
    "cart/cart-empty.php": "7.0.1",
    "cart/cart-totals.php": "2.3.6",
    "cart/cart-shipping.php": "8.8.0",
    "cart/mini-cart.php": "9.4.0",
    "cart/proceed-to-checkout-button.php": "7.0.1",
    "checkout/form-checkout.php": "9.4.0",
    "checkout/form-billing.php": "3.6.0",
    "checkout/form-shipping.php": "3.6.0",
    "checkout/review-order.php": "5.2.0",
    "checkout/payment.php": "8.1.0",
    "checkout/payment-method.php": "3.5.0",
    "checkout/thankyou.php": "8.1.0",
    "emails/email-header.php": "7.4.0",
    "emails/email-footer.php": "7.4.0",
    "emails/email-order-details.php": "3.7.0",
    "emails/email-order-items.php": "3.7.0",
    "emails/customer-completed-order.php": "3.7.0",
    "emails/customer-processing-order.php": "3.7.0",
    "emails/customer-on-hold-order.php": "7.3.0",
    "emails/admin-new-order.php": "3.7.0",
    "myaccount/my-account.php": "3.5.0",
    "myaccount/dashboard.php": "4.4.0",
    "myaccount/orders.php": "9.2.0",
    "myaccount/form-login.php": "9.2.0",
    "myaccount/form-edit-account.php": "8.7.0",
    "notices/error.php": "8.6.0",
    "notices/notice.php": "8.6.0",
    "notices/success.php": "8.6.0",
}

# Delta baseline for sites explicitly pinned to WooCommerce 8.x.
WOO_8_TEMPLATE_OVERRIDES: dict[str, str] = {
    "content-product.php": "3.6.0",
    "global/quantity-input.php": "7.8.0",
    "loop/add-to-cart.php": "3.3.0",
    "loop/pagination.php": "3.3.1",
    "single-product/product-image.php": "7.8.0",
    "cart/mini-cart.php": "7.9.0",
    "checkout/form-checkout.php": "3.5.0",
    "myaccount/orders.php": "8.5.0",
    "myaccount/form-login.php": "7.0.1",
}


def parse_template_version(content: str) -> str | None:
    """Extract the ``@version`` header value from a WooCommerce PHP template."""
    if not isinstance(content, str) or not content.strip():
        return None
    head = content[:16384]
    match = _VERSION_TAG_RE.search(head)
    if not match:
        return None
    return match.group(1)


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not version.strip():
        return None
    match = _SEMVER_RE.fullmatch(version.strip())
    if not match:
        return None
    parts = [int(seg) for seg in match.group(1).split(".")]
    return tuple(parts)


def compare_versions(override_ver: str | None, core_ver: str | None) -> str:
    """Compare override template version against core reference version."""
    left = _version_tuple(override_ver)
    right = _version_tuple(core_ver)
    if left is None or right is None:
        return "unknown"
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    if padded_left == padded_right:
        return "current"
    if padded_left < padded_right:
        return "outdated"
    return "newer"


def _normalize_template_key(path: str) -> str:
    clean = path.strip().replace("\\", "/").lstrip("./")
    if clean.lower().startswith("woocommerce/"):
        return clean[len("woocommerce/") :]
    marker = "/woocommerce/"
    idx = clean.lower().find(marker)
    if idx != -1:
        return clean[idx + len(marker) :]
    return clean


def _is_woo_override(path: str) -> bool:
    lower = path.lower()
    if not lower.endswith(".php"):
        return False
    return lower.startswith("woocommerce/") or "/woocommerce/" in lower


def _is_critical_template(template: str) -> bool:
    lower = template.lower()
    if lower.startswith(_CRITICAL_PREFIXES):
        return True
    return any(keyword in lower for keyword in _CRITICAL_KEYWORDS)


def _baseline_versions(
    woo_version: str | None,
    reference_versions: dict[str, str] | None,
) -> dict[str, str]:
    versions = dict(CORE_TEMPLATE_VERSIONS)
    ver_text = str(woo_version or "").strip()
    if ver_text.startswith("8.") or ver_text.lower() == "8":
        versions.update(WOO_8_TEMPLATE_OVERRIDES)
    if reference_versions:
        for raw_key, raw_ver in reference_versions.items():
            if not isinstance(raw_key, str) or not isinstance(raw_ver, str):
                continue
            norm_key = _normalize_template_key(raw_key)
            if norm_key and raw_ver.strip():
                versions[norm_key] = raw_ver.strip()
    return versions


def audit_overrides(
    theme_files: dict[str, str] | Iterable[tuple[str, str]],
    woo_version: str | None = None,
    reference_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Audit all ``woocommerce/...`` template overrides inside a theme."""
    baseline = _baseline_versions(woo_version, reference_versions)
    normalized_files = _iter_files(theme_files)

    items: list[dict[str, Any]] = []
    outdated = 0
    current = 0
    newer = 0
    unknown = 0
    critical_overrides: list[str] = []

    for path, content in normalized_files:
        if not _is_woo_override(path):
            continue
        template = _normalize_template_key(path)
        override_ver = parse_template_version(content)
        ref_ver = baseline.get(template)
        status = compare_versions(override_ver, ref_ver)
        critical = _is_critical_template(template)

        if status == "outdated":
            outdated += 1
        elif status == "current":
            current += 1
        elif status == "newer":
            newer += 1
        else:
            unknown += 1

        if critical:
            critical_overrides.append(path)

        items.append(
            {
                "path": path,
                "template": template,
                "override_version": override_ver,
                "reference_version": ref_ver,
                "status": status,
                "critical": critical,
            }
        )

    return {
        "woo_version": str(woo_version).strip() if woo_version and str(woo_version).strip() else "9.x",
        "total_overrides": len(items),
        "outdated": outdated,
        "current": current,
        "newer": newer,
        "unknown": unknown,
        "critical_overrides": critical_overrides,
        "items": items,
    }
