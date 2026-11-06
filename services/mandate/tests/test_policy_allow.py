from datetime import timedelta

from mandate import policy
from mandate.models import PurchaseIntent

from .conftest import NOW, claims, intent, raw_intent, state


def allow(v):
    assert v.allowed is True
    assert v.reason_code is None


def test_plain_allow():
    allow(policy.evaluate(intent(), claims(), state(), NOW))


def test_exactly_at_per_txn_cap():
    allow(policy.evaluate(intent(amount_cents=2500), claims(), state(), NOW))


def test_exactly_at_period_cap():
    allow(policy.evaluate(intent(amount_cents=450), claims(), state(spent_cents=4550, txn_count=1), NOW))


def test_last_allowed_transaction():
    allow(policy.evaluate(intent(), claims(), state(txn_count=1), NOW))


def test_one_second_before_expiry():
    m = claims(exp=int((NOW + timedelta(seconds=1)).timestamp()))
    allow(policy.evaluate(intent(), m, state(), NOW))


def test_any_listed_merchant_and_category():
    m = claims(merchant_allowlist=["channel3:a", "channel3:b"], category_allowlist=["coffee", "tea"])
    allow(policy.evaluate(intent(merchant="channel3:b", category="tea"), m, state(), NOW))


def test_parse_intent_returns_typed_intent():
    assert isinstance(policy.parse_intent(raw_intent()), PurchaseIntent)
