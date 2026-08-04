"""Marketing actions: GA4 + Google Ads + Merchant (read → suggest → HITL mutate)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations import ga4 as ga4_mod
from ecom_ops.integrations import google_ads as ads_mod
from ecom_ops.integrations import merchant as merchant_mod
from ecom_ops.marketing.config import load_marketing_config
from ecom_ops.marketing.kill_switch import (
    ads_mutate_allowed,
    merchant_write_allowed,
    mp_allowed,
)
from ecom_ops.marketing.suggest_store import MarketingSuggestStore
from ecom_ops.rbac import (
    AccessDenied,
    Actor,
    Permission,
    require_permission,
    resolve_actor,
)
from ecom_ops.telemetry import Telemetry, default_telemetry


@dataclass(frozen=True)
class MarketingResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "data": self.data,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }


class MarketingService:
    def __init__(
        self,
        *,
        ga4: ga4_mod.GA4Client | None = None,
        ads: ads_mod.GoogleAdsClient | None = None,
        merchant: merchant_mod.MerchantClient | None = None,
        suggests: MarketingSuggestStore | None = None,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
        use_mock: bool | None = None,
    ) -> None:
        self._use_mock = use_mock
        self.ga4 = ga4 or ga4_mod.client_from_env(use_mock=use_mock)
        self.ads = ads or ads_mod.client_from_env(use_mock=use_mock)
        self.merchant = merchant or merchant_mod.client_from_env(use_mock=use_mock)
        self.suggests = suggests or MarketingSuggestStore()
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation

    def _actor(self, actor: Actor | str | None) -> Actor:
        return actor if isinstance(actor, Actor) else resolve_actor(actor)

    def digest(
        self,
        *,
        days: int | None = None,
        actor: Actor | str | None = None,
    ) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_READ)
            cfg = load_marketing_config()
            d = int(days or cfg.default_lookback_days)
            ads = self.ads.digest(days=d)
            ga = self.ga4.digest(days=d)
            payload = {
                "days": d,
                "truth_note": (
                    "Woo orders = commerce truth; "
                    "ads.roas_ads_reported = Ads attribution; "
                    "ga.purchase_revenue = GA reporting (may be modeled)"
                ),
                "ads": ads,
                "ga4": ga,
                "currency": ads.get("currency") or ga.get("currency") or cfg.default_currency,
            }
            self.telemetry.record(
                action="marketing_report",
                site="azom",
                meta={"kind": "digest", "days": d, "actor": actor_obj.name},
            )
            return MarketingResult(ok=True, message="Marketing digest", data=payload)
        except AccessDenied as exc:
            return MarketingResult(ok=False, message=str(exc))
        except PermissionError as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            ticket = self.escalation.escalate_critical(
                "Marketing digest failed",
                details={"error": str(exc)[:200]},
            )
            return MarketingResult(
                ok=False,
                message=f"Failed: {exc}",
                escalated=True,
                ticket_id=ticket.id,
            )

    def health(self, *, actor: Actor | str | None = None) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_READ)
            conv = self.ga4.conversion_health()
            funnel = self.ga4.funnel_events()
            data = {
                "conversion": conv,
                "ecommerce_events": funnel,
                "labels": {
                    "purchase_key_event": conv.get("purchase_is_key_event"),
                    "ads_linked": conv.get("google_ads_linked"),
                    "funnel_ok": funnel.get("ok"),
                },
            }
            return MarketingResult(ok=True, message="Marketing health", data=data)
        except (AccessDenied, PermissionError) as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            return MarketingResult(ok=False, message=f"Failed: {exc}")

    def waste(
        self,
        *,
        days: int | None = None,
        actor: Actor | str | None = None,
    ) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_READ)
            cfg = load_marketing_config()
            d = int(days or cfg.default_lookback_days)
            rows = self.ads.waste_report(days=d)
            return MarketingResult(
                ok=True,
                message=f"{len(rows)} waste search terms",
                data={"days": d, "terms": rows, "source": "google_ads_search_terms"},
            )
        except (AccessDenied, PermissionError) as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            return MarketingResult(ok=False, message=f"Failed: {exc}")

    def pacing(self, *, actor: Actor | str | None = None) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_READ)
            cfg = load_marketing_config()
            rows = self.ads.pacing()
            alerts = [
                r
                for r in rows
                if float(r.get("pacing_ratio") or 0) >= cfg.pacing_alert_ratio
            ]
            return MarketingResult(
                ok=True,
                message=f"{len(alerts)} pacing alert(s)",
                data={
                    "campaigns": rows,
                    "alerts": alerts,
                    "threshold": cfg.pacing_alert_ratio,
                    "note": "Alert only — budget change requires HITL mutate",
                },
            )
        except (AccessDenied, PermissionError) as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            return MarketingResult(ok=False, message=f"Failed: {exc}")

    def consistency(
        self,
        *,
        days: int | None = None,
        woo_purchases: int | None = None,
        actor: Actor | str | None = None,
    ) -> MarketingResult:
        """Compare Woo / GA / Ads purchase volumes (labeled)."""
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_READ)
            cfg = load_marketing_config()
            d = int(days or cfg.default_lookback_days)
            ga = self.ga4.digest(days=d)
            ads = self.ads.digest(days=d)
            ga_purchases = int(ga.get("ecommerce_purchases") or 0)
            ads_conv = float(ads.get("conversions") or 0)
            woo_n = woo_purchases
            if woo_n is None:
                woo_n = self._woo_purchase_count(days=d)
            bands = self._divergence_bands(
                woo=float(woo_n),
                ga=float(ga_purchases),
                ads=ads_conv,
                warn_pct=cfg.purchase_divergence_warn_pct,
                error_pct=cfg.purchase_divergence_error_pct,
            )
            landings = self.ga4.landing_pages(days=d)
            urls = self.ads.final_urls()
            landing_flags = [
                lp
                for lp in landings
                if "404" in str(lp.get("landing_page") or "").lower()
                or int(lp.get("purchases") or 0) == 0
                and int(lp.get("sessions") or 0) >= 50
            ]
            shopping = self.ads.shopping()
            data = {
                "days": d,
                "counts": {
                    "woo_purchases": woo_n,
                    "ga_ecommerce_purchases": ga_purchases,
                    "ads_conversions": ads_conv,
                },
                "divergence": bands,
                "ads_ga_link": self.ga4.conversion_health().get("google_ads_linked"),
                "landing_flags": landing_flags,
                "ads_final_urls": urls,
                "shopping_products": shopping,
                "truth_note": "Prefer Woo for order count; treat GA/Ads as marketing views",
            }
            self.telemetry.record(
                action="marketing_report",
                site="azom",
                meta={"kind": "consistency", "days": d, "actor": actor_obj.name},
            )
            return MarketingResult(ok=True, message="Consistency check", data=data)
        except (AccessDenied, PermissionError) as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            return MarketingResult(ok=False, message=f"Failed: {exc}")

    def mer(
        self,
        *,
        days: int | None = None,
        woo_revenue: float | None = None,
        actor: Actor | str | None = None,
    ) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_READ)
            cfg = load_marketing_config()
            d = int(days or cfg.default_lookback_days)
            ads = self.ads.digest(days=d)
            spend = float(ads.get("cost") or 0)
            rev = woo_revenue
            if rev is None:
                rev = self._woo_revenue(days=d)
            mer_val = (float(rev) / spend) if spend > 0 else None
            return MarketingResult(
                ok=True,
                message="MER (Woo revenue / Ads spend)",
                data={
                    "days": d,
                    "woo_revenue": rev,
                    "ads_spend": spend,
                    "mer": round(mer_val, 3) if mer_val is not None else None,
                    "currency": ads.get("currency") or cfg.default_currency,
                    "note": "MER uses Woo revenue ÷ Google Ads spend only (Meta not included)",
                },
            )
        except (AccessDenied, PermissionError) as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            return MarketingResult(ok=False, message=f"Failed: {exc}")

    def build_waste_suggests(
        self, *, actor: Actor | str | None = None
    ) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_SUGGEST)
            open_rows = self.suggests.list(status="open", limit=500)
            open_keys = {
                (
                    r.kind,
                    str((r.payload or {}).get("search_term") or ""),
                    str((r.payload or {}).get("campaign_id") or ""),
                    str((r.payload or {}).get("op") or ""),
                )
                for r in open_rows
            }
            waste = self.ads.waste_report()
            created = []
            skipped = 0
            for term in waste[:20]:
                key = (
                    "negative",
                    str(term.get("search_term") or ""),
                    str(term.get("campaign_id") or ""),
                    "add_negative_keyword",
                )
                if key in open_keys:
                    skipped += 1
                    continue
                item = self.suggests.add(
                    kind="negative",
                    title=f"Negativ: {term.get('search_term')}",
                    reason="zero_conversions_with_cost",
                    evidence=term,
                    payload={
                        "op": "add_negative_keyword",
                        "search_term": term.get("search_term"),
                        "campaign_id": term.get("campaign_id"),
                    },
                )
                open_keys.add(key)
                created.append(item.to_dict())
            pacing = self.ads.pacing()
            cfg = load_marketing_config()
            for row in pacing:
                if float(row.get("pacing_ratio") or 0) < cfg.pacing_alert_ratio:
                    continue
                key = (
                    "budget",
                    "",
                    str(row.get("campaign_id") or ""),
                    "adjust_budget",
                )
                if key in open_keys:
                    skipped += 1
                    continue
                item = self.suggests.add(
                    kind="budget",
                    title=f"Budget pacing: {row.get('campaign_name')}",
                    reason="pacing_over_threshold",
                    evidence=row,
                    payload={
                        "op": "adjust_budget",
                        "campaign_id": row.get("campaign_id"),
                        "suggested": "review_budget",
                    },
                )
                open_keys.add(key)
                created.append(item.to_dict())
            return MarketingResult(
                ok=True,
                message=f"Created {len(created)} suggest(s), skipped {skipped} duplicate(s)",
                data={"suggests": created, "skipped_duplicates": skipped},
            )
        except (AccessDenied, PermissionError) as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            return MarketingResult(ok=False, message=f"Failed: {exc}")

    def list_suggests(
        self,
        *,
        status: str = "open",
        actor: Actor | str | None = None,
    ) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_READ)
            rows = [s.to_dict() for s in self.suggests.list(status=status)]
            return MarketingResult(
                ok=True,
                message=f"{len(rows)} suggest(s)",
                data={"suggests": rows},
            )
        except AccessDenied as exc:
            return MarketingResult(ok=False, message=str(exc))

    def deny_suggest(
        self, suggest_id: str, *, actor: Actor | str | None = None
    ) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_SUGGEST)
            updated = self.suggests.set_status(
                suggest_id, "denied", actor=actor_obj.name
            )
            if not updated:
                return MarketingResult(ok=False, message="Suggest not found")
            return MarketingResult(
                ok=True, message="Suggest denied", data=updated.to_dict()
            )
        except AccessDenied as exc:
            return MarketingResult(ok=False, message=str(exc))

    def approve_and_mutate(
        self, suggest_id: str, *, actor: Actor | str | None = None
    ) -> MarketingResult:
        """HITL: apply an open suggest via Ads/MP/Merchant mutate path."""
        try:
            actor_obj = self._actor(actor)
            item = self.suggests.get(suggest_id)
            if not item:
                return MarketingResult(ok=False, message="Suggest not found")
            if item.status != "open":
                return MarketingResult(
                    ok=False,
                    message=f"Suggest status is {item.status}, expected open",
                    data=item.to_dict(),
                )
            kind = item.kind
            # Jonatan may HITL-approve negatives; Oscar required for pause/budget/MP/feed
            if kind == "negative":
                require_permission(actor_obj, Permission.MARKETING_SUGGEST)
            else:
                require_permission(actor_obj, Permission.MARKETING_MUTATE)
            if kind in {"negative", "pause", "budget", "recommendation"}:
                allowed, reason = ads_mutate_allowed()
                if not allowed:
                    self.telemetry.record(
                        action="ads_mutate_blocked_kill",
                        site="azom",
                        meta={
                            "suggest_id": suggest_id,
                            "reason": reason,
                            "actor": actor_obj.name,
                        },
                    )
                    return MarketingResult(
                        ok=False,
                        message=f"Ads mutate blocked ({reason})",
                        data=item.to_dict(),
                    )
                expected_ops = {
                    "negative": "add_negative_keyword",
                    "pause": "pause_campaign",
                    "budget": "adjust_budget",
                    "recommendation": "apply_recommendation",
                }
                op = str(item.payload.get("op") or "")
                if op != expected_ops.get(kind, op):
                    return MarketingResult(
                        ok=False,
                        message=f"Payload op {op!r} does not match kind {kind!r}",
                        data=item.to_dict(),
                    )
                ops = [item.payload]
                result = self.ads.mutate(ops)
                if not self._mutate_ok(result):
                    self.suggests.set_status(
                        suggest_id, "failed", actor=actor_obj.name
                    )
                    return MarketingResult(
                        ok=False,
                        message="Ads mutate failed",
                        data={"suggest_id": suggest_id, "result": result},
                    )
                self.suggests.set_status(
                    suggest_id, "applied", actor=actor_obj.name
                )
                self.telemetry.record(
                    action="ads_mutated",
                    site="azom",
                    meta={
                        "suggest_id": suggest_id,
                        "kind": kind,
                        "actor": actor_obj.name,
                    },
                )
                return MarketingResult(
                    ok=True,
                    message="Ads mutate applied",
                    data={"suggest_id": suggest_id, "result": result},
                )
            if kind == "mp":
                allowed, reason = mp_allowed()
                if not allowed:
                    return MarketingResult(
                        ok=False, message=f"MP blocked ({reason})"
                    )
                result = self.ga4.send_measurement_protocol(item.payload)
                if not self._mutate_ok(result):
                    self.suggests.set_status(
                        suggest_id, "failed", actor=actor_obj.name
                    )
                    return MarketingResult(
                        ok=False,
                        message="Measurement Protocol send failed",
                        data={"result": result},
                    )
                self.suggests.set_status(
                    suggest_id, "applied", actor=actor_obj.name
                )
                return MarketingResult(
                    ok=True,
                    message="Measurement Protocol event sent",
                    data={"result": result},
                )
            if kind == "merchant":
                allowed, reason = merchant_write_allowed()
                if not allowed:
                    return MarketingResult(
                        ok=False, message=f"Merchant write blocked ({reason})"
                    )
                op = str(item.payload.get("op") or "upsert")
                if op == "delete":
                    result = self.merchant.delete_product(
                        str(item.payload.get("id") or "")
                    )
                else:
                    result = self.merchant.upsert_product(
                        dict(item.payload.get("product") or item.payload)
                    )
                if not self._mutate_ok(result):
                    self.suggests.set_status(
                        suggest_id, "failed", actor=actor_obj.name
                    )
                    return MarketingResult(
                        ok=False,
                        message="Merchant write failed",
                        data={"result": result},
                    )
                self.suggests.set_status(
                    suggest_id, "applied", actor=actor_obj.name
                )
                return MarketingResult(
                    ok=True,
                    message="Merchant write applied",
                    data={"result": result},
                )
            return MarketingResult(ok=False, message=f"Unknown suggest kind: {kind}")
        except AccessDenied as exc:
            return MarketingResult(ok=False, message=str(exc))
        except Exception as exc:
            if suggest_id:
                self.suggests.set_status(suggest_id, "failed", actor="system")
            return MarketingResult(ok=False, message=f"Mutate failed: {exc}")

    def queue_mp_event(
        self,
        payload: dict[str, Any],
        *,
        actor: Actor | str | None = None,
    ) -> MarketingResult:
        """Queue MP event as suggest — never silent send."""
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_SUGGEST)
            allowed, reason = mp_allowed()
            # Still allow queuing when disabled so Oscar can review intent
            item = self.suggests.add(
                kind="mp",
                title=f"MP event: {payload.get('name') or payload.get('events')}",
                reason="measurement_protocol_hitl",
                evidence={"mp_gate": reason, "mp_allowed": allowed},
                payload=payload,
            )
            return MarketingResult(
                ok=True,
                message="MP event queued for approve (not sent)",
                data=item.to_dict(),
            )
        except AccessDenied as exc:
            return MarketingResult(ok=False, message=str(exc))

    def queue_merchant_write(
        self,
        product: dict[str, Any],
        *,
        op: str = "upsert",
        actor: Actor | str | None = None,
    ) -> MarketingResult:
        try:
            actor_obj = self._actor(actor)
            require_permission(actor_obj, Permission.MARKETING_SUGGEST)
            item = self.suggests.add(
                kind="merchant",
                title=f"Merchant {op}: {product.get('offerId') or product.get('id')}",
                reason="merchant_write_hitl",
                evidence={"blast_radius": "shopping_feed"},
                payload={"op": op, "product": product, "id": product.get("id")},
            )
            return MarketingResult(
                ok=True,
                message="Merchant write queued for approve",
                data=item.to_dict(),
            )
        except AccessDenied as exc:
            return MarketingResult(ok=False, message=str(exc))

    def snapshot(self, *, actor: Actor | str | None = None) -> MarketingResult:
        """Compact bot/dashboard snapshot."""
        dig = self.digest(actor=actor)
        if not dig.ok:
            return dig
        health = self.health(actor=actor)
        pace = self.pacing(actor=actor)
        return MarketingResult(
            ok=True,
            message="Marketing snapshot",
            data={
                "digest": dig.data,
                "health": health.data if health.ok else {"error": health.message},
                "pacing": pace.data if pace.ok else {"error": pace.message},
            },
        )

    def _mock_runtime(self) -> bool:
        if self._use_mock is True:
            return True
        if self._use_mock is False:
            return False
        return os.environ.get("AZOM_USE_MOCK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _woo_orders_in_window(self, *, days: int) -> list[Any]:
        """Orders in lookback window (paginated, capped)."""
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=self._use_mock)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, int(days)))
        after = start.isoformat().replace("+00:00", "Z")
        before = end.isoformat().replace("+00:00", "Z")
        collected: list[Any] = []
        page = 1
        while page <= 20 and len(collected) < 2000:
            batch = woo.list_orders(
                status="processing,completed",
                per_page=100,
                page=page,
                after=after,
                before=before,
            )
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return collected

    def _woo_purchase_count(self, *, days: int) -> int:
        if self._mock_runtime():
            return 45
        try:
            return len(self._woo_orders_in_window(days=days))
        except Exception:
            return 0

    def _woo_revenue(self, *, days: int) -> float:
        if self._mock_runtime():
            return 132_000.0
        try:
            total = 0.0
            for order in self._woo_orders_in_window(days=days):
                raw = getattr(order, "total", None)
                if raw is None and isinstance(order, dict):
                    raw = order.get("total")
                try:
                    total += float(raw or 0)
                except (TypeError, ValueError):
                    continue
            return round(total, 2)
        except Exception:
            return 0.0

    @staticmethod
    def _mutate_ok(result: Any) -> bool:
        if result is None:
            return False
        if isinstance(result, dict):
            if result.get("ok") is False:
                return False
            if result.get("error"):
                return False
        return True

    @staticmethod
    def _divergence_bands(
        *,
        woo: float,
        ga: float,
        ads: float,
        warn_pct: float,
        error_pct: float,
    ) -> dict[str, Any]:
        def band(a: float, b: float) -> str:
            if a <= 0 and b <= 0:
                return "ok"
            base = max(a, b, 1.0)
            pct = abs(a - b) / base * 100
            if pct >= error_pct:
                return "error"
            if pct >= warn_pct:
                return "warn"
            return "ok"

        return {
            "woo_vs_ga": band(woo, ga),
            "woo_vs_ads": band(woo, ads),
            "ga_vs_ads": band(ga, ads),
            "warn_pct": warn_pct,
            "error_pct": error_pct,
        }
