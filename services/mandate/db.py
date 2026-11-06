"""SQLite storage. Plain sqlite3: four tables and two triggers don't need an ORM."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id    TEXT PRIMARY KEY,
    revocation_id TEXT NOT NULL UNIQUE,
    claims_json   TEXT NOT NULL,
    issued_at     TEXT NOT NULL,
    revoked_at    TEXT
);

-- One row per mandate per period window. A new window starts at zero.
CREATE TABLE IF NOT EXISTS spend (
    mandate_id   TEXT NOT NULL REFERENCES mandates(mandate_id),
    period_start TEXT NOT NULL,
    spent_cents  INTEGER NOT NULL CHECK (spent_cents >= 0),
    txn_count    INTEGER NOT NULL CHECK (txn_count >= 0),
    PRIMARY KEY (mandate_id, period_start)
);

-- Intents that passed policy. Makes retries idempotent: one intent_id, one charge.
CREATE TABLE IF NOT EXISTS intents (
    mandate_id TEXT NOT NULL,
    intent_id  TEXT NOT NULL,
    ledger_id  TEXT,  -- NULL while the purchase is in flight
    PRIMARY KEY (mandate_id, intent_id)
);

CREATE TABLE IF NOT EXISTS ledger (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id    TEXT NOT NULL UNIQUE,
    ts           TEXT NOT NULL,
    mandate_id   TEXT,
    intent_id    TEXT,
    merchant     TEXT,
    category     TEXT,
    amount_cents INTEGER,
    item         TEXT,
    verdict      TEXT NOT NULL CHECK (verdict IN ('ALLOW', 'DENY')),
    reason_code  TEXT,
    message      TEXT NOT NULL,
    order_id     TEXT,
    CHECK ((verdict = 'ALLOW') = (reason_code IS NULL))
);

-- Append-only is enforced by the database, not by convention.
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: we open transactions ourselves, so we choose when the write lock is taken.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE takes the write lock up front, so read-check-reserve is atomic
    and two parallel requests cannot both see room under a cap."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
