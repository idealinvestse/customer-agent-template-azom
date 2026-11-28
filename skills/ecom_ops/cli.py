"""Unified CLI for Azom ecom-ops V3."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from ecom_ops import __version__
from ecom_ops.actions.mail import MailService
from ecom_ops.actions.order_status import OrderStatusService
from ecom_ops.actions.product_desc import ProductDescService
from ecom_ops.actions.ssh_ops import SSHOpsService
from ecom_ops.actions.support import SupportService
from ecom_ops.integrations.mail import client_from_env as mail_client_from_env
from ecom_ops.integrations.woocommerce import client_from_env
from ecom_ops.profile import load_profile


class _WpCliError:
    def __init__(self, error_class: str, message: str) -> None:
        self.message = message
        self._error_class = error_class

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "message": self.message,
            "data": {
                "error_class": self._error_class,
                "executed": False,
                "live_calls": False,
            },
        }


def _load_edit_source(path: str) -> tuple[str | None, str | None]:
    """Read one local edit file. Errors do not include the file body."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None, "FileMissing"
    if b"\x00" in data:
        return None, "NotText"
    if len(data) > 65536:
        return None, "FileTooLarge"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "NotText"


def _print(result: Any) -> int:
    data = result.to_dict() if hasattr(result, "to_dict") else result
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    if isinstance(data, dict):
        return 0 if data.get("ok", True) else 1
    return 0


def _use_table_format(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "table", False)) or (
        str(getattr(args, "output_format", "json") or "json").lower() == "table"
    )


def _add_table_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--table",
        action="store_true",
        help="Render compact human-readable table instead of JSON",
    )
    subparser.add_argument(
        "--format",
        default="json",
        choices=("json", "table"),
        dest="output_format",
        help="Output format (json or table)",
    )


def _format_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    headers = [label for _, label in columns]
    matrix: list[list[str]] = []
    for row in rows:
        vals: list[str] = []
        for key, _label in columns:
            raw = row.get(key) if isinstance(row, dict) else ""
            if isinstance(raw, list):
                text = str(len(raw))
            elif raw is None:
                text = "-"
            else:
                text = str(raw).replace("\r", " ").replace("\n", " ").strip()
            if len(text) > 56:
                text = text[:53] + "..."
            vals.append(text or "-")
        matrix.append(vals)
    widths = [len(h) for h in headers]
    for r in matrix:
        for idx, cell in enumerate(r):
            if len(cell) > widths[idx]:
                widths[idx] = len(cell)
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for r in matrix:
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(len(headers))))
    if not matrix:
        lines.append("(inga poster)")
    return "\n".join(lines)


def _print_or_table(
    payload: dict[str, Any],
    *,
    args: argparse.Namespace,
    rows: list[dict[str, Any]],
    columns: list[tuple[str, str]],
) -> int:
    if not payload.get("ok", True) or not _use_table_format(args):
        return _print(payload)
    print(_format_table(rows, columns))
    return 0


