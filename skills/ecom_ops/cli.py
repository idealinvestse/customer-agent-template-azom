"""Unified CLI for Azom ecom-ops V3."""

from __future__ import annotations

import argparse
import json
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
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if isinstance(data, dict):
        return 0 if data.get("ok", True) else 1
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
    p_wp_show.add_argument("--id", required=True, dest="wp_site_id")
    wp_sub.add_parser("status", help="Health summary for every registered site")
    p_wp_check = wp_sub.add_parser("check", help="Run read-only health checks")
    p_wp_check.add_argument("--id", dest="wp_site_id")
    p_wp_check.add_argument(
        "--due",
        action="store_true",
        help="Run checks whose interval has elapsed",
    )
    p_wp_backup = wp_sub.add_parser("backup", help="Create a local mock backup manifest")
    p_wp_backup.add_argument("--id", required=True, dest="wp_site_id")
    p_wp_backups = wp_sub.add_parser("backups", help="List local mock backup manifests")
    p_wp_backups.add_argument("--id", required=True, dest="wp_site_id")
    p_wp_drill = wp_sub.add_parser("restore-drill", help="Record a staging restore drill")
    p_wp_drill.add_argument("--backup", required=True, dest="backup_id")
    p_wp_drill.add_argument("--target", required=True, dest="target_site_id")
    p_wp_files = wp_sub.add_parser("files", help="List themes, plugins, or files in one component")
    p_wp_files.add_argument("--id", required=True, dest="wp_site_id")
    p_wp_files.add_argument("--kind", required=True, choices=("theme", "plugin"))
    p_wp_files.add_argument("--slug", default="")
    p_wp_propose = wp_sub.add_parser(
        "file-propose",
        help="Propose a typed edit of one existing theme or plugin file",
    )
    p_wp_propose.add_argument("--id", required=True, dest="wp_site_id")
    p_wp_propose.add_argument("--kind", required=True, choices=("theme", "plugin"))
    p_wp_propose.add_argument("--slug", required=True)
    p_wp_propose.add_argument("--path", required=True, dest="relative_path")
    p_wp_propose.add_argument("--from", required=True, dest="from_file")
    p_wp_apply = wp_sub.add_parser(
        "file-apply",
        help="Apply a pending typed edit when the file text still matches",
    )
    p_wp_apply.add_argument("--id", required=True, dest="wp_site_id")
    p_wp_apply.add_argument("--plan", required=True, dest="plan_id")
    p_wp_apply.add_argument("--from", required=True, dest="from_file")

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
        if cmd == "sites":
            return _print(wp.list_sites(actor=args.actor))
        if cmd == "status":
            return _print(wp.list_sites(actor=args.actor))
        if cmd == "show":
            return _print(wp.show_site(args.wp_site_id, actor=args.actor))
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
            return _print(wp.create_backup(args.wp_site_id, actor=args.actor))
        if cmd == "backups":
            return _print(wp.list_backups(args.wp_site_id, actor=args.actor))
        if cmd == "restore-drill":
            return _print(
                wp.restore_drill(args.backup_id, target_site_id=args.target_site_id, actor=args.actor)
            )
        if cmd == "files":
            if args.slug:
                return _print(
                    wp.list_files(args.wp_site_id, args.kind, args.slug, actor=args.actor)
                )
            return _print(wp.list_components(args.wp_site_id, args.kind, actor=args.actor))
        if cmd in {"file-propose", "file-apply"}:
            text, error_class = _load_edit_source(args.from_file)
            if text is None:
                return _print(
                    _WpCliError(error_class or "NotText", "Edit source is unreadable.")
                )
            if cmd == "file-propose":
                return _print(
                    wp.propose_file(
                        args.wp_site_id,
                        args.kind,
                        args.slug,
                        args.relative_path,
                        text,
                        actor=args.actor,
                    )
                )
            return _print(wp.apply_file(args.wp_site_id, args.plan_id, text, actor=args.actor))
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
                    f"rotated={len(res['rotated_logs'])} "
                    f"reclaimed={res['mb_reclaimed']:.2f} MB "
                    f"errors={len(res['errors'])}"
                )
            return 0 if res.get("ok") else 1
        parser.error(f"Unknown ops command: {cmd}")
        return 2

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

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
