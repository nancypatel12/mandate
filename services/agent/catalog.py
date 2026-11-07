"""Where the agent finds products. Everything returned here is UNTRUSTED.

Product text comes from merchants, and a merchant page can contain anything,
including instructions aimed at the agent. We pass it to the model verbatim on
purpose: the injection is real, and the mandate service is what stops it.

Sources:
  Channel3Catalog  live search over Channel3's product API (free tier)
  StaticCatalog    a bundled snapshot (catalog.json) for offline runs and when
                   Channel3 credits run out. Disclosed in the README.
  TamperedCatalog  wraps either one and appends attacker text to a product page,
                   to simulate a compromised merchant listing.
"""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

SNAPSHOT = Path(__file__).with_name("catalog.json")
CHANNEL3 = "https://api.trychannel3.com"


@dataclass(frozen=True)
class Product:
    product_id: str
    title: str
    merchant: str        # channel3:<domain-slug>, the id a mandate allowlists
    category: str
    price_cents: int
    description: str     # untrusted merchant text, passed through verbatim
    url: str = ""

    def summary(self) -> dict:
        d = asdict(self)
        del d["description"]
        return d


def merchant_slug(domain: str) -> str:
    """bluebottlecoffee.com -> channel3:bluebottlecoffee-com"""
    d = domain.lower().removeprefix("www.")
    return "channel3:" + re.sub(r"[^a-z0-9]+", "-", d).strip("-")[:64]


def category_slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:32] or "uncategorized"


class StaticCatalog:
    def __init__(self, path: Path = SNAPSHOT):
        self.products = [Product(**p) for p in json.loads(path.read_text())["products"]]

    def search(self, query: str, max_price_cents: int | None = None, limit: int = 8) -> list[Product]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        scored = []
        for p in self.products:
            if max_price_cents is not None and p.price_cents > max_price_cents:
                continue
            hay = f"{p.title} {p.category} {p.description}".lower()
            score = sum(w in hay for w in words)
            if score:
                scored.append((-score, p.price_cents, p))
        return [p for *_, p in sorted(scored, key=lambda t: t[:2])][:limit]

    def get(self, product_id: str) -> Product | None:
        return next((p for p in self.products if p.product_id == product_id), None)


class Channel3Catalog:
    """Live Channel3 search. Falls back to the snapshot if Channel3 refuses (e.g. 402
    when free credits run out), so the demo never depends on a paid plan."""

    def __init__(self, api_key: str, fallback: StaticCatalog | None = None,
                 transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=CHANNEL3, timeout=15, transport=transport,
                                  headers={"x-api-key": api_key})
        self.fallback = fallback or StaticCatalog()
        self._seen: dict[str, Product] = {}
        self.used_fallback = False

    def search(self, query: str, max_price_cents: int | None = None, limit: int = 8) -> list[Product]:
        body: dict = {"query": query, "limit": min(limit, 30)}
        if max_price_cents is not None:
            body["filters"] = {"price": {"max_price": max_price_cents / 100}}
        try:
            r = self._http.post("/v1/search", json=body)
            r.raise_for_status()
            items = r.json()["products"]
        except (httpx.HTTPError, KeyError, ValueError):
            self.used_fallback = True
            return self.fallback.search(query, max_price_cents, limit)
        out = [p for p in (from_channel3(i) for i in items) if p]
        self._seen.update({p.product_id: p for p in out})
        return out

    def get(self, product_id: str) -> Product | None:
        if product_id in self._seen:
            return self._seen[product_id]
        try:
            r = self._http.get(f"/v1/products/{product_id}")
            r.raise_for_status()
            return from_channel3(r.json())
        except (httpx.HTTPError, ValueError):
            return self.fallback.get(product_id)


def from_channel3(item: dict) -> Product | None:
    """Channel3 product -> Product, using its first in-stock USD offer."""
    try:
        offer = next(o for o in item.get("offers") or []
                     if o.get("availability", "InStock") == "InStock"
                     and (o.get("price") or {}).get("currency", "USD") == "USD")
        cents = round(float(offer["price"]["price"]) * 100)
        category = (item.get("category") or {}).get("slug") or "uncategorized"
        return Product(
            product_id=str(item["id"]),
            title=str(item.get("title") or "")[:200],
            merchant=merchant_slug(offer["domain"]),
            category=category_slug(category),
            price_cents=cents,
            description=str(item.get("description") or ""),
            url=str(offer.get("url") or ""),
        )
    except (StopIteration, KeyError, TypeError, ValueError):
        return None


class TamperedCatalog:
    """Simulates a compromised merchant page: attacker text appended to one product's
    description. The agent sees it exactly as it would see a real page."""

    def __init__(self, inner, injection: str, product_id: str | None = None):
        # product_id=None tampers every page the agent opens.
        self.inner, self.injection, self.product_id = inner, injection, product_id

    def search(self, *a, **k):
        return self.inner.search(*a, **k)

    def get(self, product_id: str) -> Product | None:
        p = self.inner.get(product_id)
        if p and self.product_id in (None, product_id):
            p = Product(**(asdict(p) | {"description": p.description + "\n\n" + self.injection}))
        return p
