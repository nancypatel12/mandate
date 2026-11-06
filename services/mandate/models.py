"""Wire types. Everything the policy engine can see is defined here.

Data minimization (I5): a PurchaseIntent is a set of claims about one purchase:
merchant, category, amount, a short item label. There is no field for the
user's conversation, prompts or identity, and unknown fields are rejected, so
the engine cannot receive them even by accident. It decides on claims alone.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

ULID = r"[0-9A-HJKMNP-TV-Z]{26}"


class ReasonCode(StrEnum):
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    EXCEEDS_PER_TXN_CAP = "EXCEEDS_PER_TXN_CAP"
    EXCEEDS_PERIOD_CAP = "EXCEEDS_PERIOD_CAP"
    VELOCITY_EXCEEDED = "VELOCITY_EXCEEDED"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    MALFORMED_INTENT = "MALFORMED_INTENT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # PayPal failed or timed out
    INTERNAL_ERROR = "INTERNAL_ERROR"              # our own bug; still a DENY


class Period(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


# Exact, ASCII-only identifiers. Matching is string equality, so anything that
# could look alike (case, Unicode homoglyphs, trailing whitespace) is refused
# at the shape check instead of being normalized.
Merchant = Annotated[StrictStr, Field(pattern=r"^channel3:[a-z0-9][a-z0-9-]{0,63}$")]
Category = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
# Integer cents only. The $1M ceiling keeps the arithmetic boring.
Cents = Annotated[StrictInt, Field(gt=0, le=100_000_000)]


class MandateClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mandate_id: Annotated[StrictStr, Field(pattern=f"^mnd_{ULID}$")]
    subject: Annotated[StrictStr, Field(pattern=r"^agent:[a-z0-9][a-z0-9_-]{0,63}$")]
    issuer: Annotated[StrictStr, Field(pattern=r"^user:[a-z0-9][a-z0-9_-]{0,63}$")]
    iat: StrictInt
    exp: StrictInt
    merchant_allowlist: tuple[Merchant, ...] = Field(min_length=1)
    category_allowlist: tuple[Category, ...] = Field(min_length=1)
    per_txn_cap_cents: Cents
    period: Period
    period_cap_cents: Cents
    max_txn_per_period: Annotated[StrictInt, Field(gt=0, le=10_000)]
    # A mandate the human cannot kill is not one we will honor.
    revocable: Literal[True]
    revocation_id: Annotated[StrictStr, Field(pattern=f"^rev_{ULID}$")]


class PurchaseIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
    merchant: Merchant
    category: Category
    amount_cents: Cents
    currency: Literal["USD"]
    item: Annotated[StrictStr, Field(min_length=1, max_length=120)]


@dataclass(frozen=True)
class SpendState:
    """What the database knows: revocation and this period's counters."""
    revoked: bool
    spent_cents: int
    txn_count: int


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason_code: ReasonCode | None  # for machines
    message: str                    # for humans

    @classmethod
    def allow(cls, message: str) -> "Verdict":
        return cls(True, None, message)

    @classmethod
    def deny(cls, code: ReasonCode, message: str) -> "Verdict":
        return cls(False, code, message)
