"""Order status update action (WooCommerce)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.woocommerce import WooCommerceClient, WooOrder, client_from_env
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import SecurityError, validate_order_id, validate_order_status, validate_site
from ecom_ops.telemetry import Telemetry, default_telemetry


@dataclass(frozen=True)
class OrderStatusResult:
    ok: bool
    order: WooOrder | None
    previous_status: str | None
    new_status: str | None
    message: str
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "order_id": self.order.id if self.order else None,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "message": self.message,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }


class OrderStatusService:
    def __init__(
        self,
        woo: WooCommerceClient | None = None,
        *,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self.woo = woo or client_from_env(use_mock=None)
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation

    def update(
        self,
        *,
        order_id: str | int,
        status: str,
        site: str = "azom",
        actor: Actor | str | None = None,
        note: str | None = None,
    ) -> OrderStatusResult:
        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)

        try:
            require_permission(actor_obj, Permission.ORDER_STATUS_UPDATE)
            oid = validate_order_id(order_id)
            new_status = validate_order_status(status)

            current = self.woo.get_order(oid)
            if current.status == new_status:
                self.telemetry.record(
                    action="order_status_noop",
                    site=site,
                    meta={"order_id": oid, "status": new_status},
                )
                return OrderStatusResult(
                    ok=True,
                    order=current,
                    previous_status=current.status,
                    new_status=new_status,
                    message=f"Order {oid} already {new_status}",
                )

            updated = self.woo.update_order_status(oid, new_status)
            self.telemetry.record(
                action="order_status_update",
                site=site,
                meta={
                    "order_id": oid,
                    "from": current.status,
                    "to": new_status,
                    "actor": actor_obj.name,
                    "note": note,
                },
            )
            return OrderStatusResult(
                ok=True,
                order=updated,
                previous_status=current.status,
                new_status=updated.status,
                message=f"Order {oid}: {current.status} -> {updated.status}",
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Order status denied for {actor_obj.name}",
                details={"order_id": str(order_id), "error": str(exc), "site": site},
            )
            return OrderStatusResult(
                ok=False,
                order=None,
                previous_status=None,
                new_status=None,
                message=str(exc),
                escalated=True,
                ticket_id=ticket.id,
            )
        except SecurityError as exc:
            return OrderStatusResult(
                ok=False,
                order=None,
                previous_status=None,
                new_status=None,
                message=str(exc),
            )
        except Exception as exc:  # network / API
            ticket = self.escalation.escalate_critical(
                f"Order status failed for order {order_id}",
                details={"error": str(exc), "site": site},
            )
            return OrderStatusResult(
                ok=False,
                order=None,
                previous_status=None,
                new_status=None,
                message=f"Failed: {exc}",
                escalated=True,
                ticket_id=ticket.id,
            )


def update_order_status(
    order_id: str | int,
    status: str,
    *,
    site: str = "azom",
    actor: str | None = None,
) -> OrderStatusResult:
    return OrderStatusService().update(
        order_id=order_id, status=status, site=site, actor=actor
    )
