"""python -m mandate.cli issue|authorize|revoke|show|ledger

Reads MANDATE_SIGNING_KEY and MANDATE_DB from the environment. Prints JSON.
`authorize` exits 0 on ALLOW and 2 on DENY, so scripts can branch on it.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from ulid import ULID

from . import db, ledger, service


def _key() -> str:
    key = os.environ.get("MANDATE_SIGNING_KEY", "")
    if not key:
        sys.exit("MANDATE_SIGNING_KEY is not set. See .env.example.")
    return key


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

    a = sub.add_parser("authorize", help="submit a purchase intent (agent action)")
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

    if args.cmd == "authorize":
        key = _key()
        intent = {
            "intent_id": args.intent_id or f"int_{ULID()}",
            "merchant": args.merchant,
            "category": args.category,
            "amount_cents": args.amount,
            "currency": "USD",
            "item": args.item,
        }
        result = service.authorize(conn, key, args.token, intent, now)
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
