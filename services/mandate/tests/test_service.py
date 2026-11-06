"""End to end through the service: token → state → policy → reserve → broker → ledger."""

import threading
from datetime import timedelta

import jwt
import pytest

from mandate import db, ledger, policy, service, tokens
from mandate.models import ReasonCode as R

from .conftest import KEY, NOW, claims, raw_intent


def issue(conn, **over):
    args = dict(
        agent="shopper", user="nancy",
        merchants=["channel3:blue-bottle"], categories=["coffee"],
        per_txn_cap_cents=2500, period="day", period_cap_cents=5000,
        max_txn_per_period=2, ttl_seconds=86_400, now=NOW,
    ) | over
    return service.issue(conn, KEY, **args)


def authorize(conn, token, raw=None, now=NOW, broker=None):
    return service.authorize(conn, KEY, token, raw if raw is not None else raw_intent(), now, broker)


def denied(code, r):
    assert r.verdict.allowed is False
    assert r.verdict.reason_code is code
    assert r.order_id is None
    assert r.ledger_id


# The broker is the seam PayPal plugs into in M2.
def ok_broker(intent, mandate):
    return "ORDER-123"


def down_broker(intent, mandate):
    raise TimeoutError("paypal timed out")


# ── allow ─────────────────────────────────────────────────────────────

def test_allow_without_broker(conn):
    token, _ = issue(conn)
    r = authorize(conn, token)
    assert r.verdict.allowed and r.order_id is None
    assert ledger.get(conn, r.ledger_id)["verdict"] == "ALLOW"


def test_allow_records_order_id(conn):
    token, _ = issue(conn)
    r = authorize(conn, token, broker=ok_broker)
    assert r.order_id == "ORDER-123"
    assert ledger.get(conn, r.ledger_id)["order_id"] == "ORDER-123"


# ── every reason code, through the full path ──────────────────────────

def test_invalid_signature(conn):
    issue(conn)
    r = authorize(conn, "not.a.token")
    denied(R.INVALID_SIGNATURE, r)
    assert ledger.get(conn, r.ledger_id)["mandate_id"] is None  # unverified claims are not trusted, even for logging


def test_forged_mandate(conn):
    forged = tokens.sign(claims(per_txn_cap_cents=10**8), "attacker-key-that-is-also-long-enough-xx")
    denied(R.INVALID_SIGNATURE, authorize(conn, forged))


def test_malformed_intent(conn):
    token, m = issue(conn)
    r = authorize(conn, token, raw=raw_intent(amount_cents="450"))
    denied(R.MALFORMED_INTENT, r)
    assert ledger.get(conn, r.ledger_id)["mandate_id"] == m.mandate_id


def test_revoked(conn):
    token, m = issue(conn)
    assert service.revoke(conn, m.mandate_id, NOW)
    denied(R.REVOKED, authorize(conn, token))


def test_revoke_is_idempotent_and_unknown_is_false(conn):
    _, m = issue(conn)
    assert service.revoke(conn, m.mandate_id, NOW)
    assert service.revoke(conn, m.mandate_id, NOW)
    assert not service.revoke(conn, "mnd_nope", NOW)


def test_signed_but_unknown_mandate_is_revoked(conn):
    # Valid signature, but the database has no live record (e.g. it was wiped).
    # Authority needs both the token and the record.
    denied(R.REVOKED, authorize(conn, tokens.sign(claims(), KEY)))


def test_revocation_id_mismatch_is_revoked(conn):
    _, m = issue(conn)
    twin = tokens.sign(claims(mandate_id=m.mandate_id), KEY)  # same id, different revocation_id
    denied(R.REVOKED, authorize(conn, twin))


def test_expired(conn):
    token, _ = issue(conn, ttl_seconds=60)
    denied(R.EXPIRED, authorize(conn, token, now=NOW + timedelta(seconds=60)))


def test_merchant_not_allowed(conn):
    token, _ = issue(conn)
    denied(R.MERCHANT_NOT_ALLOWED, authorize(conn, token, raw_intent(merchant="channel3:evil-shop")))


def test_category_not_allowed(conn):
    token, _ = issue(conn)
    denied(R.CATEGORY_NOT_ALLOWED, authorize(conn, token, raw_intent(category="gift-cards")))


def test_per_txn_cap(conn):
    token, _ = issue(conn)
    denied(R.EXCEEDS_PER_TXN_CAP, authorize(conn, token, raw_intent(amount_cents=2501)))


def test_period_cap_accumulates(conn):
    token, _ = issue(conn, max_txn_per_period=5)
    assert authorize(conn, token, raw_intent(amount_cents=2500)).verdict.allowed
    assert authorize(conn, token, raw_intent(amount_cents=2000)).verdict.allowed
    denied(R.EXCEEDS_PERIOD_CAP, authorize(conn, token, raw_intent(amount_cents=501)))


def test_velocity(conn):
    token, _ = issue(conn)
    assert authorize(conn, token).verdict.allowed
    assert authorize(conn, token).verdict.allowed
    denied(R.VELOCITY_EXCEEDED, authorize(conn, token))


def test_period_resets_next_day(conn):
    token, _ = issue(conn, ttl_seconds=3 * 86_400)
    authorize(conn, token)
    authorize(conn, token)
    denied(R.VELOCITY_EXCEEDED, authorize(conn, token))
    assert authorize(conn, token, now=NOW + timedelta(days=1)).verdict.allowed


