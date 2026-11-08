"""Red team: every attack in redteam/cases/*.yaml against the real mandate service.

    uv run python redteam/run.py            # offline: PayPal is a recorder, no money moves
    uv run python redteam/run.py --live     # real PayPal sandbox (needs .env and a funded wallet)
    uv run python redteam/run.py --markdown # also write redteam/RESULTS.md

Each case gets a fresh database and talks to the real FastAPI app over HTTP
(in-process). Only the payment rail changes between modes. The fence under test
is the same code that ships.
"""

import argparse
import base64
import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import yaml
from fastapi.testclient import TestClient
from ulid import ULID

from mandate import db, service, tokens
from mandate.main import Settings, create_app
from mandate.paypal import PayPal

ROOT = Path(__file__).resolve().parents[1]
KEY = secrets.token_urlsafe(48)
ADMIN = secrets.token_urlsafe(48)
H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}
USER = "nancy"

MANDATE = {"agent": "shopper", "user": USER,
           "merchants": ["channel3:target-com", "channel3:wayfair-com"],
           "categories": ["coffee-filters", "drippers"],
           "per_txn_cap_cents": 1500, "period": "day", "period_cap_cents": 4000,
           "max_txn_per_period": 3, "ttl_seconds": 3600}
INTENT = {"merchant": "channel3:target-com", "category": "coffee-filters", "amount_cents": 999,
          "currency": "USD", "item": "AeroPress Paper Coffee Filters"}


# ── payment rails ─────────────────────────────────────────────────────

class Recorder:
    """Offline stand-in for PayPal: counts charges, returns a labeled id, moves no money."""

    def charge(self, vault_id, amount_cents, description, custom_id, request_id):
        return f"OFFLINE-{request_id[-8:]}"


class Counted:
    """Wraps any rail and counts how many times the service tried to charge."""

    def __init__(self, inner):
        self.inner, self.calls, self._lock = inner, 0, threading.Lock()

    def charge(self, **kw):
        with self._lock:
            self.calls += 1
        return self.inner.charge(**kw)


def unreachable_paypal() -> PayPal:
    """The real PayPal client, with a network where every connection is refused."""
    def refuse(req):
        raise httpx.ConnectError("connection refused", request=req)
    return PayPal("redteam-client", "redteam-secret", transport=httpx.MockTransport(refuse))


def live_paypal() -> tuple[PayPal, str]:
    pp = PayPal(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"],
                os.environ.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"))
    src = sqlite3.connect(os.environ.get("MANDATE_DB", str(ROOT / "data" / "mandate.db")))
    row = src.execute("SELECT payment_token_id FROM funding WHERE user_id = ?", (USER,)).fetchone()
    if not row or not row[0]:
        sys.exit(f"--live needs a funded wallet for user '{USER}'. Run: mandate fund --user {USER}")
    return pp, row[0]


# ── tokens an attacker might hold ─────────────────────────────────────

def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def token_for(kind: str, client: TestClient, conn) -> str:
    if kind == "expired":
        past = datetime.now(UTC) - timedelta(hours=2)
        args = {k: v for k, v in MANDATE.items()} | {"merchants": MANDATE["merchants"],
                                                     "categories": MANDATE["categories"]}
        token, _ = service.issue(conn, KEY, now=past, **args)
        return token
    r = client.post("/mandates", json=MANDATE, headers=H_ADMIN)
    token, mid = r.json()["token"], r.json()["mandate"]["mandate_id"]
    if kind == "revoked":
        client.post(f"/mandates/{mid}/revoke", headers=H_ADMIN)
    elif kind == "tampered":
        head, body, sig = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(body + "=="))
        payload["per_txn_cap_cents"] = 100_000_000
        token = f"{head}.{b64(payload)}.{sig}"
    elif kind == "alg_none":
        payload = jwt.decode(token, options={"verify_signature": False})
        token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}."
    elif kind == "forged":
        claims = tokens.verify(token, KEY).model_copy(update={"per_txn_cap_cents": 100_000_000})
        token = tokens.sign(claims, secrets.token_urlsafe(48))
    return token


# ── one case ──────────────────────────────────────────────────────────

