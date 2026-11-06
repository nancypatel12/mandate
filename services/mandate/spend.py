"""Per-period spend counters. Callers hold a write transaction (db.write_txn)."""

import sqlite3


def read(conn: sqlite3.Connection, mandate_id: str, period_start: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT spent_cents, txn_count FROM spend WHERE mandate_id = ? AND period_start = ?",
        (mandate_id, period_start),
    ).fetchone()
    return (row["spent_cents"], row["txn_count"]) if row else (0, 0)


def reserve(conn: sqlite3.Connection, mandate_id: str, period_start: str, cents: int) -> None:
    conn.execute(
        "INSERT INTO spend (mandate_id, period_start, spent_cents, txn_count) VALUES (?, ?, ?, 1)"
        " ON CONFLICT (mandate_id, period_start)"
        " DO UPDATE SET spent_cents = spent_cents + excluded.spent_cents, txn_count = txn_count + 1",
        (mandate_id, period_start, cents),
    )


def release(conn: sqlite3.Connection, mandate_id: str, period_start: str, cents: int) -> None:
    conn.execute(
        "UPDATE spend SET spent_cents = spent_cents - ?, txn_count = txn_count - 1"
        " WHERE mandate_id = ? AND period_start = ?",
        (cents, mandate_id, period_start),
    )
