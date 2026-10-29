"""One chat request, start to finish. Each step is a few lines in `complete`, in order:

1. cache    exact-match hit? answer at GBP 0, no routing, no model call
2. route    pinned tier, else the canary router for its share of traffic, else the
            configured router
3. policy   x-gearbox-max-tier and the monthly budget can only lower the tier
4. call     the chosen tier; if it is down: retry once, then the other allowed tiers
            (bigger first, so quality only drops when nothing bigger is allowed or up)
5. store    complete answers go into the cache
6. log      one request_log row, success or failure

Kept apart from the FastAPI layer so it can be tested (and reused by the eval) without
HTTP.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from gearbox.config import TIERS, AppConfig, Tier
from gearbox.routers import Route, Router
from gearbox.service import policy
from gearbox.service.cache import Cache, NoCache, cache_key
from gearbox.service.db import RequestLog, month_spend, write_log
from gearbox.service.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)
from gearbox.tiers import TierError, TierResult, TierUnavailableError, call_tier
from gearbox.tiers import Usage as TierUsage

log = logging.getLogger(__name__)
CallTier = Callable[..., Awaitable[TierResult]]


@dataclass
class Completed:
    response: ChatCompletionResponse
    route: Route
    router: str
    result: TierResult
    cache_hit: bool = False
    policy_notes: list[str] = field(default_factory=list)
    attempts: list[Tier] = field(default_factory=list)  # tiers tried, in order

    @property
    def fallback_used(self) -> bool:
        return len(self.attempts) > 1


class AllTiersFailed(TierUnavailableError):
    """Every allowed tier was unavailable (the last error is in the message)."""


def fallback_order(first: Tier, cap: Tier) -> list[Tier]:
    """The tier itself (tried twice: one retry), then bigger tiers up to the cap, then
    smaller ones. S -> [S, S, M, L]; L with cap L -> [L, L, M, S]."""
    allowed = TIERS[: TIERS.index(cap) + 1]
    i = allowed.index(first)
    return [first, first, *allowed[i + 1 :], *reversed(allowed[:i])]


def in_canary(request_id: str, percent: float) -> bool:
    """Deterministic: the same request id always lands on the same side."""
    bucket = int(hashlib.sha256(request_id.encode()).hexdigest()[:8], 16) % 10_000
    return bucket < percent * 100


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        router: Router,
        sessions: sessionmaker[Session],
        call: CallTier = call_tier,
        cache: Cache | None = None,
        canary: Router | None = None,
    ) -> None:
        self.config = config
        self.router = router
        self.canary = canary
        self.sessions = sessions
        self._call = call
        self._cache = cache or NoCache()

    async def complete(
        self, req: ChatCompletionRequest, request_id: str, max_tier: Tier | None = None
    ) -> Completed:
        """Raises TierError (after logging it) when no allowed tier can answer."""
        start = time.perf_counter()
        row: dict[str, Any] = {"request_id": request_id, "created_at": datetime.now(UTC)}

        # 1. cache
        key = cache_key(req)
        hit = await asyncio.to_thread(self._cache.get, key)
        if hit is not None:
            done = self._from_cache(hit, request_id)
            await self._log(
                RequestLog(
                    **row,
                    status="ok",
                    router="cache",
                    tier=hit["tier"],
                    model=hit["model"],
                    reason="cache hit",
                    cache_hit=True,
                    latency_ms=_ms(start),
                    route_ms=0,
                )
            )
            return done

        # 2. route
        route, router_name = await self._route(req, request_id)
        route_ms = _ms(start)

        # 3. policy
        budget = self.config.policy.monthly_budget_gbp
        spent = await asyncio.to_thread(month_spend, self.sessions) if budget else 0.0
        decision = policy.apply(route.tier, max_tier, spent, budget)
        reason = "; ".join([route.reason, *decision.notes])[:500]
        row |= {"router": router_name, "reason": reason, "route_ms": route_ms}

        # 4. call, with fallback
        attempts: list[Tier] = []
        try:
            result = await self._call_with_fallback(req, decision, attempts)
        except TierError as exc:
            last = attempts[-1] if attempts else decision.tier
            await self._log(
                RequestLog(
                    **row,
                    status="error",
                    tier=last,
                    model=self.config.tiers[last].model,
                    error=str(exc)[:500],
                    fallback_used=len(attempts) > 1,
                    latency_ms=_ms(start),
                )
            )
            raise

        # 5. store (complete answers only)
        if result.finish_reason == "stop":
            await asyncio.to_thread(self._cache.set, key, _to_cache(result))

        # 6. log
        await self._log(
            RequestLog(
                **row,
                status="ok",
                tier=result.tier,
                model=result.model,
                tokens_in=result.usage.input_tokens,
                tokens_out=result.usage.output_tokens,
                cost_gbp=result.cost_gbp,
                fallback_used=len(attempts) > 1,
                latency_ms=_ms(start),
            )
        )
        return Completed(
            _response(request_id, result),
            Route(result.tier, reason),
            router_name,
            result,
            policy_notes=decision.notes,
            attempts=attempts,
        )

    async def _route(self, req: ChatCompletionRequest, request_id: str) -> tuple[Route, str]:
        pinned: Tier | None = req.pinned_tier  # type: ignore[assignment]
        if pinned is not None:
            return Route(pinned, f"pinned by request (model={req.model})"), "pinned"
        router = self.router
        canary = self.config.routing.canary
        if self.canary is not None and canary is not None and in_canary(request_id, canary.percent):
            router = self.canary
        return await router.route(req.routing_text()), router.name

    async def _call_with_fallback(
        self, req: ChatCompletionRequest, decision: policy.Decision, attempts: list[Tier]
    ) -> TierResult:
        last_error: TierUnavailableError | None = None
        for i, tier in enumerate(fallback_order(decision.tier, decision.cap)):
            if i == 1:  # the retry of the same tier: give a blip a moment to clear
                await asyncio.sleep(self.config.fallback.retry_delay_s)
            attempts.append(tier)
            try:
                return await self._call(
                    tier, req.plain_messages(), config=self.config, max_tokens=req.token_limit
                )
            except TierUnavailableError as exc:  # TierRequestError: no other tier would do
                log.warning("tier unavailable", extra={"tier": tier, "error": str(exc)[:200]})
                last_error = exc
        assert last_error is not None
        raise AllTiersFailed(attempts[-1], f"no allowed tier answered ({last_error})")

    def _from_cache(self, hit: dict[str, Any], request_id: str) -> Completed:
        result = TierResult(
            tier=hit["tier"],
            model=hit["model"],
            text=hit["text"],
            finish_reason=hit["finish_reason"],
            usage=TierUsage(hit["input_tokens"], hit["output_tokens"]),
            cost_gbp=0.0,  # served from the cache: nothing was spent
            latency_ms=0,
        )
        return Completed(
            _response(request_id, result),
            Route(result.tier, "cache hit"),
            "cache",
            result,
            cache_hit=True,
        )

    async def _log(self, row: RequestLog) -> None:
        # A logging failure must not lose the user's answer: report it and carry on.
        try:
            await asyncio.to_thread(write_log, self.sessions, row)
        except Exception:
            log.exception("could not write request log", extra={"request_id": row.request_id})


def _to_cache(result: TierResult) -> dict[str, Any]:
    return {
        "tier": result.tier,
        "model": result.model,
        "text": result.text,
        "finish_reason": result.finish_reason,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
    }


def _response(request_id: str, result: TierResult) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=request_id,
        created=int(time.time()),
        model=result.model,
        choices=[
            Choice(message=ResponseMessage(content=result.text), finish_reason=result.finish_reason)
        ],
        usage=Usage(
            prompt_tokens=result.usage.input_tokens,
            completion_tokens=result.usage.output_tokens,
            total_tokens=result.usage.input_tokens + result.usage.output_tokens,
        ),
    )


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
