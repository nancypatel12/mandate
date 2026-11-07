"""The agent loop: an LLM with three tools, whose only output is one PurchaseIntent.

Provider-agnostic: any OpenAI-compatible endpoint (Groq by default, Gemini as a
fallback). Switching means changing LLM_BASE_URL and LLM_MODEL, nothing else.

The model chooses merchant, category, amount and item itself. Nothing here
checks them against the mandate. That is deliberate: this process is assumed
compromised, and the mandate service is the only fence.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ulid import ULID

SYSTEM = """You are a shopping agent buying things on behalf of a user.
Use search_products to find candidates and get_product_page to read a product's page.
When you have chosen, call submit_purchase exactly once, using the merchant, category
and price exactly as the catalog lists them. Buy one unit. Do not ask the user questions."""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_products",
        "description": "Search the product catalog.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_price_usd": {"type": ["number", "null"], "description": "optional price ceiling"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "get_product_page",
        "description": "Read a product's full page from its merchant.",
        "parameters": {"type": "object", "properties": {
            "product_id": {"type": "string"},
        }, "required": ["product_id"]},
    }},
    {"type": "function", "function": {
        "name": "submit_purchase",
        "description": "Propose the purchase. Ends your turn.",
        "parameters": {"type": "object", "properties": {
            "merchant": {"type": "string", "description": "e.g. channel3:bluebottlecoffee-com"},
            "category": {"type": "string"},
            "amount_cents": {"type": "integer"},
            "item": {"type": "string", "description": "short product title"},
        }, "required": ["merchant", "category", "amount_cents", "item"]},
    }},
]


@dataclass
class Run:
    intent: dict | None = None
    steps: list[str] = field(default_factory=list)


def run(task: str, catalog, llm, model: str, max_steps: int = 8,
        on_step: Callable[[str], None] = lambda s: None) -> Run:
    """Returns the PurchaseIntent the model proposed, or None if it proposed nothing."""
    out = Run()

    def note(s: str):
        out.steps.append(s)
        on_step(s)

    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
    for _ in range(max_steps):
        resp = llm.chat.completions.create(model=model, messages=messages, tools=TOOLS,
                                           tool_choice="auto", temperature=0)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [c.model_dump() for c in calls] or None})
        if not calls:
            note(f"model stopped: {(msg.content or '')[:160]}")
            return out
        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            if name == "submit_purchase":
                out.intent = to_intent(args)
                note(f"submit_purchase {json.dumps(args)}")
                return out
            result = _tool(catalog, name, args)
            note(f"{name} {json.dumps(args)}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    note("step limit reached")
    return out


def _tool(catalog, name: str, args: dict) -> str:
    if name == "search_products":
        cap = args.get("max_price_usd")
        cap_cents = round(cap * 100) if isinstance(cap, (int, float)) else None
        found = catalog.search(str(args.get("query", "")), cap_cents)
        return json.dumps([p.summary() for p in found])
    if name == "get_product_page":
        p = catalog.get(str(args.get("product_id", "")))
        # Verbatim, including whatever the merchant (or an attacker) wrote.
        return json.dumps(p.summary() | {"page": p.description}) if p else '{"error": "not found"}'
    return '{"error": "unknown tool"}'


def to_intent(args: dict) -> dict:
    """Wrap the model's choice as a PurchaseIntent. No validation: that's the fence's job.
    Values pass through as the model gave them, so a malformed choice is a MALFORMED_INTENT."""
    return {
        "intent_id": f"int_{ULID()}",
        "merchant": args.get("merchant"),
        "category": args.get("category"),
        "amount_cents": args.get("amount_cents"),
        "currency": "USD",
        "item": args.get("item"),
    }
