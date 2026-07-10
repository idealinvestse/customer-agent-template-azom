"""CLI module: python -m ecom_ops.support_handler"""

from __future__ import annotations

import argparse
import json
import sys

from ecom_ops.actions.support import SupportService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Handle support message")
    parser.add_argument("--site", default="azom")
    parser.add_argument("--message", required=True)
    parser.add_argument("--email")
    parser.add_argument("--customer-name")
    parser.add_argument("--language", default="sv")
    parser.add_argument("--actor", default="agent")
    args = parser.parse_args(argv)

    result = SupportService().handle(
        args.message,
        customer_email=args.email,
        customer_name=args.customer_name,
        language=args.language,
        site=args.site,
        actor=args.actor,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
