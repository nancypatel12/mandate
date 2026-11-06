"""A scripted stand-in for PayPal's HTTP API, used only in tests.

It sits behind httpx.MockTransport, so the real client code (paypal.py) runs
unchanged. The live sandbox run is the proof; this is how we test the failures
PayPal won't produce on demand.
"""

import json

import httpx

CLIENT_ID, CLIENT_SECRET = "test-client-id", "test-client-secret-do-not-log"


class FakePayPal:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.order_status = "COMPLETED"   # what create-order returns
        self.capture_status = "COMPLETED"
        self.captured_value: str | None = None  # override to simulate an amount mismatch
        self.setup_status = "PAYER_ACTION_REQUIRED"
        self.fail: dict[str, httpx.Response | Exception] = {}  # path prefix → failure
        self.tokens_issued = 0
        self.expire_next = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = json.loads(req.content) if req.content and path != "/v1/oauth2/token" else None
        self.calls.append((req.method, path, dict(req.headers), body))
        for prefix, failure in self.fail.items():
            if path.startswith(prefix):
                if isinstance(failure, Exception):
                    raise failure
                return failure

        if path == "/v1/oauth2/token":
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"A21-{self.tokens_issued}", "expires_in": 32000})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"error": "invalid_token"})
        if req.headers.get("authorization") != f"Bearer A21-{self.tokens_issued}":
            return httpx.Response(401, json={"error": "invalid_token"})

        if path == "/v3/vault/setup-tokens" and req.method == "POST":
            return httpx.Response(201, json={
                "id": "SETUP-1", "status": "PAYER_ACTION_REQUIRED",
                "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/agreements/approve?x=1"}],
            })
        if path.startswith("/v3/vault/setup-tokens/"):
            return httpx.Response(200, json={"id": "SETUP-1", "status": self.setup_status})
        if path == "/v3/vault/payment-tokens":
            return httpx.Response(201, json={"id": "VAULT-1", "customer": {"id": "cust_1"}})
        if path == "/v2/checkout/orders":
            value = body["purchase_units"][0]["amount"]["value"]
            return httpx.Response(201, json=self._order(self.order_status, value))
        if path.endswith("/capture"):
            value = self.calls[-2][3]["purchase_units"][0]["amount"]["value"]
            return httpx.Response(201, json=self._order("COMPLETED", value))
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "debug_id": "dbg404"})

    def _order(self, status: str, value: str) -> dict:
        order = {"id": "ORDER-1", "status": status}
        if status == "COMPLETED":
            order["purchase_units"] = [{"payments": {"captures": [{
                "id": "CAP-1", "status": self.capture_status,
                "amount": {"currency_code": "USD", "value": self.captured_value or value},
            }]}}]
        return order
