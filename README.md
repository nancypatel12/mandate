# MANDATE

**An AI agent can only spend what a human signed for: MANDATE checks every purchase against a signed, capped, revocable mandate before PayPal sees it.**

> Work in progress. Milestone 1 (the policy engine, tokens, ledger and CLI) is done. PayPal, the agent and the UI come next.

## Run the tests

```bash
pip install uv
uv sync
uv run pytest
```

## Try the CLI

```bash
cp .env.example .env    # then set MANDATE_SIGNING_KEY
set -a; source .env; set +a
mkdir -p data

uv run mandate issue --agent shopper --user nancy \
  --merchant channel3:blue-bottle --category coffee \
  --per-txn 2500 --period day --period-cap 5000 --max-txn 2 --ttl 86400
# copy the "token" from the output

uv run mandate authorize --token "$TOKEN" --merchant channel3:blue-bottle \
  --category coffee --amount 450 --item "Bella Donovan, 12 oz"     # ALLOW

uv run mandate authorize --token "$TOKEN" --merchant channel3:evil-shop \
  --category coffee --amount 450 --item "Gift card"                # DENY MERCHANT_NOT_ALLOWED

uv run mandate revoke <mandate_id>
uv run mandate ledger
```

`authorize` exits 0 on ALLOW and 2 on DENY.

## License

Apache-2.0.