def test_upstream_down_fails_closed_and_releases_spend(conn):
    token, m = issue(conn)
    denied(R.UPSTREAM_UNAVAILABLE, authorize(conn, token, broker=down_broker))
    denied(R.UPSTREAM_UNAVAILABLE, authorize(conn, token, broker=down_broker))
    # Two failed attempts did not use up the two-per-day budget.
    assert service.status(conn, m.mandate_id, NOW)["spend"] == {"spent_cents": 0, "txn_count": 0}
    assert authorize(conn, token, broker=ok_broker).verdict.allowed


@pytest.mark.parametrize("bad", ["", None, 123, "x" * 300])
def test_broker_returning_garbage_is_upstream_unavailable(conn, bad):
    token, _ = issue(conn)
    denied(R.UPSTREAM_UNAVAILABLE, authorize(conn, token, broker=lambda i, m: bad))


def test_internal_error_fails_closed(conn, monkeypatch):
    token, _ = issue(conn)

    def boom(*a):
        raise RuntimeError("bug")

    monkeypatch.setattr(policy, "evaluate", boom)
    r = authorize(conn, token)
    denied(R.INTERNAL_ERROR, r)


def test_naive_clock_fails_closed(conn):
    token, _ = issue(conn)
    denied(R.INTERNAL_ERROR, authorize(conn, token, now=NOW.replace(tzinfo=None)))


def test_short_key_fails_closed(conn):
    r = service.authorize(conn, "short", "a.b.c", raw_intent(), NOW)
    denied(R.INTERNAL_ERROR, r)


def test_internal_error_even_when_ledger_is_broken(conn, monkeypatch):
    token, _ = issue(conn)
    monkeypatch.setattr(policy, "evaluate", lambda *a: 1 / 0)
    monkeypatch.setattr(ledger, "append", lambda *a, **k: 1 / 0)
    r = authorize(conn, token)
    assert r.verdict.allowed is False and r.verdict.reason_code is R.INTERNAL_ERROR
    assert r.ledger_id is None


# ── replay ────────────────────────────────────────────────────────────

def test_replayed_intent_returns_original_and_does_not_charge_twice(conn):
    token, m = issue(conn)
    calls = []

    def broker(i, mm):
        calls.append(i.intent_id)
        return f"ORDER-{len(calls)}"

    raw = raw_intent()
    first = authorize(conn, token, raw, broker=broker)
    again = authorize(conn, token, raw, broker=broker)
    assert again.replayed and again.ledger_id == first.ledger_id and again.order_id == "ORDER-1"
    assert len(calls) == 1
    assert service.status(conn, m.mandate_id, NOW)["spend"]["txn_count"] == 1


def test_denied_intent_can_be_retried(conn):
    token, _ = issue(conn)
    raw = raw_intent()
    denied(R.UPSTREAM_UNAVAILABLE, authorize(conn, token, raw, broker=down_broker))
    assert authorize(conn, token, raw, broker=ok_broker).verdict.allowed


def test_intent_still_in_flight(conn):
    token, m = issue(conn)
    raw = raw_intent()
    conn.execute("INSERT INTO intents(mandate_id, intent_id) VALUES (?, ?)", (m.mandate_id, raw["intent_id"]))
    denied(R.UPSTREAM_UNAVAILABLE, authorize(conn, token, raw))


# ── the ledger sees everything and leaks nothing ──────────────────────

def test_every_attempt_is_logged(conn):
    token, _ = issue(conn)
    authorize(conn, token)
    authorize(conn, token, raw_intent(merchant="channel3:evil-shop"))
    authorize(conn, "garbage")
    authorize(conn, token, raw=[])
    assert len(ledger.page(conn)) == 4


def test_token_never_stored_in_ledger(conn):
    token, _ = issue(conn)
    authorize(conn, token)
    authorize(conn, token, raw_intent(merchant="channel3:evil-shop"))
    dump = "\n".join(conn.iterdump())
    assert token not in dump
    assert token.split(".")[2] not in dump  # not even the signature


# ── issue + status ────────────────────────────────────────────────────

def test_issued_token_is_a_valid_mandate(conn):
    token, m = issue(conn)
    assert tokens.verify(token, KEY) == m
    assert m.revocable is True and m.exp - m.iat == 86_400


@pytest.mark.parametrize("ttl", [0, -1, 91 * 86_400])
def test_issue_rejects_bad_ttl(conn, ttl):
    with pytest.raises(ValueError):
        issue(conn, ttl_seconds=ttl)


def test_status(conn):
    token, m = issue(conn)
    authorize(conn, token)
    s = service.status(conn, m.mandate_id, NOW)
    assert s["mandate"]["mandate_id"] == m.mandate_id
    assert s["revoked_at"] is None
    assert s["spend"] == {"spent_cents": 450, "txn_count": 1}
    assert s["period_start"] == "2026-11-06T00:00:00+00:00"
    assert service.status(conn, "mnd_nope", NOW) is None


# ── concurrency: caps hold under a burst ──────────────────────────────

def test_parallel_burst_cannot_exceed_velocity(tmp_path):
    path = str(tmp_path / "m.db")
    setup = db.connect(path)
    token, m = issue(setup)
    results = []

    def worker():
        c = db.connect(path)
        results.append(authorize(c, token).verdict.allowed)
        c.close()

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 2
    assert service.status(setup, m.mandate_id, NOW)["spend"]["txn_count"] == 2
    assert len(ledger.page(setup)) == 12


def test_hs256_token_signed_by_us_with_extra_claims_is_rejected(conn):
    _, m = issue(conn)
    payload = m.model_dump(mode="json") | {"admin": True}
    denied(R.INVALID_SIGNATURE, authorize(conn, jwt.encode(payload, KEY, algorithm="HS256")))
