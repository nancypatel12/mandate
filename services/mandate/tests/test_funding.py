import httpx
import pytest

from mandate import funding, ledger, service
from mandate.models import ReasonCode as R
from mandate.paypal import PayPal

from .conftest import KEY, NOW, raw_intent
from .fake_paypal import CLIENT_ID, CLIENT_SECRET, FakePayPal
from .test_service import issue


@pytest.fixture
def fake():
    return FakePayPal()


@pytest.fixture
def pp(fake):
    return PayPal(CLIENT_ID, CLIENT_SECRET, transport=fake.transport())


def fund(conn, pp, fake, user="nancy"):
    funding.start(conn, pp, user, "http://localhost/r", "http://localhost/r", NOW)
    fake.setup_status = "APPROVED"
    assert funding.complete(conn, pp, user, NOW) == "FUNDED"


def test_complete_waits_for_approval(conn, pp, fake):
    url = funding.start(conn, pp, "nancy", "r", "r", NOW)
    assert url.startswith("https://www.sandbox.paypal.com/")
    assert funding.complete(conn, pp, "nancy", NOW) == "PAYER_ACTION_REQUIRED"
    assert not funding.is_funded(conn, "nancy")
    fake.setup_status = "APPROVED"
    assert funding.complete(conn, pp, "nancy", NOW) == "FUNDED"
    assert funding.is_funded(conn, "nancy")
    assert funding.complete(conn, pp, "nancy", NOW) == "FUNDED"  # idempotent


def test_complete_without_start(conn, pp):
    with pytest.raises(LookupError):
        funding.complete(conn, pp, "nobody", NOW)


def test_restart_unlinks_old_wallet(conn, pp, fake):
    fund(conn, pp, fake)
    funding.start(conn, pp, "nancy", "r", "r", NOW)
    assert not funding.is_funded(conn, "nancy")


# ── the broker inside authorize ──────────────────────────────────────

def test_allow_places_a_real_order(conn, pp, fake):
    fund(conn, pp, fake)
    token, m = issue(conn)
    r = service.authorize(conn, KEY, token, raw_intent(), NOW, funding.make_broker(conn, pp))
    assert r.verdict.allowed and r.order_id == "ORDER-1"
    body = fake.calls[-1][3]
    assert body["purchase_units"][0]["custom_id"] == m.mandate_id


def test_deny_never_calls_paypal(conn, pp, fake):
    fund(conn, pp, fake)
    token, _ = issue(conn)
    before = len(fake.calls)
    r = service.authorize(conn, KEY, token, raw_intent(merchant="channel3:evil-shop"), NOW,
                          funding.make_broker(conn, pp))
    assert r.verdict.reason_code is R.MERCHANT_NOT_ALLOWED
    assert len(fake.calls) == before


def test_unfunded_issuer_fails_closed(conn, pp):
    token, _ = issue(conn)
    r = service.authorize(conn, KEY, token, raw_intent(), NOW, funding.make_broker(conn, pp))
    assert r.verdict.reason_code is R.UPSTREAM_UNAVAILABLE
    assert "No PayPal wallet" in r.verdict.message


def test_paypal_down_fails_closed_and_ledgers_it(conn, pp, fake):
    fund(conn, pp, fake)
    token, m = issue(conn)
    fake.fail["/v2/checkout/orders"] = httpx.ReadTimeout("slow")
    r = service.authorize(conn, KEY, token, raw_intent(), NOW, funding.make_broker(conn, pp))
    assert r.verdict.reason_code is R.UPSTREAM_UNAVAILABLE and r.order_id is None
    assert ledger.get(conn, r.ledger_id)["reason_code"] == "UPSTREAM_UNAVAILABLE"
    assert service.status(conn, m.mandate_id, NOW)["spend"]["txn_count"] == 0


def test_payment_token_never_in_ledger(conn, pp, fake):
    fund(conn, pp, fake)
    token, _ = issue(conn)
    service.authorize(conn, KEY, token, raw_intent(), NOW, funding.make_broker(conn, pp))
    rows = ledger.page(conn)
    assert "VAULT-1" not in str(rows)
