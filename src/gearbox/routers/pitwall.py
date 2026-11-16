"""Router v4: ask Pitwall's fine-tuned model (an HTTP service) which tier to use.

Pitwall serves a small LLM fine-tuned on Gearbox's own labelled prompts, runs it on CPU
through Ollama, and manages its versions (training, Dyno gate, promotion, rollback).
Gearbox only calls it: POST {url}/route {"prompt", "model"} -> {"tier", ...}.

Registered twice, for canary releases:
- v4-finetuned  -> Pitwall's `production` model
- v4-candidate  -> Pitwall's `candidate` model (e.g. routing.canary: {router:
                   v4-candidate, percent: 10} tries a new version on 10% of traffic)

Fails safe like v2: if Pitwall is down, slow or errors, route to M (the tier that passes
most prompts), so Gearbox keeps answering whatever happens to Pitwall.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from gearbox.config import AppConfig, Tier
from gearbox.routers import Route, register

FALLBACK: Tier = "M"


class PitwallRouter:
    def __init__(
        self,
        name: str,
        model: str,
        url: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._model = model
        self._client = httpx.AsyncClient(base_url=url, timeout=timeout_s, transport=transport)

    async def route(self, prompt: str) -> Route:
        try:
            response = await self._client.post(
                "/route", json={"prompt": prompt, "model": self._model}
            )
            response.raise_for_status()
            data = response.json()
            tier = data["tier"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return Route(FALLBACK, f"pitwall unavailable ({type(exc).__name__}), using M")
        if tier not in ("S", "M", "L"):
            return Route(FALLBACK, f"pitwall returned tier {tier!r}, using M")
        # Pitwall's request id links this decision to Pitwall's own route log.
        reason = f"pitwall v{data.get('model_version')}: {data.get('reason', '')}"
        return Route(tier, f"{reason}; pitwall request {data.get('request_id', '?')}")


def _builder(name: str, model: str) -> Callable[[AppConfig], PitwallRouter]:
    def build(config: AppConfig) -> PitwallRouter:
        settings = config.routing.pitwall
        if settings is None:
            raise ValueError(f"router {name} needs a routing.pitwall section in the config")
        return PitwallRouter(name, model, settings.url, settings.timeout_s)

    return build


register("v4-finetuned")(_builder("v4-finetuned", "production"))
register("v4-candidate")(_builder("v4-candidate", "candidate"))
