"""PayPal sandbox client: OAuth, the vault, and Orders v2. Only the mandate service imports this (I1).

Why the vault: an Orders v2 order normally needs the buyer to click "approve" in a
browser, and an agent has no browser and no buyer. So the human approves once, when
they fund their mandates. PayPal returns a payment token, which stays in our
database. Each allowed purchase then charges that token server-side. The agent never
sees the token, the client secret, or an access token.

Every failure here raises PayPalError, and service.authorize turns any exception into
UPSTREAM_UNAVAILABLE. Error messages carry PayPal's error name and debug_id only, never
credentials or response bodies.
"""

import threading
import time

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class PayPalError(Exception):
    pass


def usd_value(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


class PayPal:
    def __init__(self, client_id: str, client_secret: str, base_url: str = SANDBOX,
                 transport: httpx.BaseTransport | None = None):
        # I7 and "never touch live": refuse anything but the sandbox host.
        if base_url.rstrip("/") != SANDBOX:
            raise ValueError(f"PAYPAL_BASE_URL must be {SANDBOX}")
        if not client_id or not client_secret:
            raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required")
        self._auth = (client_id, client_secret)
        self._http = httpx.Client(base_url=SANDBOX, timeout=TIMEOUT, transport=transport)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_until = 0.0

    # ── plumbing ──────────────────────────────────────────────────────

    def _access_token(self, refresh: bool = False) -> str:
        with self._lock:
            if refresh or not self._token or time.monotonic() >= self._token_until:
                r = self._http.post("/v1/oauth2/token", auth=self._auth,
                                    data={"grant_type": "client_credentials"})
                body = self._json(r, "oauth")
                token, ttl = body.get("access_token"), body.get("expires_in")
                if not isinstance(token, str) or not isinstance(ttl, int):
                    raise PayPalError("oauth: unexpected response")
                # Refresh a minute early so a token never expires mid-request.
                self._token, self._token_until = token, time.monotonic() + max(ttl - 60, 0)
            return self._token

    def _call(self, method: str, path: str, what: str, json: dict | None = None,
              request_id: str | None = None) -> dict:
        headers = {"PayPal-Request-Id": request_id} if request_id else {}
        for attempt in (0, 1):
            headers["Authorization"] = f"Bearer {self._access_token(refresh=attempt == 1)}"
            r = self._http.request(method, path, json=json, headers=headers)
            # One retry, only for an expired access token. Never retry a charge on 5xx:
            # we can't know whether it went through, so we fail closed and let the
            # agent retry with the same intent_id (same PayPal-Request-Id).
            if r.status_code != 401:
                break
        return self._json(r, what)

    @staticmethod
    def _json(r: httpx.Response, what: str) -> dict:
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 or not isinstance(body, dict):
            name = body.get("name") or body.get("error") if isinstance(body, dict) else None
            debug = body.get("debug_id") if isinstance(body, dict) else None
            raise PayPalError(f"{what}: HTTP {r.status_code} {name or ''} debug_id={debug}".strip())
        return body

    # ── vault: the human approves once ────────────────────────────────

    def create_setup_token(self, return_url: str, cancel_url: str, request_id: str) -> tuple[str, str]:
        """Returns (setup_token_id, approve_url). The human opens approve_url."""
        body = self._call("POST", "/v3/vault/setup-tokens", "setup-token", request_id=request_id, json={
            "payment_source": {"paypal": {
                "description": "MANDATE: purchases by your AI agent, within mandates you sign",
                "usage_type": "MERCHANT",
                "customer_type": "CONSUMER",
                # The agent buys whenever the mandate allows, not on a schedule.
                "usage_pattern": "UNSCHEDULED_POSTPAID",
                "permit_multiple_payment_tokens": False,
                "experience_context": {
                    "brand_name": "MANDATE",
                    "shipping_preference": "NO_SHIPPING",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            }},
        })
        approve = next((l.get("href") for l in body.get("links", []) if l.get("rel") == "approve"), None)
        if not isinstance(body.get("id"), str) or not isinstance(approve, str):
            raise PayPalError("setup-token: no id or approve link")
        return body["id"], approve

    def setup_token_status(self, setup_token_id: str) -> str:
        body = self._call("GET", f"/v3/vault/setup-tokens/{setup_token_id}", "setup-token-status")
        return str(body.get("status", ""))

    def create_payment_token(self, setup_token_id: str, request_id: str) -> tuple[str, str | None]:
        """Exchange an approved setup token. Returns (payment_token_id, paypal_customer_id)."""
        body = self._call("POST", "/v3/vault/payment-tokens", "payment-token", request_id=request_id, json={
            "payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}},
        })
        if not isinstance(body.get("id"), str):
            raise PayPalError("payment-token: no id")
        return body["id"], (body.get("customer") or {}).get("id")

    # ── orders: one charge per allowed intent ─────────────────────────

    def charge(self, vault_id: str, amount_cents: int, description: str, custom_id: str,
               request_id: str) -> str:
        """Create and capture an order against the vaulted wallet. Returns the order id
        only if PayPal reports the capture COMPLETED for exactly the amount we asked."""
        value = usd_value(amount_cents)
        order = self._call("POST", "/v2/checkout/orders", "create-order", request_id=request_id, json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "USD", "value": value},
                "description": description[:127],
                "custom_id": custom_id[:127],
            }],
            "payment_source": {"paypal": {"vault_id": vault_id}},
        })
        order_id = order.get("id")
        if not isinstance(order_id, str):
            raise PayPalError("create-order: no id")
        # With a vaulted source PayPal usually captures on create. If not, capture now.
        if order.get("status") != "COMPLETED":
            order = self._call("POST", f"/v2/checkout/orders/{order_id}/capture", "capture",
                               request_id=f"{request_id}-capture")
        _check_captured(order, value)
        return order_id


def _check_captured(order: dict, value: str) -> None:
    try:
        cap = order["purchase_units"][0]["payments"]["captures"][0]
        status, amount = cap["status"], cap["amount"]
    except (KeyError, IndexError, TypeError):
        raise PayPalError("order: no capture in response") from None
    if order.get("status") != "COMPLETED" or status != "COMPLETED":
        raise PayPalError(f"order: capture {status}, not COMPLETED")
    if amount.get("value") != value or amount.get("currency_code") != "USD":
        raise PayPalError("order: captured amount differs from the intent")
