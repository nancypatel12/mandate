"""The four demo beats, deterministic. Zero LLM calls: this file imports no model client.

  1. allow             an in-scope purchase -> ALLOW + a real PayPal sandbox order
  2. cap-deny          same merchant, over the per-purchase cap -> EXCEEDS_PER_TXN_CAP
  3. injection-blocked the intent a real agent produced after reading a tampered
                       product page (recorded in scripts/intents/injected.json)
                       -> MERCHANT_NOT_ALLOWED
  4. revoke            pull the kill switch, retry beat 1 -> REVOKED

Each run signs a fresh mandate, so it runs clean any number of times in a row.
"""

import json
import os
import sys
from pathlib import Path

import httpx
from ulid import ULID

URL = os.environ.get("MANDATE_URL", "http://localhost:8000").rstrip("/")
ADMIN = {"Authorization": f"Bearer {os.environ.get('MANDATE_ADMIN_TOKEN', '')}"}
HERE = Path(__file__).parent

# Real products from the Channel3 snapshot (services/agent/catalog.json).
FILTERS = {"merchant": "channel3:target-com", "category": "coffee-filters", "amount_cents": 999,
           "currency": "USD", "item": "AeroPress Paper Coffee Filters"}
DRIPPER = {"merchant": "channel3:wayfair-com", "category": "drippers", "amount_cents": 1799,
           "currency": "USD", "item": "Dowan Pour Over Coffee Dripper"}
MANDATE = {"agent": "shopper", "user": os.environ.get("DEMO_USER", "nancy"),
           "merchants": ["channel3:target-com", "channel3:wayfair-com"],
           "categories": ["coffee-filters", "drippers"],
           "per_txn_cap_cents": 1500, "period": "day", "period_cap_cents": 4000,
           "max_txn_per_period": 3, "ttl_seconds": 3600}


def fresh(intent: dict) -> dict:
    return intent | {"intent_id": f"int_{ULID()}"}


def main() -> int:
    http = httpx.Client(base_url=URL, timeout=30)
    health = http.get("/healthz").json()
    if health.get("paypal") != "configured":
        sys.exit("The mandate service has no PayPal credentials. Fill .env (see .env.example).")

    r = http.post("/mandates", json=MANDATE, headers=ADMIN)
    if r.status_code != 201:
        sys.exit(f"Could not sign a mandate (HTTP {r.status_code}). Is MANDATE_ADMIN_TOKEN set?")
    token, mid = r.json()["token"], r.json()["mandate"]["mandate_id"]
    agent = {"Authorization": f"Bearer {token}"}
    print(f"Signed {mid}: Target + Wayfair, coffee filters + drippers, $15.00 per purchase, "
          f"$40.00 per day, 3 purchases a day.\n")

    injected = json.loads((HERE / "intents" / "injected.json").read_text())["intent"]
    beats = [
        ("1 allow", fresh(FILTERS), "ALLOW"),
        ("2 cap-deny", fresh(DRIPPER), "EXCEEDS_PER_TXN_CAP"),
        ("3 injection-blocked", fresh(injected), "MERCHANT_NOT_ALLOWED"),
        ("4 revoke", None, "REVOKED"),
    ]

    ok = True
    print(f"{'beat':<22}{'merchant':<34}{'amount':>9}  {'verdict':<8}{'reason_code':<22}{'order_id':<20}result")
    for name, intent, expect in beats:
        if intent is None:
            http.post(f"/mandates/{mid}/revoke", headers=ADMIN).raise_for_status()
            intent = fresh(FILTERS)
        v = http.post("/authorize", json=intent, headers=agent).json()
        got = "ALLOW" if v["verdict"] == "ALLOW" else v["reason_code"]
        passed = got == expect and (expect != "ALLOW" or bool(v["order_id"]))
        ok &= passed
        amount = f"${intent['amount_cents'] / 100:,.2f}" if isinstance(intent.get("amount_cents"), int) else "?"
        print(f"{name:<22}{str(intent.get('merchant')):<34}{amount:>9}  {v['verdict']:<8}"
              f"{(v['reason_code'] or '-'):<22}{(v['order_id'] or '-'):<20}{'PASS' if passed else 'FAIL'}")
        if not passed:
            print(f"    expected {expect}: {v['message']}")

    # Proof of "zero LLM calls": no model client was ever imported.
    assert not any(m.split(".")[0] in ("openai", "agent") for m in sys.modules), "demo must not load an LLM client"
    print("\nAll four beats passed. No LLM was called." if ok else "\nSome beats FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
