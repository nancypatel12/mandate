"""Every attempt, allowed and denied. Append-only (the triggers in db.py make sure)."""

import sqlite3
from datetime import UTC, datetime

from ulid import ULID

from .models import PurchaseIntent, Verdict


def append(
    conn: sqlite3.Connection,
    verdict: Verdict,
    now: datetime,
    *,
    mandate_id: str | None = None,
    intent: PurchaseIntent | None = None,
    order_id: str | None = None,
) -> str:
    ledger_id = f"led_{ULID()}"
    i = intent
    conn.execute(
        "INSERT INTO ledger (ledger_id, ts, mandate_id, intent_id, merchant, category, amount_cents,"
        " item, verdict, reason_code, message, order_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ledger_id,
            now.astimezone(UTC).isoformat(timespec="milliseconds"),
            mandate_id,
            i and i.intent_id, i and i.merchant, i and i.category, i and i.amount_cents, i and i.item,
            "ALLOW" if verdict.allowed else "DENY",
            verdict.reason_code and verdict.reason_code.value,
            verdict.message,
            order_id,
        ),
    )
    return ledger_id


def get(conn: sqlite3.Connection, ledger_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM ledger WHERE ledger_id = ?", (ledger_id,)).fetchone()
    return dict(row) if row else None


def page(conn: sqlite3.Connection, limit: int = 50, before: int | None = None) -> list[dict]:
    """Newest first. Pass the last row's `seq` as `before` to get the next page."""
    limit = max(1, min(limit, 500))
    if before is None:
        rows = conn.execute("SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (limit,))
    else:
        rows = conn.execute("SELECT * FROM ledger WHERE seq < ? ORDER BY seq DESC LIMIT ?", (before, limit))
    return [dict(r) for r in rows]
