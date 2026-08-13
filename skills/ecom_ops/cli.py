"""Unified CLI for Azom ecom-ops V2."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ecom_ops import __version__
from ecom_ops.actions.mail import MailService
from ecom_ops.actions.order_status import OrderStatusService
from ecom_ops.actions.product_desc import ProductDescService
from ecom_ops.actions.ssh_ops import SSHOpsService
from ecom_ops.actions.support import SupportService
from ecom_ops.integrations.mail import client_from_env as mail_client_from_env
from ecom_ops.integrations.woocommerce import client_from_env


def _print(result: Any) -> int:
    data = result.to_dict() if hasattr(result, "to_dict") else result
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if isinstance(data, dict):
        return 0 if data.get("ok", True) else 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecom-ops",
        description=(
            "Azom ecom-ops V2: order-status, product-desc, support, SSH, mail "
            "(dashboard/OAuth/Telegram via separate entrypoints)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ecom-ops {__version__}",
    )
    parser.add_argument("--site", default="azom", help="Customer/site id")
    parser.add_argument(
        "--actor",
        default="agent",
        help="Actor name (jonatan|oscar|agent)",
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
        help="Support-loop KPIs last N days (TTA, edit distance, FAQ retrieve hits)",
    )
    p_kpis.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (default 7)",
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

    p_faq = sub.add_parser("faq", help="FAQ / knowledge base (search + WP publish)")
    faq_sub = p_faq.add_subparsers(dest="faq_command", required=True)
    p_faq_list = faq_sub.add_parser("list", help="List FAQ articles")
    p_faq_list.add_argument("--market", default=None)
    p_faq_list.add_argument("--category", default=None)
    p_faq_list.add_argument(
        "--all",
        action="store_true",
        help="Include non-customer_safe articles",
    )
    p_faq_search = faq_sub.add_parser("search", help="Lexical FAQ search")
    p_faq_search.add_argument("--q", required=True, dest="query")
    p_faq_search.add_argument("--market", default="se")
    p_faq_search.add_argument("--category", default=None)
    p_faq_search.add_argument("--limit", type=int, default=None)
    faq_sub.add_parser("coverage", help="Corpus counts per market/category")
    faq_sub.add_parser("reload", help="Reload corpus cache (long-lived workers)")
    faq_sub.add_parser("validate", help="Validate YAML corpus schema")
    p_faq_sync = faq_sub.add_parser(
        "sync-draft", help="Upsert WP FAQ page as draft (Oscar)"
    )
    p_faq_sync.add_argument("--market", default="se")
    p_faq_pub = faq_sub.add_parser(
        "publish", help="Set WP FAQ page status (Oscar; default publish)"
    )
    p_faq_pub.add_argument("--market", default="se")
    p_faq_pub.add_argument(
        "--status",
        default="publish",
        choices=["publish", "draft", "private"],
    )

    p_faq_ds = faq_sub.add_parser("dataset", help="FAQ Q&A dataset export")
    faq_ds_sub = p_faq_ds.add_subparsers(dest="faq_dataset_command", required=True)
    p_faq_ds_exp = faq_ds_sub.add_parser(
        "export", help="Export inbound→outbound Q&A pairs from cases.db"
    )
    p_faq_ds_exp.add_argument("--market", default=None)
    p_faq_ds_exp.add_argument("--since-days", type=int, default=None)
    p_faq_ds_exp.add_argument(
        "--exclude-stale",
        action="store_true",
        help="Drop pairs older than dataset_max_age_days (default: include stale)",
    )
    p_faq_ds_exp.add_argument(
        "--raw",
        action="store_true",
        help="Skip PII redaction (Oscar only)",
    )

    p_faq_ingest = faq_sub.add_parser("ingest", help="FAQ staging ingest")
    faq_ingest_sub = p_faq_ingest.add_subparsers(
        dest="faq_ingest_command", required=True
    )
    p_faq_ing_site = faq_ingest_sub.add_parser(
        "site", help="Ingest WP pages/posts into staging"
    )
    p_faq_ing_site.add_argument("--market", default="se")
    p_faq_ing_site.add_argument(
        "--pages", action="store_true", help="Include pages (default from config)"
    )
    p_faq_ing_site.add_argument(
        "--posts", action="store_true", help="Include posts (default from config)"
    )
    p_faq_ing_site.add_argument(
        "--no-pages", action="store_true", help="Skip pages"
    )
    p_faq_ing_site.add_argument(
        "--no-posts", action="store_true", help="Skip posts"
    )
    p_faq_ing_prod = faq_ingest_sub.add_parser(
        "products", help="Ingest Woo products (+ allowlisted guides)"
    )
    p_faq_ing_prod.add_argument("--market", default="se")
    p_faq_ing_prod.add_argument(
        "--no-guides", action="store_true", help="Skip guide URL fetch"
    )

    p_faq_sug = faq_sub.add_parser(
        "suggest", help="Suggest FAQ article drafts into staging"
    )
    faq_sug_sub = p_faq_sug.add_subparsers(dest="faq_suggest_command", required=True)
    p_faq_sug_art = faq_sug_sub.add_parser(
        "articles", help="Write YAML drafts under faq_staging/articles"
    )
    p_faq_sug_art.add_argument("--market", default="se")
    p_faq_sug_art.add_argument(
        "--from",
        dest="source",
        default="products",
        choices=["staging", "dataset", "products"],
    )
    p_faq_sug_art.add_argument("--limit", type=int, default=50)

    p_faq_prom = faq_sub.add_parser(
        "promote", help="Promote staging draft to config/faq (Oscar)"
    )
    p_faq_prom.add_argument("--id", required=True, dest="article_id")
    p_faq_prom.add_argument("--market", default=None)
    p_faq_prom.add_argument(
        "--apply",
        action="store_true",
        help="Write to config/faq (default dry-run)",
    )

    faq_sub.add_parser(
        "staging", help="Show FAQ staging counts under AZOM_DATA_DIR"
    )

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

    p_retention = cases_sub.add_parser(
        "retention-purge",
        help="GDPR: delete/redact closed cases older than N days (default 90)",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["AZOM_USE_MOCK"] = "1"

    if getattr(args, "null_send", False):
        from ecom_ops.runtime_profile import enable_null_send

        enable_null_send()

    # Ops logs to stderr/file only — never touch CLI JSON stdout contract.
    log_name = "cli"
    if args.command == "cases" and getattr(args, "cases_command", None) == "poll":
        log_name = "cases-poll"
    os.environ.setdefault("AZOM_LOG_NAME", log_name)
    from ecom_ops.json_logging import configure_json_logging

    configure_json_logging(log_name=log_name)

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
        result = svc.generate(
            product_id=args.product_id,
            name=args.name,
            features=args.features,
            language=args.language,
            site=args.site,
            publish=args.publish,
            actor=args.actor,
        )
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
        from ecom_ops.ops_status import readiness_from_last_poll
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
                "budget": budget,
            }
        except Exception as exc:
            status = {
                "ok": False,
                "version": __version__,
                "null_send": null_send_label(),
                "ga4": "off",
                "ads": "off",
                "error": str(exc),
            }
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status.get("ok") else 1

    if args.command == "smoke":
        from ecom_ops.smoke import run_live_smoke

        result = run_live_smoke(force=bool(getattr(args, "live", False)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", False) else 1

    if args.command == "kpis":
        from ecom_ops.kpis import support_kpis_last_days

        result = support_kpis_last_days(days=int(getattr(args, "days", 7) or 7))
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
                from datetime import datetime, timedelta, timezone

                from ecom_ops.cases.store import CaseStore

                store = CaseStore()
                days = int(args.days or 90)
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                cases = store.list_cases(status="closed", limit=10000)
                eligible = [c for c in cases if (c.updated_at or "") < cutoff]
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "dry_run": True,
                            "eligible": len(eligible),
                            "retention_days": days,
                            "message": f"Dry run: {len(eligible)} cases eligible",
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
        parser.error(f"Unknown cases command: {args.cases_command}")
        return 2

    if args.command == "faq":
        from ecom_ops.faq.config import load_faq_config
        from ecom_ops.faq.publish import (
            corpus_content_hash,
            publish_page,
            sync_draft,
        )
        from ecom_ops.faq.publish_map import FaqPublishMap
        from ecom_ops.faq.search import search_faq
        from ecom_ops.faq.store import (
            default_faq_store,
            load_faq_corpus,
            reload_faq_store,
        )
        from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor

        actor_obj = resolve_actor(args.actor)
        cmd = args.faq_command
        if cmd == "list":
            try:
                require_permission(actor_obj, Permission.FAQ_READ)
            except AccessDenied as exc:
                return _print({"ok": False, "message": str(exc)})
            arts = default_faq_store().list(
                market=args.market,
                category=args.category,
                customer_safe_only=not args.all,
            )
            return _print(
                {
                    "ok": True,
                    "count": len(arts),
                    "enabled": load_faq_config().enabled,
                    "articles": [a.to_dict() for a in arts],
                }
            )
        if cmd == "search":
            try:
                require_permission(actor_obj, Permission.FAQ_READ)
            except AccessDenied as exc:
                return _print({"ok": False, "message": str(exc)})
            hits = search_faq(
                args.query,
                market=args.market,
                category=args.category,
                limit=args.limit,
            )
            return _print(
                {
                    "ok": True,
                    "count": len(hits),
                    "hits": [h.to_dict() for h in hits],
                }
            )
        if cmd == "coverage":
            try:
                require_permission(actor_obj, Permission.FAQ_READ)
            except AccessDenied as exc:
                return _print({"ok": False, "message": str(exc)})
            store = default_faq_store()
            cov = store.coverage()
            drift: dict[str, Any] = {}
            pmap = FaqPublishMap()
            for mkt in ("se", "no", "dk"):
                current = corpus_content_hash(mkt, store=store)
                row = pmap.get(mkt, "")
                drift[mkt] = {
                    "corpus_hash": current,
                    "published_hash": row.content_hash if row else None,
                    "drift": bool(
                        row and current and row.content_hash != current
                    ),
                    "wp_status": row.status if row else None,
                }
            return _print({"ok": True, "coverage": cov, "drift": drift})
        if cmd == "reload":
            try:
                require_permission(actor_obj, Permission.FAQ_READ)
            except AccessDenied as exc:
                return _print({"ok": False, "message": str(exc)})
            store = reload_faq_store()
            return _print(
                {
                    "ok": True,
                    "count": len(store.list()),
                    "issues": [
                        {"level": i.level, "message": i.message, "path": i.path}
                        for i in store.last_issues
                    ],
                }
            )
        if cmd == "validate":
            try:
                require_permission(actor_obj, Permission.FAQ_READ)
            except AccessDenied as exc:
                return _print({"ok": False, "message": str(exc)})
            report = load_faq_corpus()
            errors = [i for i in report.issues if i.level == "error"]
            return _print(
                {
                    "ok": len(errors) == 0,
                    "article_count": len(report.articles),
                    "error_count": len(errors),
                    "issues": [
                        {
                            "level": i.level,
                            "message": i.message,
                            "path": i.path,
                            "article_id": i.article_id,
                        }
                        for i in report.issues
                    ],
                }
            )
        if cmd == "sync-draft":
            return _print(
                sync_draft(
                    args.market,
                    actor=actor_obj,
                    use_mock=args.mock or None,
                )
            )
        if cmd == "publish":
            return _print(
                publish_page(
                    args.market,
                    status=args.status,
                    actor=actor_obj,
                    use_mock=args.mock or None,
                )
            )
        if cmd == "dataset":
            from ecom_ops.faq.dataset import export_qa_pairs

            if args.faq_dataset_command == "export":
                include_stale = not bool(getattr(args, "exclude_stale", False))
                return _print(
                    export_qa_pairs(
                        market=args.market,
                        since_days=args.since_days,
                        include_stale=include_stale,
                        redact=not bool(args.raw),
                        actor=actor_obj,
                        use_mock=args.mock or None,
                    )
                )
            parser.error(f"Unknown faq dataset command: {args.faq_dataset_command}")
            return 2
        if cmd == "ingest":
            use_mock = args.mock or None
            if args.faq_ingest_command == "site":
                from ecom_ops.faq.ingest_site import ingest_site

                pages = None
                posts = None
                if args.pages:
                    pages = True
                if args.no_pages:
                    pages = False
                if args.posts:
                    posts = True
                if args.no_posts:
                    posts = False
                return _print(
                    ingest_site(
                        market=args.market,
                        pages=pages,
                        posts=posts,
                        actor=actor_obj,
                        use_mock=use_mock,
                    )
                )
            if args.faq_ingest_command == "products":
                from ecom_ops.faq.ingest_products import ingest_products

                return _print(
                    ingest_products(
                        market=args.market,
                        actor=actor_obj,
                        use_mock=use_mock,
                        fetch_guides=not bool(args.no_guides),
                    )
                )
            parser.error(f"Unknown faq ingest command: {args.faq_ingest_command}")
            return 2
        if cmd == "suggest":
            from ecom_ops.faq.suggest_articles import suggest_articles

            if args.faq_suggest_command == "articles":
                return _print(
                    suggest_articles(
                        market=args.market,
                        source=args.source,
                        limit=args.limit,
                        actor=actor_obj,
                        use_mock=args.mock or None,
                    )
                )
            parser.error(f"Unknown faq suggest command: {args.faq_suggest_command}")
            return 2
        if cmd == "promote":
            from ecom_ops.faq.promote import promote_article

            return _print(
                promote_article(
                    args.article_id,
                    market=args.market,
                    apply=bool(args.apply),
                    actor=actor_obj,
                )
            )
        if cmd == "staging":
            from ecom_ops.faq.staging import staging_counts

            counts = staging_counts()
            return _print({"ok": True, "staging": counts})
        parser.error(f"Unknown faq command: {cmd}")
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
        parser.error(f"Unknown marketing command: {cmd}")
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
