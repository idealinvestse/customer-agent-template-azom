"""CLI module: python -m ecom_ops.order_status_update"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ecom_ops.actions.order_status import OrderStatusService
from ecom_ops.integrations.woocommerce import client_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update WooCommerce order status")
    parser.add_argument("--site", default="azom")
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--actor", default="agent")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["AZOM_USE_MOCK"] = "1"

    svc = OrderStatusService(woo=client_from_env(use_mock=args.mock or None))
    result = svc.update(
        order_id=args.order_id,
        status=args.status,
        site=args.site,
        actor=args.actor,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
