# Mandate spec

A mandate is a signed, limited grant of spending authority from a human to one agent. This page defines the token, the intent, the verdict, the reason codes and the API.

## Mandate token

An HS256 JWT. The payload is exactly these claims: all required, nothing else allowed.

| Claim | Type | Example | Meaning |
|---|---|---|---|
| `mandate_id` | `mnd_<ULID>` | `mnd_01M6W967WJPEMRTAAB2XHTYGMG` | Identity of this mandate |
| `subject` | `agent:<name>` | `agent:shopper` | The agent it's for |
| `issuer` | `user:<id>` | `user:nancy` | The human who signed; their linked wallet pays |
| `iat`, `exp` | Unix seconds | | Issued and expires. At most 90 days apart. |
| `merchant_allowlist` | list, ≥ 1 | `["channel3:target-com"]` | Exact match. Lowercase ASCII `channel3:<slug>`. |
| `category_allowlist` | list, ≥ 1 | `["coffee-filters"]` | Exact match. Lowercase ASCII slug. |
| `per_txn_cap_cents` | int > 0 | `1500` | Most any one purchase may cost |
| `period` | `day` · `week` · `month` | `day` | Calendar windows in UTC: midnight, ISO Monday, the 1st |
| `period_cap_cents` | int > 0 | `4000` | Most the period's allowed purchases may total |
| `max_txn_per_period` | int > 0 | `3` | Most allowed purchases per period |
| `revocable` | `true` | `true` | Always true. A mandate the human can't kill is not honored. |
| `revocation_id` | `rev_<ULID>` | | Must match the live database record |

The token carries authority. The database carries revocation state and spend counters. A purchase needs both.

## Purchase intent

What the agent sends to `POST /authorize`, as the JSON body, with `Authorization: Bearer <mandate token>`.

| Field | Type | Rule |
|---|---|---|
| `intent_id` | string | 8 to 64 of `[A-Za-z0-9_-]`. Idempotency key: the same id is charged at most once. |
| `merchant` | string | `channel3:<slug>`, lowercase ASCII |
| `category` | string | Lowercase ASCII slug |
| `amount_cents` | integer | 1 to 100,000,000. Strings, floats and booleans are rejected. |
| `currency` | `"USD"` | Only USD |
| `item` | string | 1 to 120 characters, for the ledger and the PayPal order description |

Unknown fields are rejected. On purpose, there is no field for the user's conversation, prompts or identity (data minimization, I5).

## Verdict

```json
{
  "verdict": "DENY",
  "reason_code": "MERCHANT_NOT_ALLOWED",
  "message": "channel3:partner-giftcards-com is not on this mandate's merchant list.",
  "order_id": null,
  "ledger_id": "led_01M6...",
  "replayed": false
}
```

HTTP 200 for both ALLOW and DENY: a denial is the system working, not an error. `reason_code` is for machines and `message` is for humans. On ALLOW, `order_id` is the PayPal order.

## Reason codes

Checked in this order. The first that applies wins.

| # | Code | When |
|---|---|---|
| 1 | `INVALID_SIGNATURE` | Token missing, malformed, wrong key, wrong algorithm, or claims that aren't a valid mandate |
| 2 | `MALFORMED_INTENT` | Body isn't JSON, or the intent breaks a rule above |
| 3 | `REVOKED` | Revoked, or no live record for this `mandate_id` and `revocation_id` |
| 4 | `EXPIRED` | Now ≥ `exp` |
| 5 | `MERCHANT_NOT_ALLOWED` | Merchant not in the allowlist |
| 6 | `CATEGORY_NOT_ALLOWED` | Category not in the allowlist |
| 7 | `EXCEEDS_PER_TXN_CAP` | Amount > `per_txn_cap_cents` |
| 8 | `VELOCITY_EXCEEDED` | Allowed purchases this period ≥ `max_txn_per_period` |
| 9 | `EXCEEDS_PERIOD_CAP` | This period's spend + amount > `period_cap_cents` |
| 10 | `UPSTREAM_UNAVAILABLE` | Policy allowed it, but PayPal failed, timed out, returned something unexpected, or no wallet is linked. Nothing was charged; the reservation is released. |
| 11 | `INTERNAL_ERROR` | A bug on our side. Still a DENY, still logged. |

Why this order: unverified input learns nothing (1, 2). A human's kill switch outranks everything (3). Scope comes before money (5, 6 before 7 to 9), so the code names the attack: an injected merchant is reported as `MERCHANT_NOT_ALLOWED` even when its price is also too high.

Codes 1 to 10 are from the original spec. `INTERNAL_ERROR` was added so that `UPSTREAM_UNAVAILABLE` means only "PayPal failed."

## API

| Method and path | Who | What |
|---|---|---|
| `POST /mandates` | Human (admin token) | Issue and sign. Body: `agent, user, merchants, categories, per_txn_cap_cents, period, period_cap_cents, max_txn_per_period, ttl_seconds`. Returns `{token, mandate}`. |
| `GET /mandates` | Anyone on localhost | Recent mandates with status and spend. Never returns tokens. |
| `GET /mandates/{id}` | Anyone on localhost | One mandate with this period's spend |
| `POST /mandates/{id}/revoke` | Human (admin token) | Kill switch. Takes effect on the next attempt. |
| `POST /authorize` | Agent (mandate token) | Submit an intent, get a verdict. On ALLOW, the service places the PayPal order. |
| `GET /ledger?limit=&before=` | Anyone on localhost | Every attempt, newest first. Page with the last row's `seq`. |
| `GET /healthz` | Anyone | `{ok, paypal: configured or missing}` |
