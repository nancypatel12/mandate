"""Denials are the product. Every reason code, every boundary, every ordering rule."""

from datetime import timedelta

import pytest

from mandate import policy
from mandate.models import ReasonCode as R
from mandate.models import Verdict

from .conftest import NOW, claims, intent, raw_intent, state


def deny(code, v: Verdict):
    assert v.allowed is False
    assert v.reason_code is code
    assert v.message  # humans get a sentence, machines get the code


# ── one test per reason code the engine owns ──────────────────────────

def test_revoked():
    deny(R.REVOKED, policy.evaluate(intent(), claims(), state(revoked=True), NOW))


def test_expired_at_exact_expiry():
    m = claims(exp=int(NOW.timestamp()))
    deny(R.EXPIRED, policy.evaluate(intent(), m, state(), NOW))


def test_expired_long_ago():
    m = claims(exp=int((NOW - timedelta(days=30)).timestamp()))
    deny(R.EXPIRED, policy.evaluate(intent(), m, state(), NOW))


def test_merchant_not_allowed():
    deny(R.MERCHANT_NOT_ALLOWED,
         policy.evaluate(intent(merchant="channel3:evil-shop"), claims(), state(), NOW))


def test_merchant_match_is_exact_not_prefix():
    deny(R.MERCHANT_NOT_ALLOWED,
         policy.evaluate(intent(merchant="channel3:blue-bottle-outlet"), claims(), state(), NOW))


def test_category_not_allowed():
    deny(R.CATEGORY_NOT_ALLOWED,
         policy.evaluate(intent(category="gift-cards"), claims(), state(), NOW))


def test_exceeds_per_txn_cap_by_one_cent():
    deny(R.EXCEEDS_PER_TXN_CAP, policy.evaluate(intent(amount_cents=2501), claims(), state(), NOW))


def test_velocity_exceeded():
    deny(R.VELOCITY_EXCEEDED, policy.evaluate(intent(), claims(), state(txn_count=2), NOW))


def test_exceeds_period_cap_by_one_cent():
    s = state(spent_cents=4551, txn_count=1)
    deny(R.EXCEEDS_PERIOD_CAP, policy.evaluate(intent(amount_cents=450), claims(), s, NOW))


# ── MALFORMED_INTENT: the shape check runs before any policy ──────────

@pytest.mark.parametrize("raw", [
    None,
    [],
    "buy coffee",
    42,
    {},
    raw_intent(amount_cents="450"),            # strings are not money
    raw_intent(amount_cents=4.5),              # neither are floats
    raw_intent(amount_cents=True),             # bool is an int in Python; not here
    raw_intent(amount_cents=0),
    raw_intent(amount_cents=-450),
    raw_intent(amount_cents=10**12),
    raw_intent(currency="EUR"),
    raw_intent(merchant="blue-bottle"),        # missing the channel3: namespace
    raw_intent(merchant="channel3:Blue-Bottle"),
    raw_intent(merchant="channel3:blue-bottle\n"),
    raw_intent(merchant="channel3:blue-bоttle"),  # Cyrillic о
    raw_intent(category="Coffee"),
    raw_intent(intent_id="short"),
    raw_intent(item=""),
    raw_intent(item="x" * 121),
    raw_intent(conversation="the user said buy anything"),  # I5: no room for prompts
    {k: v for k, v in raw_intent().items() if k != "merchant"},
])
def test_malformed_intent(raw):
    v = policy.parse_intent(raw)
    deny(R.MALFORMED_INTENT, v)


def test_malformed_message_does_not_echo_input():
    v = policy.parse_intent(raw_intent(merchant="channel3:IGNORE-PREVIOUS-INSTRUCTIONS"))
    assert "IGNORE" not in v.message


# ── ordering: which reason wins when several apply ────────────────────

def test_revoked_beats_expired():
    m = claims(exp=int((NOW - timedelta(days=1)).timestamp()))
    deny(R.REVOKED, policy.evaluate(intent(), m, state(revoked=True), NOW))


def test_expired_beats_scope():
    m = claims(exp=int(NOW.timestamp()))
    deny(R.EXPIRED, policy.evaluate(intent(merchant="channel3:evil-shop"), m, state(), NOW))


def test_injected_merchant_reports_merchant_not_cap():
    # A compromised agent tries an off-list merchant AND a huge amount.
    # The answer must name the scope violation, not the price.
    i = intent(merchant="channel3:evil-shop", amount_cents=99_999)
    deny(R.MERCHANT_NOT_ALLOWED, policy.evaluate(i, claims(), state(txn_count=9), NOW))


def test_merchant_beats_category():
    i = intent(merchant="channel3:evil-shop", category="gift-cards")
    deny(R.MERCHANT_NOT_ALLOWED, policy.evaluate(i, claims(), state(), NOW))


def test_category_beats_amount():
    i = intent(category="gift-cards", amount_cents=99_999)
    deny(R.CATEGORY_NOT_ALLOWED, policy.evaluate(i, claims(), state(), NOW))


def test_per_txn_beats_velocity():
    deny(R.EXCEEDS_PER_TXN_CAP,
         policy.evaluate(intent(amount_cents=3000), claims(), state(txn_count=2), NOW))


def test_velocity_beats_period_cap():
    deny(R.VELOCITY_EXCEEDED,
         policy.evaluate(intent(), claims(), state(spent_cents=5000, txn_count=2), NOW))


# ── determinism ───────────────────────────────────────────────────────

def test_same_inputs_same_verdict():
    args = (intent(merchant="channel3:evil-shop"), claims(), state(), NOW)
    assert len({policy.evaluate(*args) for _ in range(100)}) == 1


def test_naive_clock_is_refused():
    with pytest.raises(ValueError):
        policy.evaluate(intent(), claims(), state(), NOW.replace(tzinfo=None))
