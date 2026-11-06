"""HTTP surface. Run: uvicorn --factory mandate.main:app_from_env

Human endpoints (issue, revoke) need MANDATE_ADMIN_TOKEN. The agent's only
endpoint is POST /authorize, with its mandate token as the bearer.
"""

import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from . import db, funding, ledger, service
from .paypal import PayPal, PayPalError

MAX_BODY_BYTES = 16_384


@dataclass(frozen=True)
class Settings:
    signing_key: str
    admin_token: str
    db_path: str
    paypal: PayPal | None


def settings_from_env(env=os.environ) -> Settings:
    key = env.get("MANDATE_SIGNING_KEY", "")
    if len(key.encode()) < 32:
        raise SystemExit("MANDATE_SIGNING_KEY must be set and at least 32 bytes. See .env.example.")
    pp = None
    if env.get("PAYPAL_CLIENT_ID") and env.get("PAYPAL_CLIENT_SECRET"):
        pp = PayPal(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"],
                    env.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"))
    return Settings(key, env.get("MANDATE_ADMIN_TOKEN", ""), env.get("MANDATE_DB", "mandate.db"), pp)


class NotConfigured(PayPalError):
    public_message = "PayPal is not configured on this server. Nothing was charged."


def _unconfigured_broker(intent, mandate):
    raise NotConfigured("paypal credentials missing")


def broker_for(conn, pp: PayPal | None):
    # No PayPal means no ALLOW: the API never approves a purchase it cannot place.
    return funding.make_broker(conn, pp) if pp else _unconfigured_broker


class IssueRequest(BaseModel):
    agent: str
    user: str
    merchants: list[str]
    categories: list[str]
    per_txn_cap_cents: int
    period: str
    period_cap_cents: int
    max_txn_per_period: int
    ttl_seconds: int = 86_400


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="MANDATE", version="0.1.0")
    db.connect(settings.db_path).close()  # create the schema at startup

    def conn():
        c = db.connect(settings.db_path)
        try:
            yield c
        finally:
            c.close()

    def admin(authorization: str = Header(default="")):
        if not settings.admin_token:
            raise HTTPException(503, "MANDATE_ADMIN_TOKEN is not set; human endpoints are disabled.")
        if not hmac.compare_digest(authorization.encode(), f"Bearer {settings.admin_token}".encode()):
            raise HTTPException(401, "Admin token required.")

    def now() -> datetime:
        return datetime.now(UTC)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "paypal": "configured" if settings.paypal else "missing"}

    @app.post("/mandates", dependencies=[Depends(admin)], status_code=201)
    def issue(req: IssueRequest, c=Depends(conn)):
        try:
            token, claims = service.issue(c, settings.signing_key, now=now(), **req.model_dump())
        except (ValueError, ValidationError) as e:
            raise HTTPException(422, f"Invalid mandate: {str(e).splitlines()[0]}") from None
        return {"token": token, "mandate": claims.model_dump(mode="json")}

    @app.get("/mandates/{mandate_id}")
    def read(mandate_id: str, c=Depends(conn)):
        s = service.status(c, mandate_id, now())
        if s is None:
            raise HTTPException(404, "No such mandate.")
        return s

    @app.post("/mandates/{mandate_id}/revoke", dependencies=[Depends(admin)])
    def revoke(mandate_id: str, c=Depends(conn)):
        if not service.revoke(c, mandate_id, now()):
            raise HTTPException(404, "No such mandate.")
        return {"mandate_id": mandate_id, "revoked": True}

    @app.post("/authorize")
    async def authorize(request: Request, authorization: str = Header(default=""), c=Depends(conn)):
        # Parse the body ourselves: a malformed intent is a DENY in the ledger, not a 422.
        body = await request.body()
        try:
            raw = json.loads(body) if len(body) <= MAX_BODY_BYTES else None
        except ValueError:
            raw = None
        token = authorization.removeprefix("Bearer ").strip()
        result = await run_in_threadpool(
            service.authorize, c, settings.signing_key, token, raw, now(), broker_for(c, settings.paypal))
        # 200 for both verdicts. A denial is the product working, not an error.
        return result.to_dict()

    @app.get("/ledger")
    def get_ledger(limit: int = Query(50, ge=1, le=500), before: int | None = None, c=Depends(conn)):
        return ledger.page(c, limit=limit, before=before)

    @app.get("/funding/return", response_class=HTMLResponse, include_in_schema=False)
    def funding_return():
        return "<p>Wallet approved. Return to the terminal; MANDATE will finish linking it.</p>"

    return app


def app_from_env() -> FastAPI:
    return create_app(settings_from_env())
