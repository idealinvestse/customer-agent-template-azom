"""CLI module: python -m ecom_ops.product_desc_gen"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ecom_ops.actions.product_desc import ProductDescService
from ecom_ops.integrations.woocommerce import client_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate product description")
    parser.add_argument("--site", default="azom")
    parser.add_argument("--product-id")
    parser.add_argument("--name")
    parser.add_argument("--features", default="")
    parser.add_argument("--language", default="sv")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--actor", default="agent")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["AZOM_USE_MOCK"] = "1"

    svc = ProductDescService(woo=client_from_env(use_mock=args.mock or None))
    result = svc.generate(
        product_id=args.product_id,
        name=args.name,
        features=args.features,
        language=args.language,
        site=args.site,
        publish=args.publish,
        actor=args.actor,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
