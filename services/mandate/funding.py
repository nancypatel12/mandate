"""Link a human's PayPal wallet once, then broker charges against it.

Flow: start() → the human opens the approve URL and logs in to PayPal sandbox →
complete() swaps the approved setup token for a payment token and stores it.
After that, make_broker() charges that wallet for each allowed intent.
"""

import sqlite3
from datetime import UTC, datetime

from ulid import ULID

from .db import write_txn
from .models import MandateClaims, PurchaseIntent
from .paypal import PayPal, PayPalError


class NotFunded(PayPalError):
    public_message = "No PayPal wallet is linked for this mandate's issuer. Nothing was charged."


def start(conn: sqlite3.Connection, pp: PayPal, user: str, return_url: str, cancel_url: str,
          now: datetime) -> str:
    setup_id, approve_url = pp.create_setup_token(return_url, cancel_url, request_id=f"setup-{ULID()}")
    with write_txn(conn):
        conn.execute(
            "INSERT INTO funding (user_id, setup_token_id, started_at) VALUES (?, ?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET setup_token_id = excluded.setup_token_id,"
            " payment_token_id = NULL, paypal_customer_id = NULL, started_at = excluded.started_at,"
            " funded_at = NULL",
            (user, setup_id, now.astimezone(UTC).isoformat()),
        )
    return approve_url


def complete(conn: sqlite3.Connection, pp: PayPal, user: str, now: datetime) -> str:
    """Returns the setup token's status. 'FUNDED' once the wallet is linked."""
    row = conn.execute("SELECT * FROM funding WHERE user_id = ?", (user,)).fetchone()
    if row is None:
        raise LookupError(f"no funding started for {user}")
    if row["payment_token_id"]:
        return "FUNDED"
    status = pp.setup_token_status(row["setup_token_id"])
    if status != "APPROVED":
        return status
    token_id, customer_id = pp.create_payment_token(row["setup_token_id"],
                                                    request_id=f"ptok-{row['setup_token_id']}")
    with write_txn(conn):
        conn.execute(
            "UPDATE funding SET payment_token_id = ?, paypal_customer_id = ?, funded_at = ? WHERE user_id = ?",
            (token_id, customer_id, now.astimezone(UTC).isoformat(), user),
        )
    return "FUNDED"


def is_funded(conn: sqlite3.Connection, user: str) -> bool:
    row = conn.execute("SELECT payment_token_id FROM funding WHERE user_id = ?", (user,)).fetchone()
    return bool(row and row["payment_token_id"])


def make_broker(conn: sqlite3.Connection, pp: PayPal):
    """The broker service.authorize calls after the policy allows and spend is reserved."""

    def broker(intent: PurchaseIntent, mandate: MandateClaims) -> str:
        user = mandate.issuer.removeprefix("user:")
        row = conn.execute("SELECT payment_token_id FROM funding WHERE user_id = ?", (user,)).fetchone()
        if not row or not row["payment_token_id"]:
            raise NotFunded(f"no wallet for {mandate.issuer}")
        return pp.charge(
            vault_id=row["payment_token_id"],
            amount_cents=intent.amount_cents,
            description=f"{intent.item} ({intent.merchant})",
            custom_id=mandate.mandate_id,
            # Same intent, same request id: if the agent retries after a timeout,
            # PayPal deduplicates the order on its side too.
            request_id=f"{mandate.mandate_id}-{intent.intent_id}",
        )

    return broker
