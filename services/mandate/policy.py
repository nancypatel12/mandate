"""The fence. Pure functions: same inputs, same verdict. No I/O, no clock, no model.

The service calls these in this order:
  1. tokens.verify   -> INVALID_SIGNATURE   (never read claims nobody signed)
  2. parse_intent    -> MALFORMED_INTENT    (never reason about a shape we don't know)
  3. evaluate        -> everything else

Inside evaluate, the check order is part of the contract:
  REVOKED first: a human pulled the kill switch, and that answer outranks all others.
  EXPIRED next: the authority itself has ended.
  Scope (merchant, then category) before money: an out-of-scope purchase is out of
    scope at any price, so an injected merchant reports MERCHANT_NOT_ALLOWED, not a cap.
  Per-transaction cap before the period counters: it needs no history.
  Velocity before the period cap: a runaway loop should hit the count brake by name.
The only ALLOW is the last line. Every path above it is a DENY.
"""

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from .models import MandateClaims, Period, PurchaseIntent, ReasonCode, SpendState, Verdict


def usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("clock must be timezone-aware")
    return now.astimezone(UTC)


def period_start(period: Period, now: datetime) -> datetime:
    """Calendar windows in UTC: days at 00:00, weeks on Monday, months on the 1st."""
    day = _utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if period is Period.DAY:
        return day
    if period is Period.WEEK:
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def parse_intent(raw: object) -> PurchaseIntent | Verdict:
    try:
        return PurchaseIntent.model_validate(raw)
    except ValidationError as e:
        # Name the field, never echo the value: intents can carry injected text.
        err = e.errors(include_input=False, include_url=False)[0]
        where = ".".join(str(p) for p in err["loc"]) or "intent"
        return Verdict.deny(ReasonCode.MALFORMED_INTENT, f"intent rejected at '{where}': {err['msg']}")


def evaluate(intent: PurchaseIntent, mandate: MandateClaims, state: SpendState, now: datetime) -> Verdict:
    now = _utc(now)

    if state.revoked:
        return Verdict.deny(ReasonCode.REVOKED, "This mandate was revoked. Nothing it authorized can be spent.")

    if now.timestamp() >= mandate.exp:
        ended = datetime.fromtimestamp(mandate.exp, UTC).isoformat()
        return Verdict.deny(ReasonCode.EXPIRED, f"This mandate expired at {ended}.")

    if intent.merchant not in mandate.merchant_allowlist:
        return Verdict.deny(ReasonCode.MERCHANT_NOT_ALLOWED,
                            f"{intent.merchant} is not on this mandate's merchant list.")

    if intent.category not in mandate.category_allowlist:
        return Verdict.deny(ReasonCode.CATEGORY_NOT_ALLOWED,
                            f"Category '{intent.category}' is not on this mandate's category list.")

    if intent.amount_cents > mandate.per_txn_cap_cents:
        return Verdict.deny(ReasonCode.EXCEEDS_PER_TXN_CAP,
                            f"{usd(intent.amount_cents)} is over the {usd(mandate.per_txn_cap_cents)} per-purchase cap.")

    if state.txn_count >= mandate.max_txn_per_period:
        return Verdict.deny(ReasonCode.VELOCITY_EXCEEDED,
                            f"This mandate allows {mandate.max_txn_per_period} purchases per {mandate.period}, "
                            f"and {state.txn_count} were already made.")

    total = state.spent_cents + intent.amount_cents
    if total > mandate.period_cap_cents:
        return Verdict.deny(ReasonCode.EXCEEDS_PERIOD_CAP,
                            f"{usd(intent.amount_cents)} would bring this {mandate.period}'s spend to {usd(total)}, "
                            f"over the {usd(mandate.period_cap_cents)} cap.")

    return Verdict.allow(f"{usd(intent.amount_cents)} at {intent.merchant} is within the mandate.")
