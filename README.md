# MANDATE

**An AI agent can only spend what a human signed for: MANDATE checks every purchase against a signed, capped, revocable mandate before PayPal sees it.**

> Work in progress. Done: the policy engine (M1) and real PayPal sandbox payments (M2). Next: the agent, the UI, the red-team suite.

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

## License

Apache-2.0.
