import httpx
import pytest

from mandate.paypal import PayPal, PayPalError, usd_value

from .fake_paypal import CLIENT_ID, CLIENT_SECRET, FakePayPal


@pytest.fixture
def fake():
    return FakePayPal()


@pytest.fixture
def pp(fake):
    return PayPal(CLIENT_ID, CLIENT_SECRET, transport=fake.transport())


def charge(pp, cents=450):
    return pp.charge("VAULT-1", cents, "Bella Donovan (channel3:blue-bottle)", "mnd_1", "mnd_1-int_1")


def test_usd_value():
    assert [usd_value(c) for c in (1, 450, 2500, 100_000)] == ["0.01", "4.50", "25.00", "1000.00"]


@pytest.mark.parametrize("url", ["https://api-m.paypal.com", "http://api-m.sandbox.paypal.com",
                                 "https://evil.example.com"])
def test_refuses_anything_but_sandbox(url):
    with pytest.raises(ValueError):
        PayPal(CLIENT_ID, CLIENT_SECRET, base_url=url)


def test_requires_credentials():
    with pytest.raises(ValueError):
        PayPal("", CLIENT_SECRET)


def test_charge_completed_on_create(pp, fake):
    assert charge(pp) == "ORDER-1"
    method, path, headers, body = fake.calls[-1]
    assert path == "/v2/checkout/orders"
    assert headers["paypal-request-id"] == "mnd_1-int_1"
    assert body["payment_source"] == {"paypal": {"vault_id": "VAULT-1"}}
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "4.50"}


def test_charge_captures_when_not_completed_on_create(pp, fake):
    fake.order_status = "APPROVED"
    assert charge(pp) == "ORDER-1"
    assert fake.calls[-1][1] == "/v2/checkout/orders/ORDER-1/capture"


def test_access_token_is_cached(pp, fake):
    charge(pp)
    charge(pp)
    assert fake.tokens_issued == 1


def test_expired_access_token_is_refreshed_once(pp, fake):
    charge(pp)
    fake.expire_next = True
    assert charge(pp) == "ORDER-1"
    assert fake.tokens_issued == 2


# ── every way PayPal can fail is a PayPalError (→ UPSTREAM_UNAVAILABLE) ──

@pytest.mark.parametrize("failure", [
    httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR", "debug_id": "dbg1"}),
    httpx.Response(503, text="<html>maintenance</html>"),
    httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYER_ACTION_REQUIRED"}]}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(201, json={"status": "COMPLETED"}),  # no order id
])
def test_order_failures(pp, fake, failure):
    fake.fail["/v2/checkout/orders"] = failure
    with pytest.raises(PayPalError):
        charge(pp)


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("x")])
def test_network_failures_raise(pp, fake, exc):
    fake.fail["/v2/checkout/orders"] = exc
    with pytest.raises(httpx.HTTPError):  # service.authorize catches every exception
        charge(pp)


def test_oauth_failure(pp, fake):
    fake.fail["/v1/oauth2/token"] = httpx.Response(401, json={"error": "invalid_client"})
    with pytest.raises(PayPalError, match="oauth"):
        charge(pp)


def test_oauth_garbage(pp, fake):
    fake.fail["/v1/oauth2/token"] = httpx.Response(200, json={"access_token": 5})
    with pytest.raises(PayPalError):
        charge(pp)


def test_persistent_401_is_not_retried_forever(pp, fake):
    charge(pp)
    fake.fail["/v2/checkout/orders"] = httpx.Response(401, json={"error": "invalid_token"})
    with pytest.raises(PayPalError):
        charge(pp)
    assert sum(1 for c in fake.calls if c[1] == "/v2/checkout/orders") == 3  # first ok, then 2 tries


def test_capture_pending_is_not_success(pp, fake):
    fake.capture_status = "PENDING"
    with pytest.raises(PayPalError, match="PENDING"):
        charge(pp)


def test_captured_amount_must_match(pp, fake):
    fake.captured_value = "45.00"
    with pytest.raises(PayPalError, match="amount"):
        charge(pp)


def test_capture_missing_from_response(pp, fake):
    fake.fail["/v2/checkout/orders"] = httpx.Response(201, json={"id": "ORDER-1", "status": "COMPLETED"})
    with pytest.raises(PayPalError, match="no capture"):
        charge(pp)


def test_errors_never_contain_secrets(pp, fake):
    fake.fail["/v2/checkout/orders"] = httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR", "debug_id": "dbg1"})
    with pytest.raises(PayPalError) as e:
        charge(pp)
    msg = str(e.value)
    assert "dbg1" in msg
    assert CLIENT_SECRET not in msg and CLIENT_ID not in msg and "A21-" not in msg


# ── vault ────────────────────────────────────────────────────────────

def test_setup_token(pp, fake):
    sid, url = pp.create_setup_token("http://localhost/r", "http://localhost/c", request_id="setup-1")
    assert sid == "SETUP-1" and url.startswith("https://www.sandbox.paypal.com/")
    body = fake.calls[-1][3]["payment_source"]["paypal"]
    assert body["usage_type"] == "MERCHANT" and body["experience_context"]["return_url"] == "http://localhost/r"


def test_setup_token_without_approve_link(pp, fake):
    fake.fail["/v3/vault/setup-tokens"] = httpx.Response(201, json={"id": "SETUP-1", "links": []})
    with pytest.raises(PayPalError):
        pp.create_setup_token("r", "c", request_id="s")


def test_payment_token(pp, fake):
    assert pp.create_payment_token("SETUP-1", request_id="p") == ("VAULT-1", "cust_1")


def test_payment_token_without_id(pp, fake):
    fake.fail["/v3/vault/payment-tokens"] = httpx.Response(201, json={})
    with pytest.raises(PayPalError):
        pp.create_payment_token("SETUP-1", request_id="p")
