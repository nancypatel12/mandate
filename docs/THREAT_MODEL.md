# Threat model

MANDATE assumes the AI agent is compromised and asks one question: how much damage can it do with money? The answer should be "no more than the human signed for, and nothing after they revoke."

This document says what we protect, from whom, where the trust boundaries are, and what MANDATE does **not** defend against.

## Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| The human's money | PayPal (sandbox here) | The thing we protect |
| PayPal client secret | `.env`, mandate service only | Can create orders on the merchant account |
| Vault payment token | `funding` table, mandate service only | Charges the human's wallet without them present |
| Signing key | `.env`, mandate service only | Whoever holds it can mint any mandate |
| Admin token | `.env`; mandate service and the console proxy | Can issue and revoke mandates |
| Mandate token | Held by the agent | Spends within one mandate's limits |
| Ledger | SQLite, append-only | The record of every attempt; evidence after an incident |

## Adversaries

**1. A compromised model.** The main one. Prompt injection, a jailbreak, a bad fine-tune or a buggy loop all look the same from outside: the agent asks for purchases the human didn't intend. We assume it controls every field of every intent and can call any HTTP endpoint it can reach.

**2. Malicious merchant content.** Product pages are written by merchants, and a merchant (or someone who compromised one) can put instructions in them. This is how adversary 1 usually comes about. In our live runs, a plain paragraph on a product page made `openai/gpt-oss-120b` attempt a $480 off-scope purchase in **3 out of 3** runs.

**3. Token tampering and forgery.** The agent holds a JWT. It may edit the claims, strip the signature, switch the algorithm, or mint a token with a key it made up.

**4. Replay and retries.** The agent, or a network layer, resends a purchase. Each one must be charged at most once.

**5. Runaway loops.** No malice, just a loop that keeps buying. Same defense.

## Trust boundaries

```
 human ──(admin token)──▶ mandate service ◀──(mandate token)── agent ◀── merchant pages (untrusted)
   │                          │  ▲
   │                          │  └── SQLite: mandates, revocation, spend, ledger
   └──▶ console ──(proxy: read + revoke only)──┘
                              │
                              └──(client secret, vault token)──▶ PayPal sandbox
```

| Boundary | What crosses it | Rule |
|---|---|---|
| Agent → mandate service | A mandate token and a `PurchaseIntent` | Everything is untrusted: signature checked, intent shape checked, then policy. |
| Mandate service → PayPal | Client secret, vault token, order | Only the mandate service crosses it. The agent has no code or credentials for it, and an AST test fails the build if it ever does (I1). |
| Console → mandate service | Reads, plus revoke | The proxy strips browser auth headers, forwards GETs, and adds the admin token only to `POST /mandates/{id}/revoke`. Issue and authorize get 403. |
| Merchant pages → agent | Product text | Passed through verbatim. We don't try to sanitize it, because no sanitizer is reliable. The fence is downstream. |

## Defenses, and the attack each one stops

Every row is checked by `redteam/run.py` (14/14 pass offline and against live PayPal sandbox) or by the unit tests.

| Defense | Stops | Proof |
|---|---|---|
| Deterministic policy, no model in the decision path (I2) | Being talked out of the rules. The policy has no prompt to inject. | `policy.py` is pure; same inputs, same verdict, tested 100 times |
| Merchant and category allowlists, exact ASCII match | Injected merchants, look-alike merchants | `inject_merchant`, `homoglyph_merchant` |
| Per-purchase cap, period cap, purchases per period | Inflated prices, slow drains, runaway loops | `inject_amount`, `overspend_period`, `velocity_burst` |
| Check order: scope before money | Misleading reasons. An injected merchant reports `MERCHANT_NOT_ALLOWED` even when the amount is also wrong. | `test_injected_merchant_reports_merchant_not_cap` |
| `BEGIN IMMEDIATE` reserve-then-charge | Parallel requests racing past a cap | `velocity_burst`: 8 at once, exactly 3 charged |
| HS256 pinned, every claim required, unknown claims rejected | Tampering, `alg: none`, algorithm switching, extra claims | `tampered_signature`, `alg_none`, `forged_key` |
| Authority needs the token **and** a live database record with a matching `revocation_id` | Revoked or wiped mandates coming back | `revoked_token`, `test_signed_but_unknown_mandate_is_revoked` |
| Expiry checked by policy, after revocation | Stale mandates | `expired_token` |
| Strict intent schema (strict ints, no extra fields) | Type confusion, smuggled fields | `malformed_intent` |
| Fail closed on every PayPal error, release the reserved spend | Outages approving purchases or eating the budget | `upstream_down` |
| `intent_id` idempotency plus `PayPal-Request-Id` | Double charges on retry | `replay` |
| Admin token on issue and revoke | The agent signing itself a bigger mandate | `self_issue` |
| Append-only ledger, enforced by SQLite triggers | Covering tracks | `test_update_is_blocked_by_the_database` |
| Data minimization: intents carry claims only (I5) | Leaking conversations or PII into the payment path | Schema rejects unknown fields; the ledger stores no tokens (tested) |

## What MANDATE does not defend against

Honest limits. Some are inherent to the design; some are gaps we know about.

**Inherent to the design**

1. **Bad purchases inside the mandate.** If the mandate allows $15 of coffee filters at Target, a compromised agent can buy $15 of the *wrong* coffee filters at Target. MANDATE bounds the damage; it doesn't judge taste. The human picks how tight the mandate is.
2. **A stolen mandate token.** It's a bearer token. Whoever holds it can spend within its limits until it expires or is revoked. Mitigations are the limits, short expiry and the kill switch. Binding tokens to a key the agent holds (DPoP-style) would fix this; we didn't build it.
3. **A compromised mandate service.** If an attacker gets the signing key, the PayPal secret or the host, the fence is gone. Everything here assumes the service is trustworthy and its `.env` is protected.
4. **A malicious human issuer.** The admin can sign any mandate. MANDATE limits the agent, not the person who delegates to it.
5. **Merchant fraud after payment.** If an allowed merchant takes the money and ships nothing, that's a PayPal dispute, not a mandate problem.
6. **Exact merchant identity.** Merchant ids come from the catalog (`channel3:<domain>`). If the catalog is wrong or compromised, the allowlist trusts the wrong thing. In this demo every order also pays the same sandbox business account; a real deployment would route to each merchant's own PayPal account.

**Known gaps in this build**

7. **Capture succeeds, then the ledger write fails.** The agent is told DENY but money moved. It needs a SQLite failure at exactly that moment. Fix: write the ALLOW row in the same transaction that marks the intent complete, and reconcile against PayPal on startup.
8. **PENDING captures.** We treat a PENDING capture as a failure and release the budget. If PayPal completes it later, money moved without counting against the caps. Fix: keep the reservation and reconcile with webhooks.
9. **Crash between capture and ledger.** That intent stays "in flight" and every retry is refused. That's safe (no double charge) but stuck until an operator clears it.
10. **The console is unauthenticated on localhost.** Anyone on the machine who can reach `127.0.0.1:5173` can read the ledger and revoke. Revoke can only remove authority, which is why it's the one action exposed, but a shared or hosted deployment would need real login.
11. **Revocation is local.** It takes effect on the next `/authorize`, within milliseconds, but it can't cancel an order PayPal has already captured.
12. **No rate limit on `/authorize` itself.** Denials cost nothing and are logged, but a flood would grow the ledger. Put a proxy rate limit in front in production.
13. **Demo-grade secrets.** HS256 with a shared key and secrets in `.env` are fine for a local demo. Production would want asymmetric signing (so verifiers can't mint) and a secrets manager.
