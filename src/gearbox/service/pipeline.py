"""One chat request, start to finish: route -> call the tier -> log -> respond.

Kept apart from the FastAPI layer so it can be tested (and later reused by the eval)
without HTTP. M7 inserts the cache before routing, the policy layer after it, and the
fallback around the tier call; each is a step in `complete`, in that order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from gearbox.config import AppConfig, Tier
from gearbox.routers import Route, Router
from gearbox.service.db import RequestLog, write_log
from gearbox.service.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)
from gearbox.tiers import TierError, TierResult, call_tier

log = logging.getLogger(__name__)
CallTier = Callable[..., Awaitable[TierResult]]


@dataclass
class Completed:
    response: ChatCompletionResponse
    route: Route
    router: str
    result: TierResult


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        router: Router,
        sessions: sessionmaker[Session],
        call: CallTier = call_tier,
    ) -> None:
        self.config = config
        self.router = router
        self.sessions = sessions
        self._call = call

    async def complete(self, req: ChatCompletionRequest, request_id: str) -> Completed:
        """Raises TierError (after logging it) when the tier can't answer."""
        start = time.perf_counter()
        route, router_name = await self._route(req)
        route_ms = int((time.perf_counter() - start) * 1000)
        row: dict[str, Any] = {
            "request_id": request_id,
            "created_at": datetime.now(UTC),
            "router": router_name,
            "tier": route.tier,
            "model": self.config.tiers[route.tier].model,
            "reason": route.reason[:500],
            "route_ms": route_ms,
        }
        try:
            result = await self._call(
                route.tier,
                req.plain_messages(),
                config=self.config,
                max_tokens=req.token_limit,
            )
        except TierError as exc:
            await self._log(
                RequestLog(**row, status="error", error=str(exc)[:500], latency_ms=_ms(start))
            )
            raise

        await self._log(
            RequestLog(
                **row,
                status="ok",
                tokens_in=result.usage.input_tokens,
                tokens_out=result.usage.output_tokens,
                cost_gbp=result.cost_gbp,
                latency_ms=_ms(start),
            )
        )
        response = ChatCompletionResponse(
            id=request_id,
            created=int(time.time()),
            model=result.model,
            choices=[
                Choice(
                    message=ResponseMessage(content=result.text),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=Usage(
                prompt_tokens=result.usage.input_tokens,
                completion_tokens=result.usage.output_tokens,
                total_tokens=result.usage.input_tokens + result.usage.output_tokens,
            ),
        )
        return Completed(response, route, router_name, result)

    async def _route(self, req: ChatCompletionRequest) -> tuple[Route, str]:
        pinned: Tier | None = req.pinned_tier  # type: ignore[assignment]
        if pinned is not None:
            return Route(pinned, f"pinned by request (model={req.model})"), "pinned"
        return await self.router.route(req.routing_text()), self.router.name

    async def _log(self, row: RequestLog) -> None:
        # A logging failure must not lose the user's answer: report it and carry on.
        try:
            await asyncio.to_thread(write_log, self.sessions, row)
        except Exception:
            log.exception("could not write request log", extra={"request_id": row.request_id})


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
