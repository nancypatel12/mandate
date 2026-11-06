import pytest
from fastapi.testclient import TestClient

from mandate import db, funding
from mandate.main import Settings, create_app, settings_from_env
from mandate.paypal import PayPal

from .conftest import KEY, NOW, raw_intent
from .fake_paypal import CLIENT_ID, CLIENT_SECRET, FakePayPal

ADMIN = "admin-token-for-tests-0123456789abcdef"
H = {"Authorization": f"Bearer {ADMIN}"}
MANDATE = dict(agent="shopper", user="nancy", merchants=["channel3:blue-bottle"], categories=["coffee"],
               per_txn_cap_cents=2500, period="day", period_cap_cents=5000, max_txn_per_period=2)


@pytest.fixture
def fake():
    return FakePayPal()


@pytest.fixture
def make(tmp_path, fake):
    def _make(paypal=True, admin=ADMIN):
        pp = PayPal(CLIENT_ID, CLIENT_SECRET, transport=fake.transport()) if paypal else None
        path = str(tmp_path / "api.db")
        if pp:
            c = db.connect(path)
            funding.start(c, pp, "nancy", "r", "r", NOW)
            fake.setup_status = "APPROVED"
            funding.complete(c, pp, "nancy", NOW)
            c.close()
        return TestClient(create_app(Settings(KEY, admin, path, pp)))
    return _make


def issue(client):
    r = client.post("/mandates", json=MANDATE, headers=H)
    assert r.status_code == 201
    return r.json()["token"], r.json()["mandate"]["mandate_id"]


def buy(client, token, **over):
    return client.post("/authorize", json=raw_intent(**over), headers={"Authorization": f"Bearer {token}"})


def test_happy_path(make):
    c = make()
    token, mid = issue(c)
    r = buy(c, token)
    assert r.status_code == 200
    assert r.json()["verdict"] == "ALLOW" and r.json()["order_id"] == "ORDER-1"
    assert c.get(f"/mandates/{mid}").json()["spend"] == {"spent_cents": 450, "txn_count": 1}
    assert c.get("/ledger").json()[0]["order_id"] == "ORDER-1"


def test_denial_is_200_with_reason(make):
    c = make()
    token, _ = issue(c)
    r = buy(c, token, merchant="channel3:evil-shop")
    assert r.status_code == 200 and r.json()["reason_code"] == "MERCHANT_NOT_ALLOWED"


def test_revoke_then_buy(make):
    c = make()
    token, mid = issue(c)
    assert c.post(f"/mandates/{mid}/revoke", headers=H).json()["revoked"] is True
    assert buy(c, token).json()["reason_code"] == "REVOKED"


def test_agent_cannot_issue_or_revoke(make):
    c = make()
    token, mid = issue(c)
    agent = {"Authorization": f"Bearer {token}"}
    assert c.post("/mandates", json=MANDATE, headers=agent).status_code == 401
    assert c.post("/mandates", json=MANDATE).status_code == 401
    assert c.post(f"/mandates/{mid}/revoke", headers=agent).status_code == 401


def test_admin_disabled_without_token(make):
    c = make(admin="")
    assert c.post("/mandates", json=MANDATE, headers={"Authorization": "Bearer "}).status_code == 503


def test_bad_mandate_is_422(make):
    c = make()
    assert c.post("/mandates", json=MANDATE | {"period": "year"}, headers=H).status_code == 422
    assert c.post("/mandates", json=MANDATE | {"ttl_seconds": 0}, headers=H).status_code == 422


def test_unknown_mandate_404(make):
    c = make()
    assert c.get("/mandates/mnd_nope").status_code == 404
    assert c.post("/mandates/mnd_nope/revoke", headers=H).status_code == 404


@pytest.mark.parametrize("body", [b"not json", b"", b"[1,2]", b"{" + b" " * 20_000 + b"}"])
def test_malformed_body_is_a_ledgered_deny(make, body):
    c = make()
    token, _ = issue(c)
    r = c.post("/authorize", content=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["reason_code"] == "MALFORMED_INTENT"
    assert c.get("/ledger").json()[0]["reason_code"] == "MALFORMED_INTENT"


def test_missing_bearer_is_invalid_signature(make):
    c = make()
    issue(c)
    r = c.post("/authorize", json=raw_intent())
    assert r.json()["reason_code"] == "INVALID_SIGNATURE"


def test_no_paypal_means_no_allow(make):
    c = make(paypal=False)
    token, _ = issue(c)
    r = buy(c, token)
    assert r.json()["verdict"] == "DENY" and r.json()["reason_code"] == "UPSTREAM_UNAVAILABLE"
    assert c.get("/healthz").json() == {"ok": True, "paypal": "missing"}


def test_paypal_down(make, fake):
    c = make()
    token, _ = issue(c)
    import httpx
    fake.fail["/v2/checkout/orders"] = httpx.Response(503)
    assert buy(c, token).json()["reason_code"] == "UPSTREAM_UNAVAILABLE"


def test_ledger_pagination(make):
    c = make()
    token, _ = issue(c)
    for _ in range(3):
        buy(c, token, merchant="channel3:evil-shop")
    first = c.get("/ledger?limit=2").json()
    rest = c.get(f"/ledger?limit=2&before={first[-1]['seq']}").json()
    assert len(first) == 2 and len(rest) == 1


def test_settings_from_env():
    s = settings_from_env({"MANDATE_SIGNING_KEY": KEY, "PAYPAL_CLIENT_ID": "a", "PAYPAL_CLIENT_SECRET": "b"})
    assert s.paypal is not None and s.db_path == "mandate.db"
    assert settings_from_env({"MANDATE_SIGNING_KEY": KEY}).paypal is None
    with pytest.raises(SystemExit):
        settings_from_env({"MANDATE_SIGNING_KEY": "short"})
    with pytest.raises(ValueError):
        settings_from_env({"MANDATE_SIGNING_KEY": KEY, "PAYPAL_CLIENT_ID": "a", "PAYPAL_CLIENT_SECRET": "b",
                           "PAYPAL_BASE_URL": "https://api-m.paypal.com"})
