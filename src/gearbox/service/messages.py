"""POST /v1/messages: the Anthropic-format endpoint, so Anthropic SDK clients (Scout) can
use Gearbox by changing only their base URL.

A pass-through, not a translation: the request body goes to Anthropic unchanged except
for `model` (and, when the model is swapped, the refusal-fallback option). Every
Anthropic feature the caller uses (tools, structured output, effort, thinking blocks,
betas) keeps working, including ones Gearbox has never heard of.

Which tier answers:
1. The model the caller asked for is the ceiling: a Haiku request is never upgraded.
2. A task-type tag (x-gearbox-task-type, or Scout's x-scout-task-type) picks the tier
   from config routing.task_types; otherwise the configured router reads the last user
   message.
3. The max-tier header and the monthly budget can only lower it.
Only the Anthropic tiers serve this endpoint: the local model can't do tool calls or
structured output reliably.

Failures: 429, 5xx, 529 and connection errors are retried once on the same tier, then
another allowed Anthropic tier is tried. Any other error (a 400, say) is Anthropic's
answer to the caller's request and is passed back unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from opentelemetry.trace import StatusCode
from sqlalchemy.orm import Session, sessionmaker

from gearbox.config import TIERS, AppConfig, Tier
from gearbox.routers import Router
from gearbox.service import policy
from gearbox.service.db import RequestLog, month_spend, write_log
from gearbox.service.headers import header_safe
from gearbox.telemetry import tracer
from gearbox.tiers import Usage, cost_gbp

log = logging.getLogger(__name__)
ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}  # 529 = Anthropic "overloaded"
TASK_TYPE_HEADERS = ("x-gearbox-task-type", "x-scout-task-type")
FALLBACK_BETA_PREFIX = "server-side-fallback"


@dataclass
class ProxyResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def error(status: int, kind: str, message: str) -> ProxyResponse:
    """Anthropic's error shape, so the caller's SDK raises the right exception."""
    return ProxyResponse(status, {"type": "error", "error": {"type": kind, "message": message}})


def model_id(config: AppConfig, tier: Tier) -> str:
    return config.tiers[tier].model.removeprefix("anthropic/")


def anthropic_tiers(config: AppConfig) -> list[Tier]:
    return [t for t in TIERS if config.tiers[t].model.startswith("anthropic/")]


def tier_of(config: AppConfig, model: str) -> Tier | None:
    return next((t for t in anthropic_tiers(config) if model_id(config, t) == model), None)


def last_user_text(body: Mapping[str, Any]) -> str:
    """The text of the last user turn (tool results and images ignored)."""
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        texts = [b.get("text", "") for b in content or [] if b.get("type") == "text"]
        if any(texts):
            return "\n".join(texts)
    return ""


def upstream_request(
    body: dict[str, Any], betas: list[str], target: str, requested: str
) -> tuple[dict[str, Any], list[str]]:
    """The body and betas to send for `target`. When Gearbox swaps the model, the
    caller's refusal-fallback choice (made for the model it asked for) is dropped:
    Haiku has no server-side fallback and would reject it."""
    payload = {**body, "model": target}
    if target != requested:
        payload.pop("fallbacks", None)
        betas = [b for b in betas if not b.startswith(FALLBACK_BETA_PREFIX)]
    return payload, betas


class MessagesProxy:
    def __init__(
        self,
        config: AppConfig,
        router: Router,
        sessions: sessionmaker[Session],
        api_key: str | None,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.router = router
        self.sessions = sessions
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._transport = transport

    async def handle(
        self,
        body: dict[str, Any],
        beta: bool,
        headers: Mapping[str, str],
        max_tier: Tier | None,
    ) -> ProxyResponse:
        request_id = f"gbx-{uuid.uuid4().hex[:24]}"
        with tracer().start_as_current_span("gearbox.request") as span:
            span.set_attribute("langfuse.trace.name", "messages")
            span.set_attribute("gearbox.request_id", request_id)
            resp = await self._handle(body, beta, headers, max_tier, request_id)
            span.set_attribute("gearbox.status", "ok" if resp.status == 200 else "error")
            for k, v in resp.headers.items():
                if k.startswith("x-gearbox-"):
                    span.set_attribute(k.replace("x-", "").replace("-", "."), v)
            if resp.status != 200:
                span.set_status(StatusCode.ERROR, str(resp.status))
            resp.headers["x-request-id"] = request_id
            return resp

    async def _handle(
        self,
        body: dict[str, Any],
        beta: bool,
        headers: Mapping[str, str],
        max_tier: Tier | None,
        request_id: str,
    ) -> ProxyResponse:
        start = time.perf_counter()
        if not self._key:
            return error(503, "api_error", "Gearbox has no ANTHROPIC_API_KEY configured")
        if body.get("stream"):
            return error(400, "invalid_request_error", "stream=true is not supported")
        requested = str(body.get("model", ""))
        ceiling = tier_of(self.config, requested)
        if ceiling is None:
            served = ", ".join(model_id(self.config, t) for t in anthropic_tiers(self.config))
            return error(
                400,
                "invalid_request_error",
                f"model {requested!r} is not served by Gearbox (use one of: {served})",
            )

        # Which tier: task-type tag, else the router; then the ceiling; then policy.
        task = next((headers[h] for h in TASK_TYPE_HEADERS if headers.get(h)), None)
        if task is not None and task in self.config.routing.task_types:
            tier: Tier = self.config.routing.task_types[task]
            router_name, reason = "task-type", f"task type {task!r} -> {tier}"
        else:
            route = await self.router.route(last_user_text(body))
            tier, router_name, reason = route.tier, self.router.name, route.reason
        route_ms = int((time.perf_counter() - start) * 1000)
        if TIERS.index(tier) > TIERS.index(ceiling):
            reason += f"; capped at {ceiling} (the caller asked for {requested})"
            tier = ceiling
        budget = self.config.policy.monthly_budget_gbp
        spent = await asyncio.to_thread(month_spend, self.sessions) if budget else 0.0
        decision = policy.apply(tier, max_tier, spent, budget)
        allowed = [
            t for t in anthropic_tiers(self.config) if TIERS.index(t) <= TIERS.index(decision.cap)
        ]
        notes = list(decision.notes)
        if not allowed:
            return error(
                503,
                "api_error",
                "no model allowed: " + "; ".join(notes or ["cap below every tier"]),
            )
        if decision.tier not in allowed:  # e.g. the router chose S: not served here
            notes.append(f"{decision.tier} not served on /v1/messages, using {allowed[0]}")
        first = decision.tier if decision.tier in allowed else allowed[0]
        reason = "; ".join([reason, *notes])

        order = [first, first, *[t for t in reversed(allowed) if t != first]]
        betas = [b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
        url = f"{self._base}/v1/messages" + ("?beta=true" if beta else "")
        attempts: list[Tier] = []
        last: httpx.Response | None = None
        async with httpx.AsyncClient(transport=self._transport) as client:
            for i, t in enumerate(order):
                if i == 1:
                    await asyncio.sleep(self.config.fallback.retry_delay_s)
                attempts.append(t)
                payload, sent_betas = upstream_request(
                    body, betas, model_id(self.config, t), requested
                )
                out_headers = {
                    "x-api-key": self._key,
                    "anthropic-version": headers.get("anthropic-version", ANTHROPIC_VERSION),
                    "content-type": "application/json",
                }
                if sent_betas:
                    out_headers["anthropic-beta"] = ",".join(sent_betas)
                with tracer().start_as_current_span(f"call {t}") as span:
                    span.set_attribute("langfuse.observation.type", "generation")
                    span.set_attribute("gen_ai.request.model", payload["model"])
                    try:
                        last = await client.post(
                            url,
                            json=payload,
                            headers=out_headers,
                            timeout=self.config.tiers[t].timeout_s,
                        )
                    except httpx.TransportError as exc:
                        span.set_status(StatusCode.ERROR, type(exc).__name__)
                        last = None
                        continue
                    if last.status_code in RETRY_STATUSES:
                        span.set_status(StatusCode.ERROR, str(last.status_code))
                        continue
                    break

        row: dict[str, Any] = {
            "request_id": request_id,
            "created_at": datetime.now(UTC),
            "router": router_name,
            "tier": attempts[-1],
            "model": self.config.tiers[attempts[-1]].model,
            "reason": f"/v1/messages: {reason}"[:500],
            "route_ms": route_ms,
            "fallback_used": len(attempts) > 1,
        }
        if last is None:
            await self._log(
                RequestLog(
                    **row, status="error", latency_ms=_ms(start), error="could not reach Anthropic"
                )
            )
            return error(503, "api_error", "could not reach Anthropic on any allowed tier")
        try:
            data = last.json()
        except ValueError:
            data = {"type": "error", "error": {"type": "api_error", "message": last.text[:500]}}
        if last.status_code != 200:
            message = str(data.get("error", {}).get("message", ""))[:300]
            await self._log(
                RequestLog(
                    **row,
                    status="error",
                    latency_ms=_ms(start),
                    error=f"HTTP {last.status_code}: {message}",
                )
            )
            return ProxyResponse(last.status_code, data, {"x-gearbox-tier": attempts[-1]})

        u = data.get("usage") or {}
        usage = Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0))
        tier_model = self.config.tiers[attempts[-1]].model
        cost = cost_gbp(usage, self.config.prices_usd_per_mtok[tier_model], self.config.usd_to_gbp)
        await self._log(
            RequestLog(
                **row,
                status="ok",
                tokens_in=usage.input_tokens,
                tokens_out=usage.output_tokens,
                cost_gbp=cost,
                latency_ms=_ms(start),
            )
        )
        out = {
            "x-gearbox-tier": attempts[-1],
            "x-gearbox-model": tier_model,
            "x-gearbox-cost": f"{cost:.6f}",
            "x-gearbox-router": router_name,
            "x-gearbox-reason": header_safe(reason),
        }
        if len(attempts) > 1:
            out["x-gearbox-fallback"] = "->".join(attempts)
        if notes:
            out["x-gearbox-policy"] = header_safe("; ".join(notes))
        return ProxyResponse(200, data, out)

    async def _log(self, row: RequestLog) -> None:
        try:
            await asyncio.to_thread(write_log, self.sessions, row)
        except Exception:  # never lose the caller's answer because the log failed
            log.exception("could not write request log")


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