def _cli_error_result(exc: Exception) -> int:
    from ecom_ops.rbac import AccessDenied

    payload: dict[str, Any] = {
        "ok": False,
        "error_class": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, AccessDenied):
        payload["error"] = "access_denied"
    code = getattr(exc, "code", None)
    if code:
        payload["error_code"] = str(code)
    return _print(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecom-ops",
        description=(
            "Azom ecom-ops V3: order-status, product-desc, support, SSH, mail "
            "(dashboard/OAuth/Telegram via separate entrypoints)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ecom-ops {__version__}",
    )
    _prof = load_profile()
    parser.add_argument("--site", default=_prof.customer, help="Customer/site id")
    parser.add_argument(
        "--actor",
        default=_prof.operator_actor,
        help=(
            f"Actor name ({_prof.viewer_actor}|{_prof.admin_actor}|{_prof.operator_actor})"
        ),
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force mock integrations (no external network)",
    )
    parser.add_argument(
        "--null-send",
        action="store_true",
        help="Null-send profile: refuse customer mail; record FU9 shadow decisions",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_order = sub.add_parser("order-status", help="Update WooCommerce order status")
    p_order.add_argument("--order-id", required=True)
    p_order.add_argument("--status", required=True)

    p_prod = sub.add_parser("product-desc", help="Generate product description")
    p_prod.add_argument("--product-id")
    p_prod.add_argument("--name")
    p_prod.add_argument("--features", default="")
    p_prod.add_argument("--language", default="sv")
    p_prod.add_argument("--publish", action="store_true")
    p_prod.add_argument(
        "--research-brief",
        default=None,
        dest="research_brief_id",
        help="Attach an existing res-... research brief",
    )
    p_prod.add_argument(
        "--research",
        action="store_true",
        help="Run product research before generating copy (refuses --publish)",
    )

    p_sup = sub.add_parser("support", help="Handle support message")
    p_sup.add_argument("--message", required=True)
    p_sup.add_argument("--email")
    p_sup.add_argument("--customer-name")
    p_sup.add_argument("--language", default="sv")

    p_ssh = sub.add_parser("ssh", help="Run allowlisted SSH / escalate critical")
    p_ssh.add_argument("--command", required=True, dest="ssh_command")
    p_ssh.add_argument("--host")

    p_health = sub.add_parser("ssh-health", help="Run SSH health checks")
    p_health.add_argument("--host")

    sub.add_parser("version", help="Print package version")
    sub.add_parser("status", help="Print runtime status (config + mock flags)")
    p_kpis = sub.add_parser(
        "kpis",
        help="Support-loop KPIs last N days (time-to-approve, edit distance)",
    )
    p_kpis.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (default 7)",
    )
    p_kpis.add_argument(
        "--baseline",
        default="",
        help="Compare KPIs to a human-filled baseline YAML/JSON (deltas only)",
    )
    sub.add_parser(
        "soak-preflight",
        help="Read-only A1 soak pre-flight checks (never marks soak complete)",
    )
    p_eval = sub.add_parser(
        "classify-eval",
        help="Score keyword classify + suggest rails against fixture pack",
    )
    p_draft_eval = sub.add_parser(
        "draft-eval",
        help="Score draft quality (sign-off, order_id, no fabricated tracking) against fixtures",
    )
    p_draft_eval.add_argument(
        "--dir",
        default=None,
        help="Fixture directory (default: tests/fixtures/draft_quality)",
    )
    p_drift = sub.add_parser(
        "drift-check",
        help="Detect LLM classify model drift (confidence + error rate) from telemetry",
    )
    p_drift.add_argument("--days", type=int, default=7, help="Lookback window (default 7)")
    p_trends = sub.add_parser(
        "trends",
        help="LLM quality trends (daily confidence + edit distance) from telemetry",
    )
    p_trends.add_argument("--days", type=int, default=30, help="Lookback window (default 30)")
    p_eval.add_argument(
        "--fixtures",
        default="",
        help="Fixture directory (default: tests/fixtures/support_classify)",
    )
    p_mkt = sub.add_parser("marketing", help="Google Ads + GA4 marketing ops")
    mkt_sub = p_mkt.add_subparsers(dest="marketing_command", required=True)
    p_mkt_digest = mkt_sub.add_parser("digest", help="Ads + GA4 performance digest")
    p_mkt_digest.add_argument("--days", type=int, default=None)
    mkt_sub.add_parser("health", help="Conversion + ecommerce event health")
    p_mkt_waste = mkt_sub.add_parser("waste", help="Search-term waste report")
    p_mkt_waste.add_argument("--days", type=int, default=None)
    mkt_sub.add_parser("pacing", help="Budget pacing alerts (no mutate)")
    p_mkt_cons = mkt_sub.add_parser(
        "consistency", help="Woo ↔ GA ↔ Ads purchase consistency"
    )
    p_mkt_cons.add_argument("--days", type=int, default=None)
    p_mkt_cons.add_argument(
        "--woo-purchases",
        type=int,
        default=None,
        help="Override Woo purchase count for comparison",
    )
    p_mkt_mer = mkt_sub.add_parser("mer", help="MER = Woo revenue / Ads spend")
    p_mkt_mer.add_argument("--days", type=int, default=None)
    p_mkt_mer.add_argument("--woo-revenue", type=float, default=None)
    mkt_sub.add_parser("snapshot", help="Compact digest+health+pacing")
    p_mkt_sug = mkt_sub.add_parser("suggests", help="Suggest queue")
    sug_sub = p_mkt_sug.add_subparsers(dest="suggests_command", required=True)
    sug_sub.add_parser("build", help="Build waste/pacing suggests")
    p_sug_list = sug_sub.add_parser("list", help="List suggests")
    p_sug_list.add_argument("--status", default="open")
    p_sug_deny = sug_sub.add_parser("deny", help="Deny a suggest")
    p_sug_deny.add_argument("--id", required=True, dest="suggest_id")
    p_sug_approve = sug_sub.add_parser(
        "approve", help="Approve and mutate (HITL; kill-switch aware)"
    )
    p_sug_approve.add_argument("--id", required=True, dest="suggest_id")
    p_mkt_mp = mkt_sub.add_parser(
        "mp-queue", help="Queue Measurement Protocol event (HITL, not sent)"
    )
    p_mkt_mp.add_argument("--name", required=True, help="Event name e.g. purchase")
    p_mkt_mp.add_argument("--payload-json", default="{}", help="JSON event params")
    p_mkt_feed = mkt_sub.add_parser(
        "merchant-queue", help="Queue Merchant product write (HITL)"
    )
    p_mkt_feed.add_argument("--offer-id", required=True)
    p_mkt_feed.add_argument("--title", default="")
    p_mkt_feed.add_argument("--op", default="upsert", choices=["upsert", "delete"])
    p_mkt_res = mkt_sub.add_parser(
        "research", help="Run or enqueue marketing intelligence research"
    )
    p_mkt_res.add_argument("--topic", required=True)
    p_mkt_res.add_argument(
        "--depth", default="standard", choices=("quick", "standard", "deep")
    )
    p_mkt_res.add_argument("--wait", action="store_true")

    p_smoke = sub.add_parser(
        "smoke",
        help="Opt-in integration smoke (requires AZOM_LIVE_SMOKE=1 or --live)",
    )
    p_smoke.add_argument(
        "--live",
        action="store_true",
        help="Force smoke even without AZOM_LIVE_SMOKE=1",
    )

    p_mail = sub.add_parser("mail", help="Send / fetch / reply email")
    mail_sub = p_mail.add_subparsers(dest="mail_command", required=True)

    p_mail_send = mail_sub.add_parser("send", help="Send an email")
    p_mail_send.add_argument("--to", required=True, help="Recipient (comma-separated ok)")
    p_mail_send.add_argument("--subject", required=True)
    p_mail_send.add_argument("--body", required=True)
    p_mail_send.add_argument("--cc", default="")
    p_mail_send.add_argument("--html-body", default="")
    p_mail_send.add_argument(
        "--provider",
        help="gmail|outlook|exchange_graph|generic_imap|generic_pop3",
    )

    p_mail_fetch = mail_sub.add_parser("fetch", help="Fetch inbox messages")
    p_mail_fetch.add_argument("--folder", default="INBOX")
    p_mail_fetch.add_argument("--limit", type=int, default=20)
    p_mail_fetch.add_argument(
        "--all",
        action="store_true",
        help="Fetch all messages (not only unread)",
    )
    p_mail_fetch.add_argument(
        "--provider",
        help="gmail|outlook|exchange_graph|generic_imap|generic_pop3",
    )

    p_mail_reply = mail_sub.add_parser("reply", help="Reply to a sender")
    p_mail_reply.add_argument("--to", required=True)
    p_mail_reply.add_argument("--subject", required=True)
    p_mail_reply.add_argument("--body", required=True)
    p_mail_reply.add_argument("--uid", dest="original_uid")
    p_mail_reply.add_argument("--html-body", default="")
    p_mail_reply.add_argument("--provider")

    p_cases = sub.add_parser("cases", help="Support cases from inbound mail")
    cases_sub = p_cases.add_subparsers(dest="cases_command", required=True)

    p_cases_poll = cases_sub.add_parser("poll", help="Fetch mailboxes and create cases")
    p_cases_poll.add_argument("--limit", type=int, default=20)

    p_cases_list = cases_sub.add_parser("list", help="List cases")
    p_cases_list.add_argument("--status", default="open")
    p_cases_list.add_argument("--limit", type=int, default=50)

    p_cases_show = cases_sub.add_parser("show", help="Show one case")
    p_cases_show.add_argument("--id", required=True, dest="case_id")

    p_cases_reply = cases_sub.add_parser(
        "reply", help="Approve draft and send reply"
    )
    p_cases_reply.add_argument("--id", required=True, dest="case_id")
    p_cases_reply.add_argument("--body", help="Override draft body")

    p_cases_close = cases_sub.add_parser("close", help="Close case without reply")
    p_cases_close.add_argument("--id", required=True, dest="case_id")
    p_cases_close.add_argument("--reason", default="")

    p_cases_draft = cases_sub.add_parser("draft", help="Save draft without sending")
    p_cases_draft.add_argument("--id", required=True, dest="case_id")
    p_cases_draft.add_argument("--body", required=True)

    cases_sub.add_parser(
        "regenerate", help="Regenerate draft from inbound (never sends)"
    ).add_argument("--id", required=True, dest="case_id")

    p_shadow = cases_sub.add_parser(
        "shadow-report",
        help="Summarize FU9 shadow observations (null-send trail)",
    )
    p_shadow.add_argument(
        "--days", type=int, default=7, help="Lookback window (default 7)"
    )

    p_cal_export = cases_sub.add_parser(
        "calibration-export",
        help="Oscar: redacted classify samples (no subject/body/draft)",
    )
    p_cal_export.add_argument("--days", type=int, default=30)
    p_cal_report = cases_sub.add_parser(
        "calibration-report",
        help="Oscar: classifier vs human-outcome cross-tab (read-only)",
    )
    p_cal_report.add_argument("--days", type=int, default=30)

    p_retention = cases_sub.add_parser(
        "retention-purge",
        help="GDPR: delete/redact closed+replied cases older than N days (default 90)",
    )
    p_retention.add_argument(
        "--days", type=int, default=None, help="Retention window (default 90)"
    )
    p_retention.add_argument(
        "--redact", action="store_true", help="Redact PII instead of hard delete"
    )
    p_retention.add_argument(
        "--dry-run", action="store_true", help="Report counts without modifying"
    )

    p_cases_res = cases_sub.add_parser(
        "research", help="Run or enqueue web research for a case (no customer PII)"
    )
    p_cases_res.add_argument("--id", required=True, dest="case_id")
    p_cases_res.add_argument("--query", default=None)
    p_cases_res.add_argument(
        "--depth", default="quick", choices=("quick", "standard")
    )
    p_cases_res.add_argument("--wait", action="store_true")

    p_wp = sub.add_parser(
        "wp",
        help="WordPress site operations (mock health, local backup metadata, typed file edits)",
    )
    wp_sub = p_wp.add_subparsers(dest="wp_command", required=True)
    wp_sub.add_parser("sites", help="List registered sites")
    p_wp_show = wp_sub.add_parser("show", help="Show one site and its latest checks")
    p_wp_show.add_argument("--id", default=None, dest="wp_site_id")
    wp_sub.add_parser("status", help="Health summary for every registered site")
    p_wp_check = wp_sub.add_parser("check", help="Run read-only health checks")
    p_wp_check.add_argument("--id", dest="wp_site_id")
    p_wp_check.add_argument(
        "--due",
        action="store_true",
        help="Run checks whose interval has elapsed",
    )
    p_wp_backup = wp_sub.add_parser("backup", help="Create a local mock backup manifest")
    p_wp_backup.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_backups = wp_sub.add_parser("backups", help="List local mock backup manifests")
    p_wp_backups.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_drill = wp_sub.add_parser("restore-drill", help="Record a staging restore drill")
    p_wp_drill.add_argument("--backup", required=True, dest="backup_id")
    p_wp_drill.add_argument("--target", required=True, dest="target_site_id")
    p_wp_files = wp_sub.add_parser("files", help="List themes, plugins, or files in one component")
    p_wp_files.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_files.add_argument("--kind", required=True, choices=("theme", "plugin", "mu_plugin"))
    p_wp_files.add_argument("--slug", default="")
    p_wp_propose = wp_sub.add_parser(
        "file-propose",
        help="Propose a typed edit of one existing theme or plugin file",
    )
    p_wp_propose.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_propose.add_argument("--kind", required=True, choices=("theme", "plugin", "mu_plugin"))
    p_wp_propose.add_argument("--slug", required=True)
    p_wp_propose.add_argument("--path", required=True, dest="relative_path")
    p_wp_propose.add_argument("--from", required=True, dest="from_file")
    p_wp_apply = wp_sub.add_parser(
        "file-apply",
        help="Apply a pending typed edit when the file text still matches",
    )
    p_wp_apply.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_apply.add_argument("--plan", required=True, dest="plan_id")
    p_wp_apply.add_argument("--from", required=True, dest="from_file")
    p_wp_changes = wp_sub.add_parser(
        "changes",
        help="List recorded WP/Woo changes and snapshots from Change Vault",
    )
    p_wp_changes.add_argument("--id", dest="wp_site_id", default=None)
    p_wp_changes.add_argument("--limit", type=int, default=20)
    p_wp_changes.add_argument("--category", default=None)
    p_wp_changes.add_argument("--status", default=None)
    p_wp_changes.add_argument("--batch", default=None, dest="batch_id")
    _add_table_args(p_wp_changes)
    p_wp_change_show = wp_sub.add_parser(
        "change-show",
        help="Preview snapshot verification and rollback readiness for one change",
    )
    p_wp_change_show.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_change_show.add_argument("--change", required=True, dest="change_id")
    p_wp_rollback = wp_sub.add_parser(
        "rollback",
        help="Preview or execute verified rollback of one Change Vault entry",
    )
    p_wp_rollback.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_rollback.add_argument("--change", required=True, dest="change_id")
    p_wp_rollback.add_argument(
        "--execute",
        action="store_true",
        help="Execute rollback (requires Oscar, AZOM_WP_RESTORE_ENABLED=1 in live, and --expected-sha256)",
    )
    p_wp_rollback.add_argument("--expected-sha256", dest="expected_sha256", default="")
    p_wp_rb_batch = wp_sub.add_parser(
        "rollback-batch",
        help="Preview or execute verified rollback of all Change Vault entries in a batch_id",
    )
    p_wp_rb_batch.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_rb_batch.add_argument("--batch", required=True, dest="batch_id")
    p_wp_rb_batch.add_argument(
        "--execute",
        action="store_true",
        help="Execute batch rollback (requires Oscar WP_OPS_RESTORE + WP_OPS_ADMIN)",
    )
    p_wp_rb_batch.add_argument("--confirm-batch-id", dest="confirm_batch_id", default="")
    p_wp_bridge_health = wp_sub.add_parser(
        "bridge-health",
        help="Read companion plugin (azom-ops-bridge) health and multilingual info",
    )
    p_wp_bridge_health.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_cache_flush = wp_sub.add_parser(
        "cache-flush",
        help="Flush WordPress object/page caches via companion plugin",
    )
    p_wp_cache_flush.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_bridge_maint = wp_sub.add_parser(
        "bridge-maintenance",
        help="Toggle timed maintenance mode via companion plugin",
    )
    p_wp_bridge_maint.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_bridge_maint.add_argument("--enabled", required=True, choices=("1", "0", "on", "off"))
    p_wp_bridge_maint.add_argument("--duration", type=int, default=300)
    p_wp_bridge_trans = wp_sub.add_parser(
        "bridge-translations",
        help="Inspect multilingual status/translations via companion plugin",
    )
    p_wp_bridge_trans.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_bridge_trans.add_argument("--post-id", type=int, default=None)
    p_wp_deploy_live = wp_sub.add_parser(
        "deploy-live",
        help="Deploy a verified wr-* plan via Bridge or *-wp-deployer SFTP with Change Vault snapshot",
    )
    p_wp_deploy_live.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_deploy_live.add_argument("--plan", required=True, dest="plan_id")
    p_wp_deploy_live.add_argument("--base-sha256", required=True, dest="base_sha256")
    p_wp_deploy_live.add_argument("--work-sha256", required=True, dest="work_sha256")
    p_wp_deploy_live.add_argument(
        "--transport", default="auto", choices=("auto", "bridge", "sftp_deploy")
    )
    p_wp_plugins_inv = wp_sub.add_parser(
        "plugins-inventory",
        help="Read full plugin inventory, pending updates, MU-plugins, and drop-ins",
    )
    p_wp_plugins_inv.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_inv.add_argument(
        "--wporg",
        action="store_true",
        help="Include opt-in WordPress.org metadata enrichment",
    )
    p_wp_plugins_mu = wp_sub.add_parser(
        "plugins-mu",
        help="Inspect MU-plugins (load order) and read-only drop-ins",
    )
    p_wp_plugins_mu.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_plans = wp_sub.add_parser(
        "plugins-plans",
        help="List stored plugin update plans (plu-*) with canary paths and statuses",
    )
    p_wp_plugins_plans.add_argument("--id", default=None, dest="wp_site_id")
    _add_table_args(p_wp_plugins_plans)
    p_wp_plugins_plan = wp_sub.add_parser(
        "plugins-plan-update",
        help="Create a hash-pinned plugin update plan (plu-*); never executes",
    )
    p_wp_plugins_plan.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_plan.add_argument(
        "--mode",
        default="policy",
        choices=("single", "selected", "all", "policy"),
    )
    p_wp_plugins_plan.add_argument("--slugs", default="", help="Comma-separated plugin slugs")
    p_wp_plugins_plan.add_argument(
        "--policy",
        default="non_critical_only",
        choices=("security_only", "patch_only", "minor_safe", "non_critical_only"),
    )
    p_wp_plugins_plan.add_argument("--exclude", default="", help="Comma-separated slugs to exclude")
    p_wp_plugins_plan.add_argument(
        "--include-critical",
        action="store_true",
        dest="include_critical",
        help="Allow critical WooCommerce/payment plugins in plan (Oscar only)",
    )
    p_wp_plugins_upd = wp_sub.add_parser(
        "plugins-update",
        help="Execute a hash-pinned plugin update plan (plu-*) with canary stop-on-fail",
    )
    p_wp_plugins_upd.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_upd.add_argument("--plan", required=True, dest="plan_id")
    p_wp_plugins_upd.add_argument("--expected-sha256", required=True, dest="expected_sha256")
    p_wp_plugins_upd.add_argument(
        "--allow-critical",
        action="store_true",
        dest="allow_critical",
        help="Explicitly confirm critical plugin updates",
    )
    p_wp_plugins_auto = wp_sub.add_parser(
        "plugins-autoupdate",
        help="Inspect policy/drift, plan (pau-*), or execute plugin auto-update changes",
    )
    p_wp_plugins_auto.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_auto.add_argument("--show", action="store_true", help="Show stored auto-update policy")
    p_wp_plugins_auto.add_argument("--drift", action="store_true", help="Check auto-update drift vs live site")
    p_wp_plugins_auto.add_argument(
        "--plan",
        nargs="?",
        const="__CREATE__",
        default=None,
        dest="autoupdate_plan",
        help="Create a pau-* plan (flag only) or specify pau-* ID with --execute",
    )
    p_wp_plugins_auto.add_argument(
        "--preset",
        default=None,
        choices=("disable_all", "non_critical_only", "security_only", "custom"),
    )
    p_wp_plugins_auto.add_argument(
        "--changes-json",
        default="",
        dest="changes_json",
        help="Optional JSON list of {plugin_file, enabled} overrides for custom preset",
    )
    p_wp_plugins_auto.add_argument("--allow-critical", action="store_true", dest="allow_critical")
    p_wp_plugins_auto.add_argument("--execute", action="store_true", help="Execute a hash-pinned pau-* plan")
    p_wp_plugins_auto.add_argument("--expected-sha256", default="", dest="expected_sha256")
    p_wp_plugins_diag = wp_sub.add_parser(
        "plugins-diagnostics",
        help="Inspect paused plugins, scrubbed debug.log tail, hook map, and plugin backups",
    )
    p_wp_plugins_diag.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_diag.add_argument("--hooks", default="", help="Comma-separated WP hook names")
    p_wp_plugins_diag.add_argument("--log-lines", type=int, default=80, dest="log_lines")
    p_wp_plugins_quar = wp_sub.add_parser(
        "plugins-quarantine",
        help="Out-of-band SFTP quarantine of a broken plugin or MU-plugin (Oscar)",
    )
    p_wp_plugins_quar.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_quar.add_argument("--slug", required=True)
    p_wp_plugins_quar.add_argument("--kind", default="plugin", choices=("plugin", "mu_plugin"))
    p_wp_plugins_unquar = wp_sub.add_parser(
        "plugins-unquarantine",
        help="Restore a quarantined plugin or MU-plugin via out-of-band SFTP (Oscar)",
    )
    p_wp_plugins_unquar.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_unquar.add_argument("--slug", required=True)
    p_wp_plugins_unquar.add_argument("--quarantine-name", default="", dest="quarantine_name")
    p_wp_plugins_unquar.add_argument("--kind", default="plugin", choices=("plugin", "mu_plugin"))
    p_wp_plugins_rec = wp_sub.add_parser(
        "plugins-recovery",
        help="Plan (plr-*) or execute hash-pinned plugin recovery (rollback/deactivate/quarantine)",
    )
    p_wp_plugins_rec.add_argument("--id", default=None, dest="wp_site_id")
    p_wp_plugins_rec.add_argument("--slug", default="")
    p_wp_plugins_rec.add_argument(
        "--action",
        default="",
        choices=("", "rollback_snapshot", "deactivate", "quarantine_sftp", "restore_from_quarantine"),
    )
    p_wp_plugins_rec.add_argument("--change", default=None, dest="change_id")
    p_wp_plugins_rec.add_argument("--execute", action="store_true")
    p_wp_plugins_rec.add_argument("--plan", default="", dest="plan_id")
    p_wp_plugins_rec.add_argument("--expected-sha256", default="", dest="expected_sha256")

    p_ops = sub.add_parser(
        "ops",
        help="Runtime housekeeping (GC) and health status",
    )
    ops_sub = p_ops.add_subparsers(dest="ops_command", required=True)
    p_ops_gc = ops_sub.add_parser(
        "gc",
        help="Clean expired WP workspaces, old inbox media, expired proposals, and rotate logs",
    )
    p_ops_gc.add_argument(
        "--dry-run",
        action="store_true",
        help="Report cleanup counts without modifying disk or SQLite",
    )
    p_ops_gc.add_argument(
        "--grace-hours",
        type=int,
        default=24,
        help="Grace window in hours before deleting expired wr-* workspace artifacts (default 24)",
    )
    p_ops_gc.add_argument(
        "--pv-max-age-hours",
        type=int,
        default=24,
        help="Max age in hours for untracked pv-* preview workspaces (default 24)",
    )
    p_ops_gc.add_argument(
        "--inbox-max-age-hours",
        type=int,
        default=24,
        help="Max age in hours for inbound Telegram/Messenger media (default 24)",
    )
    p_ops_gc.add_argument(
        "--proposal-max-age-hours",
        type=int,
        default=24,
        help="Max age in hours for open proposals without explicit expires_at (default 24)",
    )
    p_ops_gc.add_argument(
        "--rotate-max-mb",
        type=float,
        default=10.0,
        help="Rotate and gzip telemetry.jsonl/audit.jsonl above this size in MB (default 10.0)",
    )
    p_ops_gc.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON output",
    )
    p_ops_status = ops_sub.add_parser(
        "status",
        help="Show runtime health snapshot (poller lease, queues, breakers, OAuth TTL, disk, errors)",
    )
    p_ops_status.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON output",
    )

    p_syscheck = sub.add_parser(
        "systemcheck",
        help="Run deep system, config, DB, safety, and optional network/systemd checks",
    )
    p_syscheck.add_argument("--mode", choices=("auto", "mock", "live"), default="auto")
    p_syscheck.add_argument("--mock", action="store_true", default=argparse.SUPPRESS)
    p_syscheck.add_argument("--domain", default=None)
    p_syscheck.add_argument("--expected-ip", default=None, dest="expected_ip")
    p_syscheck.add_argument("--check-network", action="store_true", dest="check_network")
    p_syscheck.add_argument("--no-port-check", action="store_true", dest="no_port_check")
    p_syscheck.add_argument("--strict", action="store_true")
    p_syscheck.add_argument("--json", action="store_true", dest="as_json")

    p_hlthcheck = sub.add_parser(
        "healthcheck",
        help="Run runtime healthcheck (local HTTP /live /health /ready, optional public HTTPS, systemd, runtime snapshot)",
    )
    p_hlthcheck.add_argument("--mode", choices=("auto", "mock", "live"), default="auto")
    p_hlthcheck.add_argument("--mock", action="store_true", default=argparse.SUPPRESS)
    p_hlthcheck.add_argument("--local-url", default=None, dest="local_url")
    p_hlthcheck.add_argument("--public-url", default=None, dest="public_url")
    p_hlthcheck.add_argument("--no-http", action="store_true", dest="no_http")
    p_hlthcheck.add_argument("--check-public", action="store_true", dest="check_public")
    p_hlthcheck.add_argument("--timeout", type=float, default=4.0)
    p_hlthcheck.add_argument("--strict", action="store_true")
    p_hlthcheck.add_argument("--json", action="store_true", dest="as_json")

    p_search = sub.add_parser("search", help="Run bounded synchronous web search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--profile", default="deep_general")
    p_search.add_argument("--locale", default=None)
    p_search.add_argument("--freshness-days", type=int, default=None)
    p_search.add_argument("--max-results", type=int, default=5)

    p_fetch = sub.add_parser("fetch-url", help="Fetch and extract clean text from a public HTTPS URL")
    p_fetch.add_argument("--url", required=True)
    p_fetch.add_argument("--profile", default="deep_general")
    p_fetch.add_argument("--max-chars", type=int, default=5000)

    p_res = sub.add_parser("research", help="Async Deep Research jobs and briefs")
    res_sub = p_res.add_subparsers(dest="research_command", required=True)

    p_res_start = res_sub.add_parser("start", help="Enqueue (or --wait) a Deep Research job")
    p_res_start.add_argument("--topic", required=True)
    p_res_start.add_argument("--profile", default="deep_general")
    p_res_start.add_argument(
        "--depth", default="standard", choices=("quick", "standard", "deep")
    )
    p_res_start.add_argument("--market", default=None, choices=("SE", "NO", "DK"))
    p_res_start.add_argument("--target-ref", default=None)
    p_res_start.add_argument("--wait", action="store_true")

    p_res_list = res_sub.add_parser("list", help="List research jobs visible to actor")
    p_res_list.add_argument("--status", default=None)
    p_res_list.add_argument("--limit", type=int, default=20)

    p_res_show = res_sub.add_parser("show", help="Show a persisted research brief")
    p_res_show.add_argument("--id", required=True, dest="research_id")
    p_res_show.add_argument(
        "--section", default="full", choices=("full", "summary", "sources")
    )

    p_res_status = res_sub.add_parser("status", help="Show research job status and events")
    p_res_status.add_argument("--id", required=True, dest="research_id")

    p_res_steer = res_sub.add_parser("steer", help="Queue a steering note for a research job")
    p_res_steer.add_argument("--id", required=True, dest="research_id")
    p_res_steer.add_argument("--note", required=True)

    p_res_cancel = res_sub.add_parser("cancel", help="Cancel a research job")
    p_res_cancel.add_argument("--id", required=True, dest="research_id")

    p_res_profiles = res_sub.add_parser(
        "profiles", help="List goal/ambition research profiles or recommend one"
    )
    p_res_profiles.add_argument("--id", default=None, dest="profile_id")
    p_res_profiles.add_argument("--recommend", default=None, dest="recommend_goal")
    p_res_profiles.add_argument(
        "--ambition",
        default=None,
        choices=("quick", "standard", "deep"),
    )

    p_seo = sub.add_parser(
        "seo",
        help="SEO Studio: audits, hash-pinned SEO fix plans, strategies, campaigns & link packages",
    )
    seo_sub = p_seo.add_subparsers(dest="seo_command", required=True)

    p_seo_audit = seo_sub.add_parser("audit", help="Run read-only SEO audit")
    p_seo_audit.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_audit.add_argument(
        "--scope",
        default="site",
        choices=(
            "site",
            "technical",
            "page",
            "product",
            "media",
            "links",
            "cannibalization",
            "gsc",
            "parity",
            "deep_crawl",
        ),
    )
    p_seo_audit.add_argument("--market", default="no", choices=("no", "se", "dk"))
    p_seo_audit.add_argument("--path", default="/")
    p_seo_audit.add_argument("--target-id", default=None, dest="target_id")
    p_seo_audit.add_argument("--limit", type=int, default=50)

    p_seo_audits = seo_sub.add_parser("audits", help="List persisted SEO audits (seoa-*)")
    p_seo_audits.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_audits.add_argument("--limit", type=int, default=20)
    _add_table_args(p_seo_audits)

    p_seo_fixes = seo_sub.add_parser("fixes", help="List persisted SEO fix plans (seom-*)")
    p_seo_fixes.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_fixes.add_argument("--status", default=None)
    p_seo_fixes.add_argument("--limit", type=int, default=30)
    _add_table_args(p_seo_fixes)

    p_seo_batches = seo_sub.add_parser("batches", help="List persisted SEO batch fix plans (seob-*)")
    p_seo_batches.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_batches.add_argument("--limit", type=int, default=20)
    _add_table_args(p_seo_batches)

    p_seo_batch_prop = seo_sub.add_parser(
        "batch-propose",
        help="Propose a SHA-256-pinned batch fix plan (seob-*) from an SEO audit",
    )
    p_seo_batch_prop.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_batch_prop.add_argument("--audit", required=True, dest="audit_id")
    p_seo_batch_prop.add_argument(
        "--scope", default="all", choices=("all", "media_alt", "seo_meta")
    )
    p_seo_batch_prop.add_argument("--limit", type=int, default=20)

    p_seo_batch_apply = seo_sub.add_parser(
        "batch-apply",
        help="Apply a SHA-256-pinned batch fix plan (seob-*) (Oscar WP_OPS_ADMIN only)",
    )
    p_seo_batch_apply.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_batch_apply.add_argument("--batch", required=True, dest="batch_id")
    p_seo_batch_apply.add_argument("--expected-sha256", required=True, dest="expected_sha256")

    p_seo_gsc = seo_sub.add_parser("gsc", help="Read-only GSC strike-distance quick-wins")
    p_seo_gsc.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_gsc.add_argument("--days", type=int, default=28)
    _add_table_args(p_seo_gsc)

    p_seo_parity = seo_sub.add_parser(
        "parity", help="Run Nordic SE/NO/DK hreflang and localization parity check"
    )
    p_seo_parity.add_argument("--site", default=None, dest="seo_site_id")
    _add_table_args(p_seo_parity)

    p_seo_diff = seo_sub.add_parser("diff", help="Diff latest vs previous SEO audit trend")
    p_seo_diff.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_diff.add_argument("--latest", default=None, dest="latest_audit_id")
    p_seo_diff.add_argument("--previous", default=None, dest="previous_audit_id")

    p_seo_digest = seo_sub.add_parser(
        "digest", help="Weekly SEO & plugin drift digest (JSON or --markdown)"
    )
    p_seo_digest.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_digest.add_argument("--days", type=int, default=7)
    p_seo_digest.add_argument("--markdown", action="store_true", help="Print Swedish Markdown brief")

    p_seo_propose = seo_sub.add_parser(
        "fix-propose", help="Create a reviewable hash-pinned SEO fix plan (seom-*); never executes"
    )
    p_seo_propose.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_propose.add_argument(
        "--fix-type",
        required=True,
        dest="fix_type",
        choices=("seo_meta", "media_alt", "product_seo", "page_seo", "schema_jsonld"),
    )
    p_seo_propose.add_argument(
        "--target-type",
        required=True,
        dest="target_type",
        choices=("product", "page", "post", "media", "category", "site"),
    )
    p_seo_propose.add_argument("--target-id", required=True, dest="target_id")
    p_seo_propose.add_argument("--json-fields", required=True, dest="json_fields")
    p_seo_propose.add_argument("--summary", default="")
    p_seo_propose.add_argument("--market", default="no", choices=("no", "se", "dk"))

    p_seo_apply = seo_sub.add_parser(
        "fix-apply", help="Apply a hash-pinned SEO fix plan (Oscar WP_OPS_ADMIN only)"
    )
    p_seo_apply.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_apply.add_argument("--plan", required=True, dest="plan_id")
    p_seo_apply.add_argument("--expected-sha256", required=True, dest="expected_sha256")

    p_seo_strat = seo_sub.add_parser("strategy", help="Generate a Nordic SEO strategy (seos-*)")
    p_seo_strat.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_strat.add_argument("--title", required=True)
    p_seo_strat.add_argument("--market", default="no", choices=("no", "se", "dk"))
    p_seo_strat.add_argument("--topics", default="", help="Comma-separated focus topics")
    p_seo_strat.add_argument("--goals", default="")
    p_seo_strat.add_argument("--research-brief", default=None, dest="research_brief_id")

    p_seo_camp = seo_sub.add_parser("campaign", help="Generate an SEO campaign (seoc-*)")
    p_seo_camp.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_camp.add_argument("--name", required=True)
    p_seo_camp.add_argument("--market", default="no", choices=("no", "se", "dk"))
    p_seo_camp.add_argument("--strategy-id", default=None, dest="strategy_id")
    p_seo_camp.add_argument("--keywords", default="", help="Comma-separated target keywords")
    p_seo_camp.add_argument("--urls", default="", help="Comma-separated target URLs")
    p_seo_camp.add_argument(
        "--campaign-type",
        default="commercial",
        dest="campaign_type",
        choices=(
            "commercial",
            "seasonal",
            "product_launch",
            "content_hub",
            "technical_sprint",
            "category",
            "informational",
            "technical",
        ),
    )
    p_seo_camp.add_argument("--objective", default="")
    p_seo_camp.add_argument("--research-brief", default=None, dest="research_brief_id")

    p_seo_lp = seo_sub.add_parser(
        "link-pack", help="Design a link-package order brief (seol-*) with anchor guardrails"
    )
    p_seo_lp.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_lp.add_argument("--name", required=True)
    p_seo_lp.add_argument("--market", default="no", choices=("no", "se", "dk"))
    p_seo_lp.add_argument("--campaign-id", default=None, dest="campaign_id")
    p_seo_lp.add_argument("--budget", type=float, default=15000.0, dest="budget_sek")
    p_seo_lp.add_argument("--links", type=int, default=6, dest="link_count")
    p_seo_lp.add_argument(
        "--targets-json",
        default="",
        dest="targets_json",
        help="Optional JSON list of target dicts ({target_url, keyword, anchor_type, anchor_text})",
    )
    p_seo_lp.add_argument("--notes", default="")
    p_seo_lp.add_argument("--export", default=None, choices=("md", "json", "csv"), dest="export_fmt")

    p_seo_lp_status = seo_sub.add_parser(
        "link-pack-status", help="Transition link-package HITL status (Oscar approves/orders)"
    )
    p_seo_lp_status.add_argument("--id", required=True, dest="package_id")
    p_seo_lp_status.add_argument(
        "--status",
        required=True,
        dest="new_status",
        choices=(
            "draft",
            "ready_for_review",
            "approved_for_manual_order",
            "ordered_external",
            "delivered",
            "cancelled",
        ),
    )
    p_seo_lp_status.add_argument("--note", default="")

    p_seo_lps = seo_sub.add_parser("link-packs", help="List persisted link packages (seol-*)")
    p_seo_lps.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_lps.add_argument("--limit", type=int, default=20)
    _add_table_args(p_seo_lps)

    p_seo_lp_edit = seo_sub.add_parser(
        "link-pack-edit", help="Edit a draft link package in-place and re-evaluate anchor guardrails"
    )
    p_seo_lp_edit.add_argument("--id", required=True, dest="package_id")
    p_seo_lp_edit.add_argument("--name", default=None)
    p_seo_lp_edit.add_argument("--budget", type=float, default=None, dest="budget_sek")
    p_seo_lp_edit.add_argument("--targets-json", default="", dest="targets_json")
    p_seo_lp_edit.add_argument("--notes", default=None)

    p_seo_lp_verify = seo_sub.add_parser(
        "link-pack-verify", help="Verify delivered backlink placements (HTTP, indexability, anchor, rel)"
    )
    p_seo_lp_verify.add_argument("--id", required=True, dest="package_id")
    _add_table_args(p_seo_lp_verify)

    p_seo_vendors = seo_sub.add_parser(
        "vendors",
        help="Vet and rank white-hat Nordic SEO link-shops (SE/NO/DK) for a market & budget",
    )
    p_seo_vendors.add_argument("--market", default="no", choices=("no", "se", "dk"))
    p_seo_vendors.add_argument("--budget", type=float, default=15000.0, dest="budget_sek")
    p_seo_vendors.add_argument("--links", type=int, default=6, dest="link_count")
    p_seo_vendors.add_argument("--goal", default="organic_growth")
    p_seo_vendors.add_argument(
        "--no-web-research",
        action="store_true",
        dest="no_web_research",
        help="Skip web-search vendor vetting signals and use curated rubric only",
    )
    _add_table_args(p_seo_vendors)

    p_seo_autopilot = seo_sub.add_parser(
        "autopilot",
        help="Run autonomous HITL SEO pipeline (audit -> research -> strategy -> campaign -> vetted shop -> draft link-pack)",
    )
    p_seo_autopilot.add_argument("--site", default=None, dest="seo_site_id")
    p_seo_autopilot.add_argument("--topic", default="")
    p_seo_autopilot.add_argument("--market", default=None, choices=("no", "se", "dk"))
    p_seo_autopilot.add_argument("--budget", type=float, default=15000.0, dest="budget_sek")
    p_seo_autopilot.add_argument("--links", type=int, default=6, dest="link_count")
    p_seo_autopilot.add_argument(
        "--no-web-research",
        action="store_true",
        dest="no_web_research",
        help="Skip SERP & vendor web-research enrichment",
    )
    p_seo_autopilot.add_argument(
        "--with-batch",
        action="store_true",
        dest="with_batch",
        help="Also generate a reviewable non-executed seob-* batch fix preview",
    )
    _add_table_args(p_seo_autopilot)

    p_si = sub.add_parser(
        "shop-intel",
        help="Webshop Intelligence: store learning, tailored context, diffs, security & schedules",
    )
    si_sub = p_si.add_subparsers(dest="shop_intel_command", required=True)

    p_si_run = si_sub.add_parser("run", help="Run read-only Shop Intelligence learning pass")
    p_si_run.add_argument("--site", default=None, dest="si_site_id")
    p_si_run.add_argument("--mode", default="full", choices=("full", "quick", "security"))
    p_si_run.add_argument("--domains", default="", help="Comma-separated domain filter")

    p_si_status = si_sub.add_parser("status", help="Show Shop Intel status, gates, and recent runs")
    p_si_status.add_argument("--site", default=None, dest="si_site_id")

    p_si_query = si_sub.add_parser("query", help="Query learned facts and dossier knowledge")
    p_si_query.add_argument("--site", default=None, dest="si_site_id")
    p_si_query.add_argument("--domain", default=None)
    p_si_query.add_argument("--query", default=None)
    p_si_query.add_argument("--purpose", default=None)
    p_si_query.add_argument("--max-chars", type=int, default=None, dest="max_chars")

    p_si_sec = si_sub.add_parser("security", help="Show latest Shop Intel security posture & findings")
    p_si_sec.add_argument("--site", default=None, dest="si_site_id")
    p_si_sec.add_argument(
        "--severity",
        default=None,
        choices=("critical", "high", "medium", "low", "info"),
    )
    p_si_sec.add_argument("--limit", type=int, default=50)

    p_si_diff = si_sub.add_parser("diff", help="Show configuration & security drift diffs (sid-*)")
    p_si_diff.add_argument("--site", default=None, dest="si_site_id")
    p_si_diff.add_argument("--limit", type=int, default=20)

    p_si_ctx = si_sub.add_parser(
        "context",
        help="Build bounded, nonce-framed SHOP_INTEL_CONTEXT slice for a purpose",
    )
    p_si_ctx.add_argument("--site", default=None, dest="si_site_id")
    p_si_ctx.add_argument("--purpose", required=True)
    p_si_ctx.add_argument("--max-chars", type=int, default=None, dest="max_chars")

    p_si_exp = si_sub.add_parser("export", help="Export latest Shop Intel dossier artifact (md|json)")
    p_si_exp.add_argument("--site", default=None, dest="si_site_id")
    p_si_exp.add_argument("--format", default="json", choices=("md", "json"), dest="export_fmt")

    p_si_srun = si_sub.add_parser("schedule-run", help="Run due Shop Intelligence schedules")
    p_si_srun.add_argument("--site", default=None, dest="si_site_id")

    p_si_sset = si_sub.add_parser(
        "schedule-set",
        help="Configure recurring Shop Intel schedule (Oscar WP_OPS_ADMIN / RESEARCH_ADMIN only)",
    )
    p_si_sset.add_argument("--site", default=None, dest="si_site_id")
    p_si_sset.add_argument("--interval-hours", type=int, default=24, dest="interval_hours")
    p_si_sset.add_argument("--mode", default="full", choices=("full", "quick", "security"))
    si_en_grp = p_si_sset.add_mutually_exclusive_group()
    si_en_grp.add_argument("--enabled", action="store_true", dest="sched_enabled", default=True)
    si_en_grp.add_argument("--disabled", action="store_false", dest="sched_enabled")

    return parser