def run_case(case: dict, live: bool) -> dict:
    tmp = tempfile.mkdtemp(prefix="redteam-")
    path = os.path.join(tmp, "m.db")
    if case.get("paypal") == "down":
        rail, vault = unreachable_paypal(), "redteam-vault"
    elif live:
        rail, vault = live_paypal()
    else:
        rail, vault = Recorder(), "offline-vault"
    pp = Counted(rail)

    conn = db.connect(path)
    conn.execute("INSERT INTO funding (user_id, setup_token_id, payment_token_id, started_at, funded_at)"
                 " VALUES (?, 'redteam', ?, 'now', 'now')", (USER, vault))
    client = TestClient(create_app(Settings(KEY, ADMIN, path, pp)))  # type: ignore[arg-type]

    token = token_for(case.get("token", "valid"), client, conn)
    agent = {"Authorization": f"Bearer {token}"}

    def intent(over: dict | None = None) -> dict:
        base = INTENT
        if "intent_file" in case:
            base = json.loads((ROOT / case["intent_file"]).read_text())["intent"]
        return base | (case.get("intent") or {}) | (over or {}) | {"intent_id": f"int_{ULID()}"}

    for i, over in enumerate(case.get("setup") or []):
        v = client.post("/authorize", json=INTENT | over | {"intent_id": f"int_{ULID()}"}, headers=agent).json()
        if v["verdict"] != "ALLOW":
            return {"error": f"setup step {i + 1} was {v['reason_code']}, expected ALLOW"}

    before = pp.calls
    out: dict = {}
    if case.get("call") == "self_issue":
        r = client.post("/mandates", json=MANDATE | {"per_txn_cap_cents": 100_000_000}, headers=agent)
        out = {"http_status": r.status_code}
    elif "burst" in case:
        verdicts: list[dict] = []
        lock = threading.Lock()

        def fire():
            v = client.post("/authorize", json=intent(), headers=agent).json()
            with lock:
                verdicts.append(v)
        threads = [threading.Thread(target=fire) for _ in range(case["burst"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        denied: dict[str, int] = {}
        for v in verdicts:
            if v["verdict"] == "DENY":
                denied[v["reason_code"]] = denied.get(v["reason_code"], 0) + 1
        out = {"allowed": sum(v["verdict"] == "ALLOW" for v in verdicts), "denied": denied}
    else:
        if "body" in case:
            r = client.post("/authorize", content=case["body"], headers=agent)
        else:
            body = intent()
            for _ in range(case.get("repeat", 1)):
                r = client.post("/authorize", json=body, headers=agent)
        v = r.json()
        out = {"verdict": v["verdict"], "reason_code": v["reason_code"], "replayed": v["replayed"],
               "order_id": v["order_id"]}

    out["paypal_calls"] = pp.calls - before
    if "spend_txn_count" in case["expect"]:
        mid = conn.execute("SELECT mandate_id FROM mandates").fetchone()[0]
        out["spend_txn_count"] = service.status(conn, mid, datetime.now(UTC))["spend"]["txn_count"]
    conn.close()
    return out


def check(expect: dict, got: dict) -> bool:
    return "error" not in got and all(got.get(k) == v for k, v in expect.items())


def summary(d: dict) -> str:
    if "error" in d:
        return d["error"]
    if "http_status" in d:
        return f"HTTP {d['http_status']}"
    if "allowed" in d:
        return f"{d['allowed']} allowed, " + ", ".join(f"{n} {c}" for c, n in d["denied"].items())
    if "verdict" in d:
        s = d["verdict"] if d["verdict"] == "ALLOW" else f"DENY {d['reason_code']}"
        return s + (" (replayed)" if d.get("replayed") else "")
    return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="use real PayPal sandbox for allowed purchases")
    p.add_argument("--markdown", action="store_true", help="write redteam/RESULTS.md")
    args = p.parse_args(argv)
    logging.getLogger("mandate").setLevel(logging.ERROR)  # upstream_down logs a warning by design

    cases = [yaml.safe_load(f.read_text()) for f in sorted((ROOT / "redteam" / "cases").glob("*.yaml"))]
    rail = "PayPal sandbox (live)" if args.live else "offline recorder (no money moves; use --live for PayPal sandbox)"
    print(f"MANDATE red team: {len(cases)} attacks. Payment rail: {rail}.\n")
    print(f"{'case':<20}{'expected':<38}{'got':<38}{'PayPal attempts':>16}  result")

    rows, passed = [], 0
    t0 = time.time()
    for c in cases:
        got = run_case(c, args.live)
        ok = check(c["expect"], got)
        passed += ok
        exp = summary({k: v for k, v in c["expect"].items()})
        charges = "-" if "paypal_calls" not in got else str(got["paypal_calls"])
        print(f"{c['name']:<20}{exp:<38}{summary(got):<38}{charges:>16}  {'PASS' if ok else 'FAIL'}")
        rows.append((c, exp, summary(got), charges, ok))

    print(f"\n{passed}/{len(cases)} PASS in {time.time() - t0:.1f}s.")
    if args.markdown:
        lines = [f"# Red team results\n", f"{passed}/{len(cases)} attacks stopped. Payment rail: {rail}. "
                 f"Generated by `redteam/run.py` on {datetime.now(UTC):%Y-%m-%d}.\n",
                 "| Attack | What the attacker tries | Expected | Got | PayPal attempts | Result |",
                 "|---|---|---|---|---|---|"]
        for c, exp, got, charges, ok in rows:
            lines.append(f"| `{c['name']}` | {c['attack']} | {exp} | {got} | {charges} | {'PASS' if ok else 'FAIL'} |")
        (ROOT / "redteam" / "RESULTS.md").write_text("\n".join(lines) + "\n")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
