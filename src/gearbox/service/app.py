"""FastAPI app: an OpenAI-compatible endpoint in front of the three tiers.

    py -m gearbox.service            # http://127.0.0.1:8002 (8000 is reserved on Windows,
                                     # 8001 is Scout's API)

Any OpenAI client works by changing its base URL:
    OpenAI(base_url="http://127.0.0.1:8002/v1", api_key="unused").chat.completions.create(
        model="auto", messages=[{"role": "user", "content": "What is 17 * 23?"}])

Endpoints: POST /v1/chat/completions, GET /health, GET /stats?hours=24.
Every completion carries x-gearbox-tier / -model / -cost / -router headers, so a
caller can see what each answer cost without parsing logs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from gearbox.config import AppConfig, Secrets, get_config, get_secrets
from gearbox.log import setup_logging
from gearbox.routers import Router, create_router
from gearbox.service import policy
from gearbox.service.cache import Cache, make_cache
from gearbox.service.db import db_is_up, make_engine, make_sessions, stats
from gearbox.service.pipeline import CallTier, Pipeline
from gearbox.service.schemas import ChatCompletionRequest, error_body
from gearbox.tiers import TierRequestError, TierUnavailableError, call_tier

log = logging.getLogger(__name__)
SAMPLING_NOTE = "temperature/top_p are not forwarded (the Claude tiers reject them)"


def create_app(
    config: AppConfig | None = None,
    secrets: Secrets | None = None,
    router: Router | None = None,
    engine: Engine | None = None,
    call: CallTier = call_tier,
    warm_up: bool = True,
    cache: Cache | None = None,
    canary: Router | None = None,
) -> FastAPI:
    config = config or get_config()
    secrets = secrets or get_secrets()
    engine = engine or make_engine(secrets.database_url)
    router = router or create_router(config.routing.router, config)
    if canary is None and config.routing.canary is not None:
        canary = create_router(config.routing.canary.router, config)
    if cache is None:
        c = config.cache
        cache = make_cache(c.enabled, c.ttl_s, c.max_entries, secrets.redis_url)
    pipeline = Pipeline(config, router, make_sessions(engine), call, cache, canary)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if warm_up:
            # Load the routers' models now (v3: ~2 s), not inside the first user request.
            for r in (router, canary):
                if r is not None:
                    await r.route("warm up")
        yield
        engine.dispose()

    app = FastAPI(title="Gearbox", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # OpenAI clients expect 400 + {"error": {...}}, not FastAPI's 422 shape.
        first = exc.errors()[0] if exc.errors() else {}
        message = str(first.get("msg", "invalid request")).removeprefix("Value error, ")
        return JSONResponse(error_body(message, "invalid_request_error", "invalid_request"), 400)

    @app.post("/v1/chat/completions")
    async def chat_completions(
        req: ChatCompletionRequest,
        x_gearbox_max_tier: str | None = Header(default=None),
    ) -> JSONResponse:
        request_id = f"gbx-{uuid.uuid4().hex[:24]}"
        try:
            max_tier = policy.parse_max_tier(x_gearbox_max_tier)
        except ValueError as exc:
            return JSONResponse(error_body(str(exc), "invalid_request_error", "bad_header"), 400)
        try:
            done = await pipeline.complete(req, request_id, max_tier)
        except TierUnavailableError as exc:
            return JSONResponse(
                error_body(str(exc), "service_unavailable", "tier_unavailable"),
                503,
                headers={"x-request-id": request_id},
            )
        except TierRequestError as exc:
            return JSONResponse(
                error_body(str(exc), "invalid_request_error", "tier_rejected_request"),
                400,
                headers={"x-request-id": request_id},
            )
        headers = {
            "x-request-id": request_id,
            "x-gearbox-tier": done.route.tier,
            "x-gearbox-model": done.result.model,
            "x-gearbox-cost": f"{done.result.cost_gbp:.6f}",  # GBP
            "x-gearbox-router": done.router,
            "x-gearbox-cache": "hit" if done.cache_hit else "miss",
        }
        if done.policy_notes:
            headers["x-gearbox-policy"] = "; ".join(done.policy_notes)
        if done.fallback_used:
            headers["x-gearbox-fallback"] = "->".join(done.attempts)  # e.g. "S->S->M"
        if req.temperature is not None or req.top_p is not None:
            headers["x-gearbox-note"] = SAMPLING_NOTE
        log.info(
            "completion",
            extra={
                "request_id": request_id,
                "router": done.router,
                "tier": done.route.tier,
                "cost_gbp": round(done.result.cost_gbp, 6),
            },
        )
        return JSONResponse(done.response.model_dump(), headers=headers)

    @app.get("/health")
    async def health() -> JSONResponse:
        database = db_is_up(engine)
        body = {
            "status": "ok" if database else "degraded",
            "database": database,
            "router": router.name,
            "tiers": {name: t.model for name, t in config.tiers.items()},
        }
        return JSONResponse(body, 200 if database else 503)

    @app.get("/stats")
    async def get_stats(hours: int = Query(24, ge=1, le=24 * 31)) -> dict[str, object]:
        return stats(pipeline.sessions, hours)

    return app


def main() -> None:
    import uvicorn

    secrets = get_secrets()
    setup_logging(secrets.log_level)
    uvicorn.run(create_app(secrets=secrets), host="127.0.0.1", port=8002, log_config=None)


if __name__ == "__main__":
    main()
