"""HTTP runner: lets the console start the agent and watch what it does.

    uvicorn --factory agent.server:app_from_env --port 8001

This is still the agent's side of the fence. It holds an LLM key, a catalog key
and whatever mandate token the caller passes in. It has no PayPal credentials
and no admin token, and it never imports the mandate service. Each run records
what the model searched for, the exact page text it read (attacker text
included), the purchase it proposed, and the verdict the mandate service gave.
"""

import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field
from ulid import ULID

from .__main__ import INJECTION
from .catalog import Channel3Catalog, StaticCatalog, TamperedCatalog
from .loop import run as run_loop

MAX_RUNS_KEPT = 50
MAX_RUNNING = 2
LLM_SETTINGS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


@dataclass
class RunState:
    run_id: str
    task: str
    tamper: bool
    status: str = "running"          # running | done | error
    events: list[dict] = field(default_factory=list)

    def view(self) -> dict:
        return {"run_id": self.run_id, "task": self.task, "tamper": self.tamper,
                "status": self.status, "events": list(self.events)}


class Recording:
    """Wraps a catalog and records what the model saw. Changes nothing."""

    def __init__(self, inner, emit: Callable[[dict], None], injection: str | None):
        self.inner, self.emit, self.injection = inner, emit, injection

    def search(self, query: str, max_price_cents: int | None = None, limit: int = 8):
        found = self.inner.search(query, max_price_cents, limit)
        self.emit({"kind": "search", "query": query,
                   "results": [{"title": p.title, "merchant": p.merchant, "price_cents": p.price_cents}
                               for p in found[:5]], "count": len(found)})
        return found

    def get(self, product_id: str):
        p = self.inner.get(product_id)
        if p is None:
            self.emit({"kind": "note", "text": f"The agent asked for a page that doesn't exist ({product_id[:40]})."})
            return None
        injected = self.injection if self.injection and self.injection in p.description else None
        self.emit({"kind": "page", "title": p.title, "merchant": p.merchant, "price_cents": p.price_cents,
                   "text": p.description, "injected": injected})
        return p


class StartRun(BaseModel):
    task: str = Field(min_length=1, max_length=500)
    token: str = Field(min_length=1, max_length=4096)
    tamper: bool = False


def default_authorize(url: str, intent: dict, token: str) -> dict:
    r = httpx.post(f"{url}/authorize", json=intent, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return r.json()


def create_app(env=os.environ, make_llm=None, authorize=default_authorize, sync: bool = False) -> FastAPI:
    """`sync` runs the agent inline instead of in a thread (tests)."""
    app = FastAPI(title="MANDATE agent runner", version="0.1.0")
    runs: OrderedDict[str, RunState] = OrderedDict()
    lock = threading.Lock()
    make_llm = make_llm or (lambda: OpenAI(api_key=env["LLM_API_KEY"], base_url=env["LLM_BASE_URL"]))
    mandate_url = env.get("MANDATE_URL", "http://localhost:8000").rstrip("/")

    def missing() -> list[str]:
        return [k for k in LLM_SETTINGS if not env.get(k)]

    def work(state: RunState, token: str):
        emit = state.events.append
        try:
            catalog = StaticCatalog()
            if env.get("CHANNEL3_API_KEY"):
                catalog = Channel3Catalog(env["CHANNEL3_API_KEY"])
            source = catalog
            if state.tamper:
                catalog = TamperedCatalog(catalog, INJECTION)
            recorded = Recording(catalog, emit, INJECTION if state.tamper else None)

            def on_step(s: str):
                if s.startswith(("model stopped", "step limit")):
                    emit({"kind": "note", "text": s})

            result = run_loop(state.task, recorded, make_llm(), env["LLM_MODEL"], on_step=on_step)
            if getattr(source, "used_fallback", False):
                emit({"kind": "note", "text": "Channel3 was unavailable, so the bundled snapshot was used."})
            if result.intent is None:
                emit({"kind": "note", "text": "The agent proposed no purchase."})
            else:
                emit({"kind": "intent", "intent": result.intent})
                emit({"kind": "verdict", "verdict": authorize(mandate_url, result.intent, token)})
            state.status = "done"
        except Exception as e:  # the run failed; the console should say so, not hang
            emit({"kind": "error", "text": f"{type(e).__name__}: {str(e)[:300]}"})
            state.status = "error"

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "missing": missing(), "model": env.get("LLM_MODEL", ""),
                "catalog": "channel3" if env.get("CHANNEL3_API_KEY") else "snapshot"}

    @app.post("/runs", status_code=201)
    def start(req: StartRun):
        if missing():
            raise HTTPException(503, f"{', '.join(missing())} not set. See .env.example.")
        state = RunState(run_id=f"run_{ULID()}", task=req.task, tamper=req.tamper)
        with lock:
            if sum(r.status == "running" for r in runs.values()) >= MAX_RUNNING:
                raise HTTPException(429, "The agent is busy. Wait for the current run to finish.")
            runs[state.run_id] = state
            while len(runs) > MAX_RUNS_KEPT:
                runs.popitem(last=False)
        state.events.append({"kind": "task", "text": req.task})
        if sync:
            work(state, req.token)
        else:
            threading.Thread(target=work, args=(state, req.token), daemon=True).start()
        return {"run_id": state.run_id}

    @app.get("/runs/{run_id}")
    def read(run_id: str):
        state = runs.get(run_id)
        if state is None:
            raise HTTPException(404, "No such run.")
        return state.view()

    return app


def app_from_env() -> FastAPI:
    return create_app()