def _mock_mode() -> bool:
    """True after ``--mock`` or when ``AZOM_USE_MOCK`` is already set.

    Mock integrations must not call the model router. Call this after
    ``--mock`` has copied itself into the environment.
    """
    from ecom_ops.runtime_env import env_flag

    return env_flag("AZOM_USE_MOCK")


def main(argv: list[str] | None = None) -> int:
    from ecom_ops.rbac import AccessDenied
    from ecom_ops.runtime_env import bootstrap_runtime

    bootstrap_runtime()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run_cli_command(parser, args)
    except AccessDenied as exc:
        return _cli_error_result(exc)


def _run_cli_command(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> int:
    if args.mock:
        import os

        os.environ["AZOM_USE_MOCK"] = "1"

    if getattr(args, "null_send", False):
        from ecom_ops.runtime_profile import enable_null_send

        enable_null_send()

    # Defer Woo client — version/status/mail/cases must not require Woo secrets
    woo = None

    def _woo():
        nonlocal woo
        if woo is None:
            woo = client_from_env(use_mock=args.mock or None)
        return woo

    if args.command == "order-status":
        svc = OrderStatusService(woo=_woo())
        result = svc.update(
            order_id=args.order_id,
            status=args.status,
            site=args.site,
            actor=args.actor,
        )
        return _print(result)

    if args.command == "product-desc":
        svc = ProductDescService(woo=_woo())
        kwargs: dict[str, Any] = {
            "product_id": args.product_id,
            "name": args.name,
            "features": args.features,
            "language": args.language,
            "site": args.site,
            "publish": args.publish,
            "actor": args.actor,
            "allow_llm": not _mock_mode(),
        }
        if getattr(args, "research_brief_id", None):
            kwargs["research_brief_id"] = args.research_brief_id
        if getattr(args, "research", False):
            kwargs["run_research"] = True
        try:
            result = svc.generate(**kwargs)
        except Exception as exc:
            return _cli_error_result(exc)
        return _print(result)

    if args.command == "support":
        svc = SupportService()
        result = svc.handle(
            args.message,
            customer_email=args.email,
            customer_name=args.customer_name,
            language=args.language,
            site=args.site,
            actor=args.actor,
            use_mock=True if _mock_mode() else None,
            allow_llm=not _mock_mode(),
        )
        return _print(result)

    if args.command == "ssh":
        svc = SSHOpsService(host=args.host)
        result = svc.run(args.ssh_command, site=args.site, actor=args.actor)
        return _print(result)

    if args.command == "ssh-health":
        svc = SSHOpsService(host=args.host)
        results = svc.health(site=args.site, actor=args.actor)
        payload = [r.to_dict() for r in results]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if all(r.ok for r in results) else 1

    if args.command == "version":
        print(json.dumps({"version": __version__, "package": "azom-ecom-ops"}, indent=2))
        return 0

    if args.command == "status":
        import os

        from ecom_ops.budget import budget_status
        from ecom_ops.config import load_app_config
        from ecom_ops.marketing.config import load_marketing_config
        from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured
        from ecom_ops.oauth.google_marketing import (
            GoogleMarketingOAuthStore,
            google_marketing_oauth_configured,
        )
        from ecom_ops.ops_status import readiness_from_last_poll, web_search_readiness
        from ecom_ops.runtime_profile import null_send_label

        try:
            cfg = load_app_config()
            budget = budget_status()
            mkt = load_marketing_config()
            mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {
                "1",
                "true",
                "yes",
            }
            ga4_ready = bool(mkt.ga4_property_ids) or mock
            ads_ready = bool(mkt.google_ads_customer_ids) or mock
            status = {
                "ok": True,
                "version": __version__,
                "mock": mock,
                "null_send": null_send_label(),
                "customer": cfg.customer.customer,
                "domains": list(cfg.customer.domains),
                "gmail_oauth_configured": gmail_oauth_configured(),
                "gmail_tokens_stored": GmailOAuthStore().has_tokens(),
                "google_marketing_oauth_configured": google_marketing_oauth_configured(),
                "google_marketing_tokens_stored": GoogleMarketingOAuthStore().has_tokens(),
                "ga4": "on" if ga4_ready else "off",
                "ads": "on" if ads_ready else "off",
                "telegram_configured": bool(
                    os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                ),
                "readiness": readiness_from_last_poll(),
                "web_search": web_search_readiness(),
                "budget": budget,
            }
        except Exception as exc:
            status = {
                "ok": False,
                "version": __version__,
                "null_send": null_send_label(),
                "ga4": "off",
                "ads": "off",
                "web_search": {"ready": False, "status": "error"},
                "error": str(exc),
            }
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status.get("ok") else 1

    if args.command == "smoke":
        from ecom_ops.smoke import run_live_smoke

        result = run_live_smoke(force=bool(getattr(args, "live", False)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", False) else 1

    if args.command == "soak-preflight":
        from ecom_ops.soak_preflight import run_soak_preflight

        result = run_soak_preflight()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "kpis":
        from ecom_ops.kpis import compare_kpis_to_baseline, load_baseline, support_kpis_last_days

        result = support_kpis_last_days(days=int(getattr(args, "days", 7) or 7))
        baseline_path = (getattr(args, "baseline", None) or "").strip()
        if baseline_path:
            result = compare_kpis_to_baseline(result, load_baseline(baseline_path))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "classify-eval":
        from ecom_ops.classify_eval import evaluate_fixtures

        fix = (getattr(args, "fixtures", None) or "").strip() or None
        result = evaluate_fixtures(directory=fix)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "draft-eval":
        from ecom_ops.draft_eval import evaluate_drafts

        fix = (getattr(args, "dir", None) or "").strip() or None
        result = evaluate_drafts(directory=fix)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "drift-check":
        from ecom_ops.drift_check import drift_check

        result = drift_check(days=args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "trends":
        from ecom_ops.trends import quality_trends

        result = quality_trends(days=args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "mail":
        provider = getattr(args, "provider", None)
        mail_client = mail_client_from_env(
            provider=provider, use_mock=args.mock or None
        )
        mail_svc = MailService(client=mail_client)

        if args.mail_command == "send":
            result = mail_svc.send(
                to=args.to,
                subject=args.subject,
                body=args.body,
                cc=args.cc or None,
                html_body=args.html_body or None,
                site=args.site,
                actor=args.actor,
            )
            return _print(result)

        if args.mail_command == "fetch":
            result = mail_svc.fetch(
                folder=args.folder,
                unread_only=not args.all,
                limit=args.limit,
                site=args.site,
                actor=args.actor,
            )
            return _print(result)

        if args.mail_command == "reply":
            result = mail_svc.reply(
                to=args.to,
                subject=args.subject,
                body=args.body,
                original_uid=args.original_uid,
                html_body=args.html_body or None,
                site=args.site,
                actor=args.actor,
            )
            return _print(result)

        parser.error(f"Unknown mail command: {args.mail_command}")
        return 2

    if args.command == "cases":
        from ecom_ops.cases.service import CaseService

        case_svc = CaseService()
        if args.cases_command == "poll":
            result = case_svc.poll(
                limit_per_mailbox=args.limit,
                actor=args.actor,
                use_mock=args.mock or None,
            )
            return _print(result)
        if args.cases_command == "list":
            cases = case_svc.store.list_cases(
                status=args.status or None, limit=args.limit
            )
            payload = {"ok": True, "count": len(cases), "cases": [c.to_dict() for c in cases]}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.cases_command == "show":
            case = case_svc.get(args.case_id)
            if not case:
                print(json.dumps({"ok": False, "message": "not found"}, indent=2))
                return 1
            msgs = [m.to_dict() for m in case_svc.store.messages(args.case_id)]
            print(
                json.dumps(
                    {"ok": True, "case": case.to_dict(), "messages": msgs},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.cases_command == "reply":
            result = case_svc.approve_and_send(
                args.case_id,
                actor=args.actor,
                body_override=args.body,
            )
            return _print(result)
        if args.cases_command == "close":
            result = case_svc.close(
                args.case_id,
                actor=args.actor,
                reason=args.reason or None,
            )
            return _print(result)
        if args.cases_command == "draft":
            result = case_svc.save_draft(
                args.case_id,
                args.body,
                actor=args.actor,
            )
            return _print(result)
        if args.cases_command == "regenerate":
            result = case_svc.regenerate_draft(
                args.case_id,
                actor=args.actor,
                use_mock=args.mock or None,
            )
            return _print(result)
        if args.cases_command == "shadow-report":
            from ecom_ops.cases.shadow_report import build_shadow_report
            from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor

            try:
                require_permission(resolve_actor(args.actor), Permission.ADMIN)
            except AccessDenied as exc:
                return _print(
                    {
                        "ok": False,
                        "error": "access_denied",
                        "message": str(exc),
                    }
                )
            report = build_shadow_report(days=int(getattr(args, "days", 7) or 7))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.cases_command in {"calibration-export", "calibration-report"}:
            from ecom_ops.cases.calibration import (
                build_calibration_report,
                export_calibration_samples,
            )
            from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor

            try:
                require_permission(resolve_actor(args.actor), Permission.ADMIN)
            except AccessDenied as exc:
                return _print(
                    {
                        "ok": False,
                        "error": "access_denied",
                        "message": str(exc),
                    }
                )
            days = int(getattr(args, "days", 30) or 30)
            if args.cases_command == "calibration-export":
                payload = export_calibration_samples(days=days)
            else:
                payload = build_calibration_report(days=days)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.cases_command == "retention-purge":
            from ecom_ops.cases.retention import purge_closed_cases
            from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor

            try:
                require_permission(resolve_actor(args.actor), Permission.ADMIN)
            except AccessDenied as exc:
                return _print(
                    {
                        "ok": False,
                        "error": "access_denied",
                        "message": str(exc),
                    }
                )

            if args.dry_run:
                from ecom_ops.cases.retention import count_eligible_cases

                days = int(args.days or 90)
                eligible_n = count_eligible_cases(retention_days=days)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "dry_run": True,
                            "eligible": eligible_n,
                            "retention_days": days,
                            "message": f"Dry run: {eligible_n} cases eligible",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            result = purge_closed_cases(
                retention_days=args.days,
                redact=args.redact,
            )
            return _print(result)
        if args.cases_command == "research":
            import inspect

            try:
                init_params = inspect.signature(CaseService.__init__).parameters
                svc_for_res = (
                    CaseService(actor=args.actor)
                    if "actor" in init_params
                    else case_svc
                )
                fn = svc_for_res.start_case_research
                fn_params = inspect.signature(fn).parameters
                res_kwargs: dict[str, Any] = {
                    "query": args.query,
                    "depth": args.depth,
                    "wait": bool(args.wait),
                }
                if "actor" in fn_params:
                    res_kwargs["actor"] = args.actor
                res = fn(args.case_id, **res_kwargs)
                return _print(res)
            except Exception as exc:
                return _cli_error_result(exc)
        parser.error(f"Unknown cases command: {args.cases_command}")
        return 2

    if args.command == "marketing":
        from ecom_ops.actions.marketing import MarketingService

        mkt = MarketingService(use_mock=args.mock or None)
        cmd = args.marketing_command
        if cmd == "digest":
            return _print(mkt.digest(days=args.days, actor=args.actor))
        if cmd == "health":
            return _print(mkt.health(actor=args.actor))
        if cmd == "waste":
            return _print(mkt.waste(days=args.days, actor=args.actor))
        if cmd == "pacing":
            return _print(mkt.pacing(actor=args.actor))
        if cmd == "consistency":
            return _print(
                mkt.consistency(
                    days=args.days,
                    woo_purchases=args.woo_purchases,
                    actor=args.actor,
                )
            )
        if cmd == "mer":
            return _print(
                mkt.mer(
                    days=args.days,
                    woo_revenue=args.woo_revenue,
                    actor=args.actor,
                )
            )
        if cmd == "snapshot":
            return _print(mkt.snapshot(actor=args.actor))
        if cmd == "suggests":
            sc = args.suggests_command
            if sc == "build":
                return _print(mkt.build_waste_suggests(actor=args.actor))
            if sc == "list":
                return _print(
                    mkt.list_suggests(status=args.status, actor=args.actor)
                )
            if sc == "deny":
                return _print(
                    mkt.deny_suggest(args.suggest_id, actor=args.actor)
                )
            if sc == "approve":
                return _print(
                    mkt.approve_and_mutate(args.suggest_id, actor=args.actor)
                )
            parser.error(f"Unknown marketing suggests command: {sc}")
            return 2
        if cmd == "mp-queue":
            from ecom_ops.actions.marketing import MarketingResult

            try:
                params = json.loads(args.payload_json or "{}")
            except json.JSONDecodeError:
                return _print(
                    MarketingResult(ok=False, message="Invalid --payload-json")
                )
            if not isinstance(params, dict):
                return _print(
                    MarketingResult(ok=False, message="payload must be object")
                )
            return _print(
                mkt.queue_mp_event(
                    {"name": args.name, "params": params},
                    actor=args.actor,
                )
            )
        if cmd == "merchant-queue":
            product = {"offerId": args.offer_id, "title": args.title or args.offer_id}
            return _print(
                mkt.queue_merchant_write(
                    product, op=args.op, actor=args.actor
                )
            )
        if cmd == "research":
            import inspect

            try:
                init_params = inspect.signature(MarketingService.__init__).parameters
                mkt_for_res = (
                    MarketingService(use_mock=args.mock or None, actor=args.actor)
                    if "actor" in init_params
                    else mkt
                )
                fn = mkt_for_res.research_topic
                fn_params = inspect.signature(fn).parameters
                res_kwargs: dict[str, Any] = {
                    "depth": args.depth,
                    "wait": bool(args.wait),
                }
                if "actor" in fn_params:
                    res_kwargs["actor"] = args.actor
                return _print(fn(args.topic, **res_kwargs))
            except Exception as exc:
                return _cli_error_result(exc)
        parser.error(f"Unknown marketing command: {cmd}")
        return 2

    if args.command == "wp":
        from ecom_ops.wp_ops import WpOpsService

        wp = WpOpsService()
        cmd = args.wp_command
        site_arg = getattr(args, "wp_site_id", None) or getattr(args, "site", None)
        if cmd == "sites":
            return _print(wp.list_sites(actor=args.actor))
        if cmd == "status":
            return _print(wp.list_sites(actor=args.actor))
        if cmd == "show":
            return _print(wp.show_site(site_arg, actor=args.actor))
        if cmd == "check":
            if args.due and args.wp_site_id:
                parser.error("wp check accepts --id or --due, not both")
                return 2
            if args.due:
                return _print(wp.run_due(actor=args.actor))
            if not args.wp_site_id:
                parser.error("wp check requires --id or --due")
                return 2
            return _print(wp.run_checks(args.wp_site_id, actor=args.actor))
        if cmd == "backup":
            return _print(wp.create_backup(site_arg, actor=args.actor))
        if cmd == "backups":
            return _print(wp.list_backups(site_arg, actor=args.actor))
        if cmd == "restore-drill":
            return _print(
                wp.restore_drill(args.backup_id, target_site_id=args.target_site_id, actor=args.actor)
            )
        if cmd == "files":
            if args.slug:
                return _print(
                    wp.list_files(site_arg, args.kind, args.slug, actor=args.actor)
                )
            return _print(wp.list_components(site_arg, args.kind, actor=args.actor))
        if cmd in {"file-propose", "file-apply"}:
            text, error_class = _load_edit_source(args.from_file)
            if text is None:
                return _print(
                    _WpCliError(error_class or "NotText", "Edit source is unreadable.")
                )
            if cmd == "file-propose":
                return _print(
                    wp.propose_file(
                        site_arg,
                        args.kind,
                        args.slug,
                        args.relative_path,
                        text,
                        actor=args.actor,
                    )
                )
            return _print(wp.apply_file(site_arg, args.plan_id, text, actor=args.actor))
        if cmd == "changes":
            import inspect

            batch_filter = getattr(args, "batch_id", None)
            params = inspect.signature(wp.list_changes).parameters
            if batch_filter and "batch_id" in params:
                res = wp.list_changes(
                    args.wp_site_id,
                    actor=args.actor,
                    category=args.category,
                    status=args.status,
                    batch_id=batch_filter,
                    limit=args.limit,
                )
            else:
                res = wp.list_changes(
                    args.wp_site_id,
                    actor=args.actor,
                    category=args.category,
                    status=args.status,
                    limit=200 if batch_filter else args.limit,
                )
                if batch_filter and res.ok and isinstance(res.data, dict):
                    rows_all = list(res.data.get("changes") or [])
                    res.data["changes"] = [
                        r
                        for r in rows_all
                        if isinstance(r, dict)
                        and (
                            str(r.get("batch_id") or "") == batch_filter
                            or str((r.get("metadata") or {}).get("batch_id") or "") == batch_filter
                        )
                    ][: args.limit]
            payload = res.to_dict() if hasattr(res, "to_dict") else res
            rows = list((payload.get("data") or {}).get("changes") or []) if isinstance(payload, dict) else []
            return _print_or_table(
                payload,
                args=args,
                rows=rows,
                columns=[
                    ("change_id", "CHANGE_ID"),
                    ("category", "CATEGORY"),
                    ("batch_id", "BATCH_ID"),
                    ("status", "STATUS"),
                    ("summary", "SUMMARY"),
                ],
            )
        if cmd == "change-show":
            return _print(
                wp.preview_change_rollback(site_arg, args.change_id, actor=args.actor)
            )
        if cmd == "rollback":
            if args.execute:
                if not args.expected_sha256:
                    parser.error("wp rollback --execute requires --expected-sha256")
                    return 2
                return _print(
                    wp.execute_change_rollback(
                        site_arg,
                        args.change_id,
                        actor=args.actor,
                        expected_before_sha256=args.expected_sha256,
                    )
                )
            return _print(
                wp.preview_change_rollback(site_arg, args.change_id, actor=args.actor)
            )
        if cmd == "rollback-batch":
            from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

            rsvc = ChangeRollbackService(
                data_dir=wp.data_dir, registry_path=wp.registry_path
            )
            if args.execute:
                fn = getattr(rsvc, "execute_batch_rollback", None)
                if not callable(fn):
                    return _print(
                        _WpCliError("NotImplemented", "Batch rollback is not available.")
                    )
                confirm = (args.confirm_batch_id or args.batch_id).strip()
                return _print(
                    fn(
                        site_arg,
                        args.batch_id,
                        actor=args.actor,
                        confirm_batch_id=confirm,
                    )
                )
            fn = getattr(rsvc, "preview_batch_rollback", None)
            if not callable(fn):
                return _print(
                    _WpCliError("NotImplemented", "Batch rollback preview is not available.")
                )
            return _print(fn(site_arg, args.batch_id, actor=args.actor))
        if cmd == "bridge-health":
            return _print(wp.bridge_health(site_arg, actor=args.actor))
        if cmd == "cache-flush":
            return _print(wp.bridge_cache_flush(site_arg, actor=args.actor))
        if cmd == "bridge-maintenance":
            return _print(
                wp.bridge_maintenance(
                    site_arg,
                    enabled=args.enabled in {"1", "on"},
                    duration_seconds=args.duration,
                    actor=args.actor,
                )
            )
        if cmd == "bridge-translations":
            return _print(
                wp.bridge_translations(site_arg, post_id=args.post_id, actor=args.actor)
            )
        if cmd == "deploy-live":
            from ecom_ops.wp_ops.deploy_orchestrator import DeployOrchestrator

            return _print(
                DeployOrchestrator(
                    data_dir=wp.data_dir, registry_path=wp.registry_path
                ).deploy_workspace(
                    site_arg,
                    args.plan_id,
                    actor=args.actor,
                    base_sha256=args.base_sha256,
                    work_sha256=args.work_sha256,
                    transport_mode=args.transport,
                )
            )
        if cmd == "plugins-inventory":
            return _print(
                wp.plugin_inventory(
                    site_arg,
                    actor=args.actor,
                    include_updates=True,
                    include_wporg=bool(args.wporg),
                )
            )
        if cmd == "plugins-mu":
            return _print(wp.mu_plugins_and_dropins(site_arg, actor=args.actor))
        if cmd == "plugins-plans":
            res = wp.list_plugin_update_plans(site_arg, actor=args.actor)
            payload = res.to_dict() if hasattr(res, "to_dict") else res
            rows = list((payload.get("data") or {}).get("plans") or []) if isinstance(payload, dict) else []
            return _print_or_table(
                payload,
                args=args,
                rows=rows,
                columns=[
                    ("plan_id", "PLAN_ID"),
                    ("selection_mode", "MODE"),
                    ("policy", "POLICY"),
                    ("status", "STATUS"),
                    ("created_at", "CREATED_AT"),
                ],
            )
        if cmd == "plugins-plan-update":
            slugs = [s.strip() for s in str(args.slugs or "").split(",") if s.strip()] or None
            exclude = [s.strip() for s in str(args.exclude or "").split(",") if s.strip()] or None
            return _print(
                wp.plan_plugin_updates(
                    site_arg,
                    actor=args.actor,
                    selection_mode=args.mode,
                    slugs=slugs,
                    policy=args.policy,
                    exclude_slugs=exclude,
                    include_critical=bool(args.include_critical),
                )
            )
        if cmd == "plugins-update":
            return _print(
                wp.execute_plugin_update_plan(
                    site_arg,
                    args.plan_id,
                    actor=args.actor,
                    expected_sha256=args.expected_sha256,
                    allow_critical=bool(args.allow_critical),
                )
            )
        if cmd == "plugins-autoupdate":
            if args.execute:
                plan_id = (
                    args.autoupdate_plan
                    if isinstance(args.autoupdate_plan, str)
                    and args.autoupdate_plan != "__CREATE__"
                    else ""
                )
                if not plan_id or not args.expected_sha256:
                    parser.error(
                        "wp plugins-autoupdate --execute requires --plan <pau-...> and --expected-sha256"
                    )
                    return 2
                return _print(
                    wp.execute_plugin_autoupdate_plan(
                        site_arg,
                        plan_id,
                        actor=args.actor,
                        expected_sha256=args.expected_sha256,
                    )
                )
            if args.autoupdate_plan is not None and args.autoupdate_plan != "__CREATE__":
                parser.error(
                    "wp plugins-autoupdate --plan <pau-...> requires --execute and --expected-sha256"
                )
                return 2
            if args.autoupdate_plan == "__CREATE__":
                changes: list[dict[str, Any]] | None = None
                if str(args.changes_json or "").strip():
                    try:
                        parsed = json.loads(args.changes_json)
                    except json.JSONDecodeError:
                        return _print(
                            _WpCliError("InvalidInput", "Invalid --changes-json payload.")
                        )
                    if not isinstance(parsed, list):
                        return _print(
                            _WpCliError("InvalidInput", "--changes-json must be a JSON list.")
                        )
                    changes = parsed
                return _print(
                    wp.plan_plugin_autoupdate(
                        site_arg,
                        actor=args.actor,
                        preset=args.preset or "non_critical_only",
                        changes=changes,
                        allow_critical=bool(args.allow_critical),
                    )
                )
            if (
                args.preset is not None
                or str(args.changes_json or "").strip()
                or args.allow_critical
            ):
                parser.error(
                    "wp plugins-autoupdate --preset/--changes-json/--allow-critical requires --plan"
                )
                return 2
            if args.drift:
                return _print(wp.check_autoupdate_drift(site_arg, actor=args.actor))
            return _print(wp.get_autoupdate_policy(site_arg, actor=args.actor))
        if cmd == "plugins-diagnostics":
            hooks = [h.strip() for h in str(args.hooks or "").split(",") if h.strip()] or None
            return _print(
                wp.plugin_diagnostics(
                    site_arg,
                    actor=args.actor,
                    hooks=hooks,
                    log_lines=int(args.log_lines),
                )
            )
        if cmd == "plugins-quarantine":
            return _print(
                wp.quarantine_plugin(
                    site_arg,
                    args.slug,
                    actor=args.actor,
                    kind=args.kind,
                )
            )
        if cmd == "plugins-unquarantine":
            return _print(
                wp.unquarantine_plugin(
                    site_arg,
                    args.slug,
                    actor=args.actor,
                    quarantine_name=args.quarantine_name,
                    kind=args.kind,
                )
            )
        if cmd == "plugins-recovery":
            if args.execute:
                if not args.plan_id or not args.expected_sha256:
                    parser.error(
                        "wp plugins-recovery --execute requires --plan <plr-...> and --expected-sha256"
                    )
                    return 2
                return _print(
                    wp.execute_plugin_recovery_plan(
                        site_arg,
                        args.plan_id,
                        actor=args.actor,
                        expected_sha256=args.expected_sha256,
                    )
                )
            if not args.slug or not args.action:
                parser.error("wp plugins-recovery requires --slug and --action (or --execute --plan --expected-sha256)")
                return 2
            return _print(
                wp.plan_plugin_recovery(
                    site_arg,
                    actor=args.actor,
                    slug=args.slug,
                    action=args.action,
                    change_id=args.change_id,
                )
            )
        parser.error(f"Unknown wp command: {cmd}")
        return 2

    if args.command == "ops":
        cmd = args.ops_command
        if cmd == "status":
            from ecom_ops.runtime_health import (
                collect_runtime_snapshot,
                format_ops_telegram_summary,
            )

            snap = collect_runtime_snapshot()
            if args.as_json:
                print(json.dumps(snap, ensure_ascii=False, indent=2))
            else:
                print(format_ops_telegram_summary(snap))
            return 0
        if cmd == "gc":
            from ecom_ops.ops_gc import run_ops_gc
            from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor

            if not args.dry_run:
                try:
                    require_permission(resolve_actor(args.actor), Permission.ADMIN)
                except AccessDenied as exc:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "action": "ops_gc",
                                "error": "access_denied",
                                "message": str(exc),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 1
            res = run_ops_gc(
                dry_run=bool(args.dry_run),
                artifact_grace_hours=args.grace_hours,
                pv_max_age_hours=args.pv_max_age_hours,
                inbox_max_age_hours=args.inbox_max_age_hours,
                proposal_max_age_hours=args.proposal_max_age_hours,
                rotate_max_mb=args.rotate_max_mb,
            )
            if args.as_json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                mode = "DRY-RUN" if res["dry_run"] else "EXECUTED"
                print(
                    f"ops gc ({mode}): expired_plans={res['expired_plans']} "
                    f"workspaces={res['removed_workspace_dirs']} "
                    f"pv_dirs={res['removed_pv_dirs']} "
                    f"media={res['removed_media_files']} "
                    f"proposals={res['purged_proposals']} "
                    f"search_cache={res['purged_search_cache_rows']} "
                    f"research_artifacts={res['removed_research_artifacts']} "
                    f"shop_intel={res.get('purged_shop_intel_runs', 0)} "
                    f"rotated={len(res['rotated_logs'])} "
                    f"reclaimed={res['mb_reclaimed']:.2f} MB "
                    f"errors={len(res['errors'])}"
                )
            return 0 if res.get("ok") else 1
        parser.error(f"Unknown ops command: {cmd}")
        return 2

    if args.command == "systemcheck":
        from ecom_ops.healthcheck import format_report, run_systemcheck

        eff_mode = "mock" if (_mock_mode() and args.mode == "auto") else args.mode
        report = run_systemcheck(
            mode=eff_mode,
            domain=args.domain,
            expected_ip=args.expected_ip,
            check_network=bool(args.check_network),
            check_ports=not bool(args.no_port_check),
            strict=bool(args.strict),
        )
        if args.as_json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(format_report(report))
        return report.exit_code(strict=bool(args.strict))

    if args.command == "healthcheck":
        from ecom_ops.healthcheck import format_report, run_healthcheck

        eff_mode = "mock" if (_mock_mode() and args.mode == "auto") else args.mode
        report = run_healthcheck(
            mode=eff_mode,
            local_url=args.local_url,
            public_url=args.public_url,
            no_http=bool(args.no_http),
            check_public=bool(args.check_public),
            strict=bool(args.strict),
            timeout_sec=float(args.timeout),
        )
        if args.as_json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(format_report(report))
        return report.exit_code(strict=bool(args.strict))

    if args.command == "search":
        from ecom_ops.integrations.search.errors import SearchRouteError
        from ecom_ops.rbac import AccessDenied
        from ecom_ops.research import SearchPolicyError, get_research_service

        try:
            svc = get_research_service()
            res = svc.quick_search(
                args.query,
                actor=args.actor,
                profile_id=args.profile,
                locale=args.locale,
                freshness_days=args.freshness_days,
                max_results=args.max_results,
            )
            return _print(res)
        except (AccessDenied, SearchRouteError, SearchPolicyError, KeyError, ValueError) as exc:
            return _cli_error_result(exc)

    if args.command == "fetch-url":
        from ecom_ops.integrations.search.errors import SearchRouteError
        from ecom_ops.rbac import AccessDenied
        from ecom_ops.research import SearchPolicyError, get_research_service

        try:
            svc = get_research_service()
            res = svc.fetch_extract(
                args.url,
                actor=args.actor,
                profile_id=args.profile,
                max_chars=args.max_chars,
            )
            return _print(res)
        except (AccessDenied, SearchRouteError, SearchPolicyError, KeyError, ValueError) as exc:
            return _cli_error_result(exc)

    if args.command == "research":
        from ecom_ops.integrations.search.errors import SearchRouteError
        from ecom_ops.rbac import AccessDenied
        from ecom_ops.research import SearchPolicyError, get_research_service

        svc = get_research_service()
        rcmd = args.research_command
        try:
            if rcmd == "start":
                job = svc.start_research(
                    args.topic,
                    actor=args.actor,
                    profile_id=args.profile,
                    depth=args.depth,
                    markets=[args.market] if args.market else None,
                    country=args.market,
                    target_ref=args.target_ref,
                    start_worker=False,
                )
                if args.wait:
                    import ecom_ops.bot.progress_events  # noqa: F401  # break bot<->research cycle
                    from ecom_ops.research.engine import ResearchEngine

                    engine = ResearchEngine(
                        store=svc.store,
                        search_router=svc.search_router,
                        fetcher=svc.fetcher,
                        telemetry=svc.telemetry,
                    )
                    job = engine.run_job(job.research_id)
                    brief_res = svc.get_brief(job.research_id, actor=args.actor)
                    return _print(
                        {
                            "ok": job.status in {"completed", "partial"},
                            "research_id": job.research_id,
                            "status": job.status,
                            "job": job.to_dict(),
                            "brief": brief_res.get("brief") if brief_res.get("ok") else None,
                            "sources": brief_res.get("sources") if brief_res.get("ok") else [],
                        }
                    )
                return _print(
                    {
                        "ok": True,
                        "research_id": job.research_id,
                        "status": job.status,
                        "job": job.to_dict(),
                    }
                )
            if rcmd == "list":
                jobs = svc.list_jobs(
                    actor=args.actor,
                    status=args.status,
                    limit=args.limit,
                )
                return _print(
                    {
                        "ok": True,
                        "count": len(jobs),
                        "jobs": [j.to_dict() for j in jobs],
                    }
                )
            if rcmd == "show":
                brief_res = svc.get_brief(args.research_id, actor=args.actor)
                if not brief_res.get("ok"):
                    return _print(brief_res)
                if args.section == "summary":
                    brief = brief_res.get("brief") or {}
                    return _print(
                        {
                            "ok": True,
                            "research_id": args.research_id,
                            "section": "summary",
                            "evidence_status": brief.get("evidence_status"),
                            "summary": brief.get("summary", ""),
                            "recommendations": brief.get("recommendations", []),
                            "gaps": brief.get("gaps", []),
                        }
                    )
                if args.section == "sources":
                    brief = brief_res.get("brief") or {}
                    return _print(
                        {
                            "ok": True,
                            "research_id": args.research_id,
                            "section": "sources",
                            "sources": brief_res.get("sources", []),
                            "citations": brief.get("citations", []),
                        }
                    )
                return _print(brief_res)
            if rcmd == "status":
                return _print(svc.get_status(args.research_id, actor=args.actor))
            if rcmd == "steer":
                return _print(svc.steer_research(args.research_id, args.note, actor=args.actor))
            if rcmd == "cancel":
                return _print(svc.cancel_research(args.research_id, actor=args.actor))
            if rcmd == "profiles":
                from ecom_ops.rbac import Permission, require_permission, resolve_actor
                from ecom_ops.research.profiles import (
                    get_profile,
                    list_profiles,
                    recommend_profile,
                )

                actor_obj = resolve_actor(args.actor)
                require_permission(actor_obj, Permission.WEB_SEARCH)
                if getattr(args, "recommend_goal", None):
                    rec = recommend_profile(
                        args.recommend_goal,
                        ambition=getattr(args, "ambition", None),
                    )
                    return _print(
                        {
                            "ok": True,
                            "mode": "recommend",
                            "recommended": rec.to_dict(),
                        }
                    )
                if getattr(args, "profile_id", None):
                    prof = get_profile(args.profile_id)
                    return _print(
                        {
                            "ok": True,
                            "mode": "detail",
                            "profile": prof.to_dict(),
                        }
                    )
                profs = list_profiles()
                amb = getattr(args, "ambition", None)
                if amb:
                    profs = [p for p in profs if p.ambition == amb]
                return _print(
                    {
                        "ok": True,
                        "count": len(profs),
                        "profiles": [p.to_dict() for p in profs],
                    }
                )
        except (AccessDenied, SearchRouteError, SearchPolicyError, KeyError, ValueError) as exc:
            return _cli_error_result(exc)
        parser.error(f"Unknown research command: {rcmd}")
        return 2

    if args.command == "seo":
        from ecom_ops.rbac import AccessDenied
        from ecom_ops.seo.service import SeoStudioService

        svc = SeoStudioService()
        scmd = args.seo_command
        seo_site = getattr(args, "seo_site_id", None) or args.site
        try:
            if scmd == "audit":
                res = svc.run_audit(
                    site_id=seo_site,
                    actor=args.actor,
                    scope=args.scope,
                    target_id=args.target_id,
                    path=args.path,
                    market=args.market,
                    limit=args.limit,
                )
                return _print(res)
            if scmd == "audits":
                from ecom_ops.seo.policy import require_seo_read

                require_seo_read(args.actor)
                site_filter = getattr(args, "seo_site_id", None)
                audits = svc.list_audits(site_id=site_filter, limit=args.limit)
                payload = {"ok": True, "count": len(audits), "audits": audits}
                return _print_or_table(
                    payload,
                    args=args,
                    rows=audits,
                    columns=[
                        ("audit_id", "AUDIT_ID"),
                        ("site_id", "SITE"),
                        ("market", "MARKET"),
                        ("scope", "SCOPE"),
                        ("score", "SCORE"),
                        ("grade", "GRADE"),
                        ("created_at", "CREATED_AT"),
                    ],
                )
            if scmd == "fixes":
                from ecom_ops.seo.policy import require_seo_read

                require_seo_read(args.actor)
                site_filter = getattr(args, "seo_site_id", None)
                status_filter = (getattr(args, "status", None) or "").strip().lower()
                fixes = svc.list_fix_plans(
                    site_id=site_filter,
                    limit=max(args.limit, 100) if status_filter else args.limit,
                )
                if status_filter:
                    fixes = [
                        fp
                        for fp in fixes
                        if isinstance(fp, dict)
                        and str(fp.get("status") or "").lower() == status_filter
                    ][: args.limit]
                payload = {"ok": True, "count": len(fixes), "fix_plans": fixes}
                return _print_or_table(
                    payload,
                    args=args,
                    rows=fixes,
                    columns=[
                        ("plan_id", "PLAN_ID"),
                        ("site_id", "SITE"),
                        ("fix_type", "TYPE"),
                        ("target_type", "TARGET"),
                        ("target_id", "ID"),
                        ("status", "STATUS"),
                        ("summary", "SUMMARY"),
                    ],
                )
            if scmd == "batches":
                from ecom_ops.seo.policy import require_seo_read

                require_seo_read(args.actor)
                site_filter = getattr(args, "seo_site_id", None)
                fn = getattr(svc, "list_fix_batches", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Fix batches unavailable."})
                batches = fn(site_id=site_filter, limit=args.limit)
                payload = {"ok": True, "count": len(batches), "batches": batches}
                return _print_or_table(
                    payload,
                    args=args,
                    rows=batches,
                    columns=[
                        ("batch_id", "BATCH_ID"),
                        ("site_id", "SITE"),
                        ("scope", "SCOPE"),
                        ("items", "ITEMS"),
                        ("status", "STATUS"),
                        ("created_at", "CREATED_AT"),
                    ],
                )
            if scmd == "batch-propose":
                fn = getattr(svc, "propose_batch_from_audit", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Batch propose unavailable."})
                res = fn(
                    site_id=seo_site,
                    audit_id=args.audit_id,
                    scope=args.scope,
                    limit=args.limit,
                    actor=args.actor,
                )
                return _print(res)
            if scmd == "batch-apply":
                fn = getattr(svc, "apply_batch", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Batch apply unavailable."})
                res = fn(
                    site_id=seo_site,
                    batch_id=args.batch_id,
                    expected_sha256=args.expected_sha256,
                    actor=args.actor,
                )
                return _print(res)
            if scmd == "gsc":
                fn = getattr(svc, "get_gsc_quick_wins", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "GSC quick-wins unavailable."})
                res = fn(site_id=seo_site, days=args.days, actor=args.actor)
                rows = list(res.get("strike_distance") or []) if isinstance(res, dict) else []
                return _print_or_table(
                    res,
                    args=args,
                    rows=rows,
                    columns=[
                        ("query", "QUERY"),
                        ("page", "PAGE"),
                        ("position", "POS"),
                        ("impressions", "IMPR"),
                        ("clicks", "CLICKS"),
                        ("ctr", "CTR"),
                    ],
                )
            if scmd == "parity":
                fn = getattr(svc, "run_nordic_parity", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Nordic parity unavailable."})
                res = fn(site_id=seo_site, actor=args.actor)
                rows = list(res.get("issues") or []) if isinstance(res, dict) else []
                return _print_or_table(
                    res,
                    args=args,
                    rows=rows,
                    columns=[
                        ("severity", "SEV"),
                        ("market", "MARKET"),
                        ("code", "CODE"),
                        ("title", "TITLE"),
                    ],
                )
            if scmd == "diff":
                fn = getattr(svc, "diff_audits", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Audit diff unavailable."})
                res = fn(
                    site_id=seo_site,
                    latest_audit_id=args.latest_audit_id,
                    previous_audit_id=args.previous_audit_id,
                    actor=args.actor,
                )
                return _print(res)
            if scmd == "digest":
                from ecom_ops.seo.digest import build_seo_plugin_drift_digest

                res = build_seo_plugin_drift_digest(
                    seo_site, actor=args.actor, days=args.days
                )
                if getattr(args, "markdown", False) and isinstance(res, dict) and res.get("ok"):
                    print(str(res.get("markdown") or ""))
                    return 0
                return _print(res)
            if scmd == "fix-propose":
                try:
                    proposed = json.loads(args.json_fields)
                except json.JSONDecodeError as exc:
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidJson",
                            "message": f"Invalid --json-fields JSON: {exc}",
                        }
                    )
                if not isinstance(proposed, dict) or not proposed:
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--json-fields must be a non-empty JSON object",
                        }
                    )
                res = svc.propose_fix(
                    site_id=seo_site,
                    actor=args.actor,
                    fix_type=args.fix_type,
                    target_type=args.target_type,
                    target_id=args.target_id,
                    proposed=proposed,
                    summary=args.summary,
                    market=args.market,
                )
                return _print(res)
            if scmd == "fix-apply":
                res = svc.apply_fix(
                    site_id=seo_site,
                    plan_id=args.plan_id,
                    actor=args.actor,
                    expected_sha256=args.expected_sha256,
                )
                return _print(res)
            if scmd == "strategy":
                topics = [t.strip() for t in (args.topics or "").split(",") if t.strip()]
                res = svc.create_strategy(
                    site_id=seo_site,
                    actor=args.actor,
                    title=args.title,
                    market=args.market,
                    focus_topics=topics or None,
                    goals=args.goals,
                    research_brief_id=args.research_brief_id,
                )
                return _print(res)
            if scmd == "campaign":
                keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
                urls = [u.strip() for u in (args.urls or "").split(",") if u.strip()]
                res = svc.create_campaign(
                    site_id=seo_site,
                    actor=args.actor,
                    name=args.name,
                    market=args.market,
                    strategy_id=args.strategy_id,
                    target_keywords=keywords or None,
                    target_urls=urls or None,
                    campaign_type=args.campaign_type,
                    objective=args.objective,
                    research_brief_id=args.research_brief_id,
                )
                return _print(res)
            if scmd == "link-pack":
                if (
                    not math.isfinite(args.budget_sek)
                    or args.budget_sek < 1000.0
                    or args.budget_sek > 500000.0
                ):
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--budget must be a finite number between 1000 and 500000 SEK",
                        }
                    )
                if args.link_count < 1 or args.link_count > 50:
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--links must be an integer between 1 and 50",
                        }
                    )
                targets: list[dict[str, Any]] | None = None
                raw_targets = str(getattr(args, "targets_json", "") or "").strip()
                if raw_targets:
                    try:
                        parsed_targets = json.loads(raw_targets)
                    except json.JSONDecodeError as exc:
                        return _print(
                            {
                                "ok": False,
                                "error_class": "InvalidJson",
                                "message": f"Invalid --targets-json JSON: {exc}",
                            }
                        )
                    if not isinstance(parsed_targets, list) or not all(
                        isinstance(item, dict) for item in parsed_targets
                    ):
                        return _print(
                            {
                                "ok": False,
                                "error_class": "InvalidInput",
                                "message": "--targets-json must be a JSON list of target objects",
                            }
                        )
                    targets = parsed_targets
                res = svc.create_link_package(
                    site_id=seo_site,
                    actor=args.actor,
                    name=args.name,
                    market=args.market,
                    campaign_id=args.campaign_id,
                    budget_sek=args.budget_sek,
                    link_count=args.link_count,
                    targets=targets,
                    notes=args.notes,
                )
                if args.export_fmt and res.get("ok"):
                    pkg_id = str((res.get("package") or {}).get("package_id") or "")
                    if pkg_id:
                        filename, mimetype, content = svc.export_link_package(
                            pkg_id, fmt=args.export_fmt
                        )
                        res = dict(res)
                        res["export"] = {
                            "format": args.export_fmt,
                            "mimetype": mimetype,
                            "filename": filename,
                            "content": content,
                        }
                return _print(res)
            if scmd == "link-pack-status":
                res = svc.transition_link_package(
                    package_id=args.package_id,
                    actor=args.actor,
                    new_status=args.new_status,
                    note=args.note,
                )
                return _print(res)
            if scmd == "link-packs":
                from ecom_ops.seo.policy import require_seo_read

                require_seo_read(args.actor)
                site_filter = getattr(args, "seo_site_id", None)
                pkgs = svc.list_link_packages(site_id=site_filter, limit=args.limit)
                payload = {"ok": True, "count": len(pkgs), "link_packages": pkgs}
                return _print_or_table(
                    payload,
                    args=args,
                    rows=pkgs,
                    columns=[
                        ("package_id", "PACKAGE_ID"),
                        ("site_id", "SITE"),
                        ("market", "MARKET"),
                        ("budget_sek", "BUDGET_SEK"),
                        ("link_count", "LINKS"),
                        ("status", "STATUS"),
                        ("name", "NAME"),
                    ],
                )
            if scmd == "link-pack-edit":
                fn = getattr(svc, "update_link_package_draft", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Link-pack edit unavailable."})
                if args.budget_sek is not None and (
                    not math.isfinite(args.budget_sek)
                    or args.budget_sek < 1000.0
                    or args.budget_sek > 500000.0
                ):
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--budget must be a finite number between 1000 and 500000 SEK",
                        }
                    )
                targets_edit: list[dict[str, Any]] | None = None
                raw_tj = str(getattr(args, "targets_json", "") or "").strip()
                if raw_tj:
                    try:
                        parsed_t = json.loads(raw_tj)
                    except json.JSONDecodeError as exc:
                        return _print(
                            {
                                "ok": False,
                                "error_class": "InvalidJson",
                                "message": f"Invalid --targets-json JSON: {exc}",
                            }
                        )
                    if not isinstance(parsed_t, list) or not all(
                        isinstance(x, dict) for x in parsed_t
                    ):
                        return _print(
                            {
                                "ok": False,
                                "error_class": "InvalidInput",
                                "message": "--targets-json must be a JSON list of target objects",
                            }
                        )
                    targets_edit = parsed_t
                res = fn(
                    args.package_id,
                    name=args.name,
                    budget_sek=args.budget_sek,
                    targets=targets_edit,
                    notes=args.notes,
                    actor=args.actor,
                )
                return _print(res)
            if scmd == "link-pack-verify":
                fn = getattr(svc, "verify_link_package", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Link-pack verify unavailable."})
                res = fn(args.package_id, actor=args.actor)
                rows = list(res.get("results") or []) if isinstance(res, dict) else []
                return _print_or_table(
                    res,
                    args=args,
                    rows=rows,
                    columns=[
                        ("published_url", "PUBLISHED_URL"),
                        ("http_status", "HTTP"),
                        ("indexable", "INDEXABLE"),
                        ("anchor_match", "ANCHOR_OK"),
                        ("rel_type", "REL"),
                    ],
                )
            if scmd == "vendors":
                fn = getattr(svc, "vet_link_vendors", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "Vendor vetting unavailable."})
                if (
                    not math.isfinite(args.budget_sek)
                    or args.budget_sek < 1000.0
                    or args.budget_sek > 500000.0
                ):
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--budget must be a finite number between 1000 and 500000 SEK",
                        }
                    )
                if args.link_count < 1 or args.link_count > 50:
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--links must be an integer between 1 and 50",
                        }
                    )
                res = fn(
                    market=args.market,
                    budget_sek=args.budget_sek,
                    link_count=args.link_count,
                    goal=args.goal,
                    include_web_research=not bool(getattr(args, "no_web_research", False)),
                    actor=args.actor,
                )
                candidates = list(res.get("candidates") or []) if isinstance(res, dict) else []
                table_rows: list[dict[str, Any]] = []
                for idx, cand in enumerate(candidates, start=1):
                    if not isinstance(cand, dict):
                        continue
                    unit_rng = cand.get("estimated_unit_sek_range")
                    if isinstance(unit_rng, (list, tuple)) and len(unit_rng) == 2:
                        est_range = f"{unit_rng[0]}-{unit_rng[1]} SEK/link"
                    else:
                        est_range = str(
                            cand.get("estimated_sek_range")
                            or cand.get("price_range_sek")
                            or "-"
                        )
                    table_rows.append(
                        {
                            "rank": f"#{idx}",
                            "vendor_id": cand.get("vendor_id") or "",
                            "name": cand.get("name") or cand.get("vendor_id") or "",
                            "domain": cand.get("domain") or "",
                            "vetting_score": cand.get("vetting_score") if cand.get("vetting_score") is not None else cand.get("score", ""),
                            "vetting_status": cand.get("vetting_status") or cand.get("status") or "",
                            "tier": cand.get("business_model") or cand.get("archetype") or cand.get("tier") or res.get("recommended_tier") or "",
                            "est_sek": est_range or "-",
                        }
                    )
                return _print_or_table(
                    res,
                    args=args,
                    rows=table_rows,
                    columns=[
                        ("rank", "RANK"),
                        ("vendor_id", "VENDOR_ID"),
                        ("name", "SHOP"),
                        ("domain", "DOMAIN"),
                        ("vetting_score", "SCORE"),
                        ("vetting_status", "STATUS"),
                        ("tier", "TIER"),
                        ("est_sek", "EST_SEK"),
                    ],
                )
            if scmd == "autopilot":
                fn = getattr(svc, "run_seo_autopilot", None)
                if not callable(fn):
                    return _print({"ok": False, "error_class": "NotImplemented", "message": "SEO autopilot unavailable."})
                if (
                    not math.isfinite(args.budget_sek)
                    or args.budget_sek < 1000.0
                    or args.budget_sek > 500000.0
                ):
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--budget must be a finite number between 1000 and 500000 SEK",
                        }
                    )
                if args.link_count < 1 or args.link_count > 50:
                    return _print(
                        {
                            "ok": False,
                            "error_class": "InvalidInput",
                            "message": "--links must be an integer between 1 and 50",
                        }
                    )
                res = fn(
                    seo_site,
                    topic=args.topic,
                    market=args.market,
                    budget_sek=args.budget_sek,
                    link_count=args.link_count,
                    include_web_research=not bool(getattr(args, "no_web_research", False)),
                    include_vendor_vetting=True,
                    include_batch_fixes=bool(getattr(args, "with_batch", False)),
                    actor=args.actor,
                )
                if isinstance(res, dict) and res.get("ok"):
                    audit = res.get("audit") or {}
                    strat = res.get("strategy") or {}
                    camp = res.get("campaign") or {}
                    vsel = res.get("vendor_selection") or {}
                    pkg = res.get("link_package") or {}
                    batch = res.get("batch_fix") or {}
                    stage_rows = [
                        {
                            "stage": "audit",
                            "artifact_id": audit.get("audit_id") or "-",
                            "status": f"score={audit.get('score', '-')}",
                            "detail": f"site={res.get('site_id')} market={res.get('market')}",
                        },
                        {
                            "stage": "strategy",
                            "artifact_id": strat.get("strategy_id") or "-",
                            "status": "draft",
                            "detail": strat.get("title") or res.get("topic") or "-",
                        },
                        {
                            "stage": "campaign",
                            "artifact_id": camp.get("campaign_id") or "-",
                            "status": camp.get("campaign_type") or "commercial",
                            "detail": camp.get("name") or "-",
                        },
                        {
                            "stage": "vendor",
                            "artifact_id": vsel.get("selected_vendor_id") or pkg.get("selected_vendor_id") or "-",
                            "status": vsel.get("recommended_tier") or "-",
                            "detail": f"{len(vsel.get('candidates') or [])} vetted shops (HITL)",
                        },
                        {
                            "stage": "link_package",
                            "artifact_id": pkg.get("package_id") or "-",
                            "status": pkg.get("status") or "draft",
                            "detail": f"{int(pkg.get('budget_sek') or args.budget_sek)} SEK / {pkg.get('link_count') or args.link_count} links",
                        },
                    ]
                    if isinstance(batch, dict) and batch.get("batch_id"):
                        stage_rows.append(
                            {
                                "stage": "batch_fix",
                                "artifact_id": batch.get("batch_id") or "-",
                                "status": batch.get("status") or "pending",
                                "detail": f"{len(batch.get('items') or [])} preview items (not executed)",
                            }
                        )
                else:
                    stage_rows = []
                return _print_or_table(
                    res,
                    args=args,
                    rows=stage_rows,
                    columns=[
                        ("stage", "STAGE"),
                        ("artifact_id", "ARTIFACT_ID"),
                        ("status", "STATUS"),
                        ("detail", "DETAIL"),
                    ],
                )
        except (AccessDenied, KeyError, ValueError) as exc:
            return _cli_error_result(exc)
        parser.error(f"Unknown seo command: {scmd}")
        return 2

    if args.command == "shop-intel":
        from ecom_ops.rbac import AccessDenied, Permission, resolve_actor
        from ecom_ops.shop_intel import build_tailored_context, get_shop_intel_service
        from ecom_ops.wp_ops.registry import load_sites

        svc = get_shop_intel_service()
        icmd = args.shop_intel_command
        default_customer = load_profile().customer
        explicit_global_site = (
            args.site if getattr(args, "site", None) and args.site != default_customer else None
        )
        si_site = getattr(args, "si_site_id", None) or explicit_global_site
        if si_site:
            resolved_site = str(si_site).strip().lower()
        else:
            sites = load_sites(data_dir=svc.data_dir, apply_overlay=False)
            resolved_site = sites[0].id if sites else "mock-shop"

        try:
            if icmd == "run":
                raw_domains = [d.strip() for d in (args.domains or "").split(",") if d.strip()]
                res = svc.run_learning(
                    site_id=resolved_site,
                    actor=args.actor,
                    mode=args.mode,
                    domains=raw_domains or None,
                )
                return _print(res)
            if icmd == "status":
                res = svc.get_status(site_id=si_site, actor=args.actor)
                return _print(res)
            if icmd == "query":
                res = svc.query_knowledge(
                    resolved_site,
                    domain=args.domain,
                    query=args.query,
                    purpose=args.purpose,
                    max_chars=args.max_chars,
                    actor=args.actor,
                )
                return _print(res)
            if icmd == "security":
                res = svc.get_security_report(
                    resolved_site,
                    severity=args.severity,
                    limit=args.limit,
                    actor=args.actor,
                )
                return _print(res)
            if icmd == "diff":
                res = svc.get_diffs(resolved_site, limit=args.limit, actor=args.actor)
                return _print(res)
            if icmd == "context":
                actor_obj = resolve_actor(args.actor)
                if not (
                    actor_obj.has(Permission.WP_OPS_READ) or actor_obj.has(Permission.READ)
                ):
                    raise AccessDenied(
                        f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks WP_OPS_READ or READ"
                    )
                ctx = build_tailored_context(
                    resolved_site,
                    purpose=args.purpose,
                    data_dir=svc.data_dir,
                    max_chars=args.max_chars,
                )
                return _print(
                    {
                        "ok": True,
                        "site_id": resolved_site,
                        "purpose": args.purpose,
                        "present": ctx is not None,
                        "context": ctx,
                    }
                )
            if icmd == "export":
                res = svc.export_dossier(
                    resolved_site,
                    fmt=args.export_fmt,
                    actor=args.actor,
                )
                return _print(res)
            if icmd == "schedule-run":
                if si_site:
                    from datetime import UTC, datetime

                    actor_obj = resolve_actor(args.actor)
                    has_run = (
                        actor_obj.has(Permission.RESEARCH_RUN)
                        or actor_obj.has(Permission.WP_OPS_OPERATE)
                        or actor_obj.has(Permission.WP_OPS_ADMIN)
                    )
                    if not (actor_obj.has(Permission.WP_OPS_READ) and has_run):
                        raise AccessDenied(
                            f"Actor {actor_obj.name!r} lacks Shop Intel run permission"
                        )
                    sched = svc.store.get_schedule(resolved_site)
                    now_iso = datetime.now(UTC).isoformat()
                    executed: list[dict[str, Any]] = []
                    if sched and sched.get("enabled"):
                        nxt = sched.get("next_run_at")
                        if not nxt or str(nxt) <= now_iso:
                            r = svc.run_learning(
                                resolved_site,
                                actor=args.actor,
                                mode=str(sched.get("mode") or "full"),
                            )
                            executed.append(
                                {
                                    "site_id": resolved_site,
                                    "ok": bool(r.get("ok")),
                                    "result": r,
                                }
                            )
                    return _print(
                        {
                            "ok": True,
                            "site_id": resolved_site,
                            "executed_count": len(executed),
                            "executed": executed,
                        }
                    )
                res = svc.run_due_schedules(actor=args.actor)
                return _print(res)
            if icmd == "schedule-set":
                res = svc.configure_schedule(
                    resolved_site,
                    enabled=bool(args.sched_enabled),
                    interval_hours=args.interval_hours,
                    mode=args.mode,
                    actor=args.actor,
                )
                return _print(res)
        except (AccessDenied, KeyError, ValueError) as exc:
            return _cli_error_result(exc)
        parser.error(f"Unknown shop-intel command: {icmd}")
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
