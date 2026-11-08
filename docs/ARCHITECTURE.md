# Architecture

Three processes, one database, one payment rail. The agent can reach exactly one endpoint.

```
                 ┌──────────────────────────── untrusted ───────────────────────────┐
  Channel3 ────▶ │ agent (services/agent)                                           │
  product pages  │   LLM via any OpenAI-compatible API (Groq: openai/gpt-oss-120b)  │
                 │   tools: search_products, get_product_page, submit_purchase      │
                 │   holds: mandate token, LLM key, catalog key                     │
                 └───────────────────────────────┬──────────────────────────────────┘
                                                 │ POST /authorize  (Bearer <mandate token>)
                                                 ▼
  human ──(admin token)──▶ ┌────────────────────────────────────────────┐
                           │ mandate service (services/mandate, FastAPI) │
  console ──(read+revoke)─▶│   tokens.verify  → INVALID_SIGNATURE         │
  (web, AG Grid)           │   policy.parse   → MALFORMED_INTENT          │
                           │   policy.evaluate (pure)  → 8 more codes     │
                           │   spend.reserve  (BEGIN IMMEDIATE)           │
                           │   broker → PayPal ─────────────────────────────▶ PayPal sandbox
                           │   ledger.append  (every attempt)             │      Orders v2 + vault
                           └───────────────────┬────────────────────────┘
                                               ▼
                                 SQLite: mandates · spend · intents · funding · ledger
```

## The request path for one purchase

1. **Verify the token.** HS256 only, every claim required, no unknown claims. Failure → `INVALID_SIGNATURE`. Nothing from an unverified token is trusted, not even its id for the log.
2. **Parse the intent.** Strict schema: integer cents, ASCII merchant and category ids, USD only, no extra fields. Failure → `MALFORMED_INTENT`.
3. **Take the write lock** (`BEGIN IMMEDIATE`). From here to step 6, no other request can read or change this mandate's counters.
4. **Replay check.** If this `intent_id` was already charged, return the original result. No second charge.
5. **Load state and evaluate.** Revocation (the token's `revocation_id` must match a live row) and this period's spend go into `policy.evaluate`, a pure function. It checks, in order: revoked, expired, merchant, category, per-purchase cap, purchases per period, period cap. The only ALLOW is its last line.
6. **Reserve.** On ALLOW, add the amount and one purchase to the counters and mark the intent in flight. Commit and release the lock.
7. **Charge** (outside the lock, so a slow network never blocks other requests). Create an Orders v2 order whose `payment_source` is the vaulted wallet, with `PayPal-Request-Id = <mandate>-<intent>`. Accept only an order and capture that are both `COMPLETED` for exactly the approved amount.
8. **Settle.** On success, write the ALLOW ledger row with the order id. On any failure, release the reservation and write `UPSTREAM_UNAVAILABLE`. Any exception anywhere → `INTERNAL_ERROR`, still a DENY, still logged.

## Why this shape

| Choice | Why | Passed on |
|---|---|---|
| Pure-function policy | Can't be prompt-injected, can be tested exhaustively, gives the same answer every time | An LLM "judge" or a risk score. Both are probabilistic, which is exactly what we're protecting against. |
| Vault, approved once at funding | Orders v2 normally needs the buyer to approve each order in a browser. An agent can't. The human's one approval is the moment they delegate. | A test card as the payment source. It works, but it isn't how a wallet would really be delegated. |
| Token for authority, database for state | The token can't be edited without the key. The database can revoke and count. Either alone is not enough. | Stateless tokens (can't revoke) or database-only sessions (the agent could be handed any id) |
| SQLite, plain `sqlite3` | One file, `BEGIN IMMEDIATE` gives the atomicity the caps need, triggers make the ledger append-only | Postgres plus an ORM: more to run, nothing gained at this scale |
| Reserve before charging, release on failure | Parallel requests see each other's spend. Outages don't eat the budget. | Charge then count: two racing requests could both pass the cap. |
| Polling console (500 ms) | Meets the under-one-second bar with no extra moving parts | Websockets or SSE |
| The console proxy holds the admin token | The browser never sees a secret. Revoke is the only write it allows, and revoking can only remove authority. | Putting the token in the frontend build |

## Code map

| Path | What |
|---|---|
| `services/mandate/models.py` | Wire types and the 11 reason codes |
| `services/mandate/policy.py` | `parse_intent`, `evaluate`, `period_start`. Pure. |
| `services/mandate/tokens.py` | Sign and verify mandate tokens |
| `services/mandate/service.py` | `issue`, `revoke`, `status`, `authorize` (the request path above) |
| `services/mandate/paypal.py` | Sandbox-only client: OAuth, vault, orders |
| `services/mandate/funding.py` | Link a wallet once; the broker that charges it |
| `services/mandate/main.py` | FastAPI routes |
| `services/mandate/db.py`, `ledger.py`, `spend.py` | Storage |
| `services/agent/` | The LLM agent, the Channel3 catalog, the injection |
| `web/` | The console (React, AG Grid Community) and its nginx proxy |
| `redteam/` | 14 attacks as YAML, and the runner |
| `scripts/demo.sh` | The four demo beats, zero LLM calls |

## Running it

- `docker compose up -d --build`: mandate service on `127.0.0.1:8000`, console on `127.0.0.1:5173`.
- Only the `mandate` container gets PayPal credentials. `web` gets the admin token for its proxy. The `agent` container gets an LLM key, a catalog key and a mandate token, nothing else.
- Data lives in `./data` (bind-mounted). There is no database container.
