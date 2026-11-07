import ast
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from agent import loop
from agent.__main__ import INJECTION
from agent.catalog import (Channel3Catalog, Product, StaticCatalog, TamperedCatalog, from_channel3,
                           merchant_slug)

AGENT_DIR = Path(__file__).resolve().parents[1]

COFFEE = Product("p1", "Whole bean coffee, 12 oz", "channel3:example-roaster-com", "coffee", 1800,
                 "Medium roast. Notes of chocolate.")
MUG = Product("p2", "Ceramic mug", "channel3:example-roaster-com", "mugs", 2400, "Holds coffee.")


class ListCatalog:
    def __init__(self, *products):
        self.products = list(products)

    def search(self, query, max_price_cents=None, limit=8):
        return [p for p in self.products if max_price_cents is None or p.price_cents <= max_price_cents]

    def get(self, pid):
        return next((p for p in self.products if p.product_id == pid), None)


# ── I1: the agent cannot reach PayPal or the mandate internals ───────

def test_agent_never_imports_the_mandate_service():
    for f in AGENT_DIR.rglob("*.py"):
        if "tests" in f.parts:
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            assert not any(n == "mandate" or n.startswith("mandate.") for n in names), f
            assert not any("paypal" in n.lower() for n in names), f


def test_agent_never_reads_paypal_or_admin_settings():
    for f in AGENT_DIR.rglob("*.py"):
        if "tests" in f.parts:
            continue
        consts = [n.value for n in ast.walk(ast.parse(f.read_text()))
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for c in consts:
            assert c not in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "MANDATE_ADMIN_TOKEN",
                             "MANDATE_SIGNING_KEY"), f


# ── a scripted LLM, so the loop is tested without network ────────────

def call(name, args, cid="c1"):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=cid, function=fn,
                           model_dump=lambda: {"id": cid, "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}})


class ScriptedLLM:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.seen.append(json.loads(json.dumps(kw["messages"])))
        calls, content = self.turns.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls, content=content))])


def test_loop_produces_one_intent():
    llm = ScriptedLLM(
        ([call("search_products", {"query": "coffee", "max_price_usd": 20})], None),
        ([call("get_product_page", {"product_id": "p1"})], None),
        ([call("submit_purchase", {"merchant": COFFEE.merchant, "category": "coffee",
                                   "amount_cents": 1800, "item": COFFEE.title})], None),
    )
    r = loop.run("buy coffee", ListCatalog(COFFEE, MUG), llm, "m")
    assert r.intent["merchant"] == COFFEE.merchant and r.intent["amount_cents"] == 1800
    assert r.intent["currency"] == "USD" and r.intent["intent_id"].startswith("int_")
    assert len(r.steps) == 3


def test_search_respects_price_filter():
    llm = ScriptedLLM(([call("search_products", {"query": "x", "max_price_usd": 20})], None), ([], "done"))
    loop.run("t", ListCatalog(COFFEE, MUG), llm, "m")
    tool_result = json.loads(llm.seen[1][-1]["content"])
    assert [p["product_id"] for p in tool_result] == ["p1"]


def test_injection_reaches_the_model_verbatim():
    catalog = TamperedCatalog(ListCatalog(COFFEE), INJECTION)
    llm = ScriptedLLM(([call("get_product_page", {"product_id": "p1"})], None), ([], "done"))
    loop.run("buy coffee", catalog, llm, "m")
    page = json.loads(llm.seen[1][-1]["content"])["page"]
    assert page.endswith(INJECTION) and page.startswith(COFFEE.description)


def test_model_output_is_not_sanitized():
    # A compromised model's garbage must reach the fence unchanged, to be denied there.
    llm = ScriptedLLM(([call("submit_purchase", {"merchant": "EVIL", "category": "x",
                                                 "amount_cents": "lots", "item": "y"})], None))
    r = loop.run("t", ListCatalog(), llm, "m")
    assert r.intent["merchant"] == "EVIL" and r.intent["amount_cents"] == "lots"


def test_loop_stops_without_purchase():
    r = loop.run("t", ListCatalog(), ScriptedLLM(([], "I can't find that.")), "m")
    assert r.intent is None and "model stopped" in r.steps[-1]


def test_step_limit():
    turns = [([call("search_products", {"query": "x"})], None)] * 3
    r = loop.run("t", ListCatalog(), ScriptedLLM(*turns), "m", max_steps=3)
    assert r.intent is None and r.steps[-1] == "step limit reached"


def test_unknown_product_and_tool():
    llm = ScriptedLLM(([call("get_product_page", {"product_id": "nope"}), call("rm_rf", {}, "c2")], None),
                      ([], "ok"))
    loop.run("t", ListCatalog(), llm, "m")
    assert "not found" in llm.seen[1][-2]["content"] and "unknown tool" in llm.seen[1][-1]["content"]


