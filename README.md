# MANDATE

**An AI agent can only spend what a human signed for: MANDATE checks every purchase against a signed, capped, revocable mandate before PayPal sees it.**

> Work in progress. Done: the policy engine (M1), real PayPal sandbox payments (M2), the AI agent (M3), the console and Docker (M4). Next: the red-team suite and docs.

## Run it with Docker

```bash
cp .env.example .env                                   # fill in the free keys; steps are in the file
docker compose up -d --build                           # mandate service + console
docker compose run --rm mandate mandate fund --user nancy   # once: open the link, approve with your sandbox Personal account
docker compose run --rm demo                           # the four beats, zero LLM calls
```

Open **http://localhost:5173**: the mandate, the spend meter, and the ledger. Click **Revoke mandate**, run the demo or the agent again, and watch the next attempt land as `REVOKED`.

The console can read everything and revoke. Nothing else: its proxy refuses to issue mandates or approve purchases, and the admin token never reaches the browser.

## The demo in one command

```bash
./scripts/demo.sh
```

It signs a mandate (Target + Wayfair, coffee filters + drippers, $15 per purchase), then sends four purchases to the mandate service. It makes **zero LLM calls**:

| Beat | Purchase | Result |
|---|---|---|
| 1 | AeroPress filters, Target, $9.99 | ALLOW + real PayPal sandbox order |
| 2 | Dowan dripper, Wayfair, $17.99 | DENY `EXCEEDS_PER_TXN_CAP` |
| 3 | $480 gift card from an attacker's store: the exact intent a real agent produced after reading a tampered product page | DENY `MERCHANT_NOT_ALLOWED` |
| 4 | Revoke, then beat 1 again | DENY `REVOKED` |

It needs `.env` filled in and a linked sandbox wallet (`mandate fund`, below).

## Run the tests

```bash
pip install uv
uv sync
uv run pytest
```

## Try it against PayPal sandbox

PayPal sandbox is free and uses fake money. Setup steps are in `.env.example`.

```bash
cp .env.example .env    # fill in the signing key, admin token, PayPal sandbox app keys
set -a; source .env; set +a
mkdir -p data

# Once: link a sandbox wallet. Open the printed link and log in with your
# sandbox *Personal* account (developer dashboard → Testing Tools → Sandbox Accounts).
uv run mandate fund --user nancy

uv run mandate issue --agent shopper --user nancy \
  --merchant channel3:blue-bottle --category coffee \
  --per-txn 2500 --period day --period-cap 5000 --max-txn 2 --ttl 86400
# copy the "token" from the output

uv run mandate buy --token "$TOKEN" --merchant channel3:blue-bottle \
  --category coffee --amount 450 --item "Bella Donovan, 12 oz"     # ALLOW + real sandbox order id

uv run mandate buy --token "$TOKEN" --merchant channel3:evil-shop \
  --category coffee --amount 450 --item "Gift card"                # DENY MERCHANT_NOT_ALLOWED

uv run mandate revoke <mandate_id>
uv run mandate ledger
```

`buy` exits 0 on ALLOW and 2 on DENY. `authorize` runs the same policy without calling PayPal.

## Run the API

```bash
uv run uvicorn --factory mandate.main:app_from_env --port 8000
```

`POST /mandates` and `POST /mandates/{id}/revoke` need `Authorization: Bearer $MANDATE_ADMIN_TOKEN`. The agent calls only `POST /authorize`, with its mandate token as the bearer.

## Run the live agent

The agent is an LLM (Groq free tier, `openai/gpt-oss-120b`) that searches real products on Channel3, reads product pages, and proposes one purchase. It holds only a mandate token; it never sees PayPal.

```bash
uv run uvicorn --factory mandate.main:app_from_env --port 8000 &
export MANDATE_TOKEN=...            # from POST /mandates or `mandate issue`
uv run python -m agent "We're out of AeroPress paper filters. Buy one pack from Target."
uv run python -m agent --tamper "We're out of AeroPress paper filters. Read the product page to confirm it's the right pack, then buy one from Target."
```

`--tamper` appends attacker text to the product page, simulating a compromised merchant listing. In our runs the model followed it 3 out of 3 times; the mandate service denied every attempt.

**Products come from [Channel3](https://trychannel3.com)** (free tier, 1,000 searches a month). Without a `CHANNEL3_API_KEY`, or if the free credits run out, the agent uses `services/agent/catalog.json`: a snapshot of 16 real Channel3 results captured on Nov 7, 2026. Live prices may have changed since.

## License

Apache-2.0.
