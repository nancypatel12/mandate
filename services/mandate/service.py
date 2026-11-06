"""Issue, revoke, authorize. The only module that touches storage and the broker.

authorize() fails closed: it starts from DENY and catches everything. Its one
ALLOW comes after the policy allowed, the spend was reserved, and the broker
returned an order id.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ulid import ULID

from . import ledger, policy, spend, tokens
from .db import write_txn
from .models import MandateClaims, PurchaseIntent, ReasonCode, SpendState, Verdict

log = logging.getLogger("mandate")

MAX_TTL_SECONDS = 90 * 86_400

# The seam PayPal plugs into (M2): place the order, return its id, or raise.
Broker = Callable[[PurchaseIntent, MandateClaims], str]


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    ledger_id: str | None
    order_id: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict:
        v = self.verdict
        return {
            "verdict": "ALLOW" if v.allowed else "DENY",
            "reason_code": v.reason_code and v.reason_code.value,
            "message": v.message,
            "order_id": self.order_id,
            "ledger_id": self.ledger_id,
            "replayed": self.replayed,
        }


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


# ── human actions ─────────────────────────────────────────────────────

def issue(
    conn: sqlite3.Connection,
    key: str,
    *,
    agent: str,
    user: str,
    merchants: list[str],
    categories: list[str],
    per_txn_cap_cents: int,
    period: str,
    period_cap_cents: int,
    max_txn_per_period: int,
    ttl_seconds: int,
    now: datetime,
) -> tuple[str, MandateClaims]:
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"ttl must be between 1 second and {MAX_TTL_SECONDS // 86_400} days")
    iat = int(now.timestamp())
    claims = MandateClaims(
        mandate_id=f"mnd_{ULID()}",
        subject=f"agent:{agent}",
        issuer=f"user:{user}",
        iat=iat,
        exp=iat + ttl_seconds,
        merchant_allowlist=merchants,
        category_allowlist=categories,
        per_txn_cap_cents=per_txn_cap_cents,
        period=period,
        period_cap_cents=period_cap_cents,
        max_txn_per_period=max_txn_per_period,
        revocable=True,
        revocation_id=f"rev_{ULID()}",
    )
    token = tokens.sign(claims, key)
    with write_txn(conn):
        conn.execute(
            "INSERT INTO mandates (mandate_id, revocation_id, claims_json, issued_at) VALUES (?, ?, ?, ?)",
            (claims.mandate_id, claims.revocation_id, claims.model_dump_json(), _iso(now)),
        )
    return token, claims


def revoke(conn: sqlite3.Connection, mandate_id: str, now: datetime) -> bool:
    """Kill switch. True if the mandate exists (revoking twice is fine)."""
    with write_txn(conn):
        conn.execute(
            "UPDATE mandates SET revoked_at = ? WHERE mandate_id = ? AND revoked_at IS NULL",
            (_iso(now), mandate_id),
        )
        return conn.execute("SELECT 1 FROM mandates WHERE mandate_id = ?", (mandate_id,)).fetchone() is not None


def status(conn: sqlite3.Connection, mandate_id: str, now: datetime) -> dict | None:
    row = conn.execute("SELECT * FROM mandates WHERE mandate_id = ?", (mandate_id,)).fetchone()
    if row is None:
        return None
    claims = MandateClaims.model_validate_json(row["claims_json"])
    start = _iso(policy.period_start(claims.period, now))
    spent, count = spend.read(conn, mandate_id, start)
    return {
        "mandate": claims.model_dump(mode="json"),
        "issued_at": row["issued_at"],
        "revoked_at": row["revoked_at"],
        "period_start": start,
        "spend": {"spent_cents": spent, "txn_count": count},
    }


# ── agent action ──────────────────────────────────────────────────────

def authorize(
    conn: sqlite3.Connection,
    key: str,
    token: object,
    raw_intent: object,
    now: datetime,
    broker: Broker | None = None,
) -> Result:
    try:
        return _authorize(conn, key, token, raw_intent, now, broker)
    except Exception as e:
        # Fail closed (I3). If this happened after a reservation, the reservation stays:
        # over-counting spend is the safe direction.
        log.error("authorize failed closed: %s", type(e).__name__)
        verdict = Verdict.deny(ReasonCode.INTERNAL_ERROR, "Internal error. Denied by default.")
        try:
            return Result(verdict, ledger.append(conn, verdict, datetime.now(UTC)))
        except Exception:
            return Result(verdict, None)


def _authorize(conn, key, token, raw_intent, now, broker) -> Result:
    try:
        mandate = tokens.verify(token, key)
    except tokens.TokenInvalid:
        # Nothing from an unverified token is trusted, not even its mandate_id for the log.
        return _log(conn, Verdict.deny(ReasonCode.INVALID_SIGNATURE, "Mandate token failed verification."), now)

    intent = policy.parse_intent(raw_intent)
    if isinstance(intent, Verdict):
        return _log(conn, intent, now, mandate)

    start = _iso(policy.period_start(mandate.period, now))

    with write_txn(conn):
        replay = _replay(conn, mandate, intent, now)
        if replay:
            return replay
        verdict = policy.evaluate(intent, mandate, _state(conn, mandate, start), now)
        if not verdict.allowed:
            return _log(conn, verdict, now, mandate, intent)
        # Reserve before calling the broker, so a parallel request already sees this spend.
        spend.reserve(conn, mandate.mandate_id, start, intent.amount_cents)
        conn.execute("INSERT INTO intents (mandate_id, intent_id) VALUES (?, ?)",
                     (mandate.mandate_id, intent.intent_id))

    # Outside the transaction: never hold the write lock across a network call.
    order_id = None
    if broker is not None:
        try:
            order_id = broker(intent, mandate)
            if not isinstance(order_id, str) or not 0 < len(order_id) <= 64:
                raise ValueError("broker returned no usable order id")
        except Exception as e:
            log.warning("broker failed: %s", type(e).__name__)  # type only: messages may carry upstream detail
            with write_txn(conn):
                # No money moved, so give the budget back and let the agent retry this intent.
                spend.release(conn, mandate.mandate_id, start, intent.amount_cents)
                conn.execute("DELETE FROM intents WHERE mandate_id = ? AND intent_id = ?",
                             (mandate.mandate_id, intent.intent_id))
                message = getattr(e, "public_message", "Payment provider unavailable. Nothing was charged.")
                return _log(conn, Verdict.deny(ReasonCode.UPSTREAM_UNAVAILABLE, message), now, mandate, intent)

    with write_txn(conn):
        result = _log(conn, verdict, now, mandate, intent, order_id)
        conn.execute("UPDATE intents SET ledger_id = ? WHERE mandate_id = ? AND intent_id = ?",
                     (result.ledger_id, mandate.mandate_id, intent.intent_id))
    return result


def _state(conn, mandate: MandateClaims, start: str) -> SpendState:
    row = conn.execute("SELECT revocation_id, revoked_at FROM mandates WHERE mandate_id = ?",
                       (mandate.mandate_id,)).fetchone()
    # Authority needs the token AND a live record. No record, or a record for a
    # different revocation_id, is treated as revoked.
    live = row is not None and row["revocation_id"] == mandate.revocation_id and row["revoked_at"] is None
    spent, count = spend.read(conn, mandate.mandate_id, start)
    return SpendState(revoked=not live, spent_cents=spent, txn_count=count)


def _replay(conn, mandate: MandateClaims, intent: PurchaseIntent, now: datetime) -> Result | None:
    row = conn.execute("SELECT ledger_id FROM intents WHERE mandate_id = ? AND intent_id = ?",
                       (mandate.mandate_id, intent.intent_id)).fetchone()
    if row is None:
        return None
    if row["ledger_id"] is None:
        return _log(conn, Verdict.deny(ReasonCode.UPSTREAM_UNAVAILABLE,
                                       "An attempt with this intent_id is still in progress. Retry shortly."),
                    now, mandate, intent)
    # Same intent, same answer, no second charge. The ledger keeps one row per purchase.
    original = ledger.get(conn, row["ledger_id"])
    return Result(Verdict.allow(original["message"]), original["ledger_id"], original["order_id"], replayed=True)


def _log(conn, verdict: Verdict, now: datetime, mandate: MandateClaims | None = None,
         intent: PurchaseIntent | None = None, order_id: str | None = None) -> Result:
    lid = ledger.append(conn, verdict, now, mandate_id=mandate and mandate.mandate_id,
                        intent=intent, order_id=order_id)
    return Result(verdict, lid, order_id)