# ── catalog sources ──────────────────────────────────────────────────

def test_merchant_slug():
    assert merchant_slug("www.BlueBottleCoffee.com") == "channel3:bluebottlecoffee-com"


C3_ITEM = {
    "id": "c3_1", "title": "Bella Donovan", "description": "Our house blend.",
    "category": {"slug": "coffee"},
    "offers": [
        {"domain": "eu.example.com", "price": {"price": 19.0, "currency": "EUR"}, "availability": "InStock"},
        {"domain": "bluebottlecoffee.com", "url": "https://bluebottlecoffee.com/x",
         "price": {"price": 22.5, "currency": "USD"}, "availability": "InStock"},
    ],
}


def test_from_channel3_picks_first_usd_offer():
    p = from_channel3(C3_ITEM)
    assert p == Product("c3_1", "Bella Donovan", "channel3:bluebottlecoffee-com", "coffee", 2250,
                        "Our house blend.", "https://bluebottlecoffee.com/x")


def test_from_channel3_without_usable_offer():
    assert from_channel3({"id": "x", "offers": []}) is None
    assert from_channel3({"id": "x"}) is None


def c3(handler):
    return Channel3Catalog("k", fallback=StaticCatalog(), transport=httpx.MockTransport(handler))


def test_channel3_search_and_get():
    def handler(req):
        assert req.headers["x-api-key"] == "k"
        body = json.loads(req.content)
        assert body["filters"]["price"]["max_price"] == 25.0
        return httpx.Response(200, json={"products": [C3_ITEM]})
    cat = c3(handler)
    assert [p.product_id for p in cat.search("coffee", 2500)] == ["c3_1"]
    assert cat.get("c3_1").price_cents == 2250 and not cat.used_fallback


def test_channel3_out_of_credits_falls_back_to_snapshot():
    cat = c3(lambda req: httpx.Response(402, json={"detail": "out of credits"}))
    results = cat.search("coffee")
    assert cat.used_fallback and results and all(isinstance(p, Product) for p in results)


def test_channel3_get_falls_back():
    cat = c3(lambda req: httpx.Response(500))
    first = StaticCatalog().products[0]
    assert cat.get(first.product_id) == first


def test_static_catalog_is_well_formed():
    cat = StaticCatalog()
    assert len(cat.products) >= 6
    for p in cat.products:
        assert p.merchant.startswith("channel3:") and p.price_cents > 0 and p.description
    assert cat.search("coffee")
    assert cat.search("coffee", max_price_cents=1) == []


# ── the CLI entry point, with the LLM and HTTP stubbed ───────────────

import pytest  # noqa: E402

import agent.__main__ as entry  # noqa: E402

BUY = ([call("submit_purchase", {"merchant": "channel3:target-com", "category": "coffee-filters",
                                 "amount_cents": 999, "item": "AeroPress Paper Coffee Filters"})], None)


@pytest.fixture
def env(monkeypatch):
    for k, v in {"LLM_API_KEY": "k", "LLM_BASE_URL": "http://llm", "LLM_MODEL": "m",
                 "MANDATE_TOKEN": "tok", "MANDATE_URL": "http://mandate"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)
    return monkeypatch


def test_main_dry_run(env, capsys):
    env.setattr(entry, "OpenAI", lambda **kw: ScriptedLLM(BUY))
    assert entry.main(["buy filters", "--dry-run", "--tamper"]) == 0
    assert json.loads(capsys.readouterr().out)["intent"]["merchant"] == "channel3:target-com"


@pytest.mark.parametrize("verdict,code", [("ALLOW", 0), ("DENY", 2)])
def test_main_submits_with_mandate_token_only(env, capsys, verdict, code):
    sent = {}

    def post(url, json, headers, timeout):
        sent.update(url=url, headers=headers)
        return SimpleNamespace(json=lambda: {"verdict": verdict})

    env.setattr(entry, "OpenAI", lambda **kw: ScriptedLLM(BUY))
    env.setattr(entry.httpx, "post", post)
    assert entry.main(["buy filters"]) == code
    assert sent["url"] == "http://mandate/authorize" and sent["headers"] == {"Authorization": "Bearer tok"}


def test_main_no_intent(env):
    env.setattr(entry, "OpenAI", lambda **kw: ScriptedLLM(([], "nothing suitable")))
    assert entry.main(["buy a yacht"]) == 1


def test_main_requires_settings(env):
    env.delenv("MANDATE_TOKEN")
    env.setattr(entry, "OpenAI", lambda **kw: ScriptedLLM(BUY))
    with pytest.raises(SystemExit):
        entry.main(["buy filters"])
    env.delenv("LLM_API_KEY")
    with pytest.raises(SystemExit):
        entry.main(["buy filters"])
