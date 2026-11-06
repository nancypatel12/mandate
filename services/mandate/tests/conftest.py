from datetime import UTC, datetime, timedelta

import pytest
from ulid import ULID

from mandate import db
from mandate.models import MandateClaims, PurchaseIntent, SpendState

KEY = "test-signing-key-that-is-long-enough-0123456789"
# A Friday afternoon, so week and month windows have room on both sides.
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)


def claims(**over) -> MandateClaims:
    base = dict(
        mandate_id=f"mnd_{ULID()}",
        subject="agent:shopper",
        issuer="user:nancy",
        iat=int(NOW.timestamp()),
        exp=int((NOW + timedelta(days=1)).timestamp()),
        merchant_allowlist=["channel3:blue-bottle"],
        category_allowlist=["coffee"],
        per_txn_cap_cents=2500,
        period="day",
        period_cap_cents=5000,
        max_txn_per_period=2,
        revocable=True,
        revocation_id=f"rev_{ULID()}",
    )
    return MandateClaims(**(base | over))


def raw_intent(**over) -> dict:
    base = dict(
        intent_id=f"int_{ULID()}",
        merchant="channel3:blue-bottle",
        category="coffee",
        amount_cents=450,
        currency="USD",
        item="Bella Donovan, 12 oz",
    )
    return base | over


def intent(**over) -> PurchaseIntent:
    return PurchaseIntent(**raw_intent(**over))


def state(**over) -> SpendState:
    return SpendState(**(dict(revoked=False, spent_cents=0, txn_count=0) | over))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()
