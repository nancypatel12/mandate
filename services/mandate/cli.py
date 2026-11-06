"""python -m mandate.cli fund|issue|buy|authorize|revoke|show|ledger

Reads settings from the environment (see .env.example). Prints JSON.
`buy` and `authorize` exit 0 on ALLOW and 2 on DENY, so scripts can branch on it.

  buy        the real thing: policy, then a PayPal sandbox order on ALLOW
  authorize  policy only, no PayPal (a dry run that still counts against the caps)
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime

from ulid import ULID

from . import db, funding, ledger, service
from .main import broker_for
from .paypal import PayPal


def _key() -> str:
    key = os.environ.get("MANDATE_SIGNING_KEY", "")
    if not key:
        sys.exit("MANDATE_SIGNING_KEY is not set. See .env.example.")
    return key


def _paypal() -> PayPal:
    cid, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not cid or not secret:
        sys.exit("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are not set. See .env.example.")
    return PayPal(cid, secret, os.environ.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"))


def _conn():
    return db.connect(os.environ.get("MANDATE_DB", "mandate.db"))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mandate")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("issue", help="sign a mandate (human action)")
    i.add_argument("--agent", required=True)
    i.add_argument("--user", required=True)
    i.add_argument("--merchant", action="append", required=True, help="repeatable, e.g. channel3:blue-bottle")
    i.add_argument("--category", action="append", required=True, help="repeatable, e.g. coffee")
    i.add_argument("--per-txn", type=int, required=True, help="cents")
    i.add_argument("--period", choices=["day", "week", "month"], required=True)
    i.add_argument("--period-cap", type=int, required=True, help="cents")
    i.add_argument("--max-txn", type=int, required=True)
    i.add_argument("--ttl", type=int, default=86_400, help="seconds")

    f = sub.add_parser("fund", help="link a PayPal sandbox wallet once (human action)")
    f.add_argument("--user", required=True)
    f.add_argument("--return-url", default="http://localhost:8000/funding/return")
    f.add_argument("--wait", type=int, default=600, help="seconds to wait for approval")

    for name, text in (("buy", "submit an intent and pay with PayPal on ALLOW"),
                       ("authorize", "submit an intent, policy only (no PayPal)")):
        a = sub.add_parser(name, help=text)
        a.add_argument("--token", required=True)
        a.add_argument("--merchant", required=True)
        a.add_argument("--category", required=True)
        a.add_argument("--amount", type=int, required=True, help="cents")
        a.add_argument("--item", required=True)
        a.add_argument("--intent-id", default=None)

    r = sub.add_parser("revoke", help="kill switch")
    r.add_argument("mandate_id")

    s = sub.add_parser("show", help="mandate + current spend")
    s.add_argument("mandate_id")

    lg = sub.add_parser("ledger", help="attempts, newest first")
    lg.add_argument("--limit", type=int, default=20)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn = _conn()
    try:
        return _run(args, conn, datetime.now(UTC))
    finally:
        conn.close()


def _run(args, conn, now) -> int:
    if args.cmd == "issue":
        token, claims = service.issue(
            conn, _key(), agent=args.agent, user=args.user,
            merchants=args.merchant, categories=args.category,
            per_txn_cap_cents=args.per_txn, period=args.period,
            period_cap_cents=args.period_cap, max_txn_per_period=args.max_txn,
            ttl_seconds=args.ttl, now=now,
        )
        _print({"token": token, "mandate": claims.model_dump(mode="json")})
        return 0

    if args.cmd == "fund":
        pp = _paypal()
        url = funding.start(conn, pp, args.user, args.return_url, args.return_url, now)
        print(f"Open this link and approve with your PayPal SANDBOX buyer account:\n\n  {url}\n",
              file=sys.stderr)
        deadline = time.monotonic() + args.wait
        status = "PAYER_ACTION_REQUIRED"
        while time.monotonic() < deadline:
            status = funding.complete(conn, pp, args.user, datetime.now(UTC))
            if status == "FUNDED":
                break
            time.sleep(3)
        _print({"user": args.user, "status": status})
        return 0 if status == "FUNDED" else 1

    if args.cmd in ("buy", "authorize"):
        key = _key()
        broker = broker_for(conn, _paypal()) if args.cmd == "buy" else None
        intent = {
            "intent_id": args.intent_id or f"int_{ULID()}",
            "merchant": args.merchant,
            "category": args.category,
            "amount_cents": args.amount,
            "currency": "USD",
            "item": args.item,
        }
        result = service.authorize(conn, key, args.token, intent, now, broker)
        _print(result.to_dict())
        return 0 if result.verdict.allowed else 2

    if args.cmd == "revoke":
        found = service.revoke(conn, args.mandate_id, now)
        _print({"mandate_id": args.mandate_id, "revoked": found})
        return 0 if found else 1

    if args.cmd == "show":
        s = service.status(conn, args.mandate_id, now)
        _print(s or {"error": "no such mandate"})
        return 0 if s else 1

    _print(ledger.page(conn, limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
