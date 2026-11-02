"""The "Try it" page's way to talk to Gearbox: one call to the running API.

Through HTTP on purpose, exactly like any other app, so what the page shows is the real
service (router, cache, budget policy, fallback), and the dashboard never needs a model
key. Every failure comes back as a TryResult with a readable `error`, never an exception:
the page only has to display it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

TIMEOUT_S = 300.0  # a long answer from the large tier can take a couple of minutes


@dataclass
class TryResult:
    ok: bool
    answer: str = ""
    tier: str = ""
    model: str = ""
    router: str = ""
    reason: str = ""
    cost_gbp: float = 0.0
    seconds: float = 0.0
    cache_hit: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str = ""
    policy: str | None = None
    fallback: str | None = None
    note: str | None = None
    error: str | None = None


def ask(
    base_url: str,
    prompt: str,
    model: str = "auto",
    max_tier: str | None = None,
    max_tokens: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> TryResult:
    """Send one prompt; `transport` lets tests swap in a fake API."""
    body: dict[str, object] = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if max_tokens:
        body["max_tokens"] = max_tokens
    headers = {"x-gearbox-max-tier": max_tier} if max_tier else {}
    start = time.perf_counter()
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_S) as client:
            r = client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions", json=body, headers=headers
            )
    except httpx.ConnectError:
        return TryResult(
            ok=False,
            error=f"Can't reach Gearbox at {base_url}. Start it with "
            "`py -m gearbox.service` (or scripts/dev_api.py) and try again.",
        )
    except httpx.TimeoutException:
        return TryResult(ok=False, error=f"No answer within {TIMEOUT_S:.0f} s.")
    seconds = time.perf_counter() - start

    if r.status_code != 200:
        try:
            message = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = r.text[:300]
        return TryResult(ok=False, seconds=seconds, error=f"HTTP {r.status_code}: {message}")

    data = r.json()
    choice = data["choices"][0]
    h = r.headers
    return TryResult(
        ok=True,
        answer=choice["message"]["content"],
        tier=h.get("x-gearbox-tier", ""),
        model=h.get("x-gearbox-model", data.get("model", "")),
        router=h.get("x-gearbox-router", ""),
        reason=h.get("x-gearbox-reason", ""),
        cost_gbp=float(h.get("x-gearbox-cost", 0.0)),
        seconds=seconds,
        cache_hit=h.get("x-gearbox-cache") == "hit",
        tokens_in=data["usage"]["prompt_tokens"],
        tokens_out=data["usage"]["completion_tokens"],
        finish_reason=choice["finish_reason"],
        policy=h.get("x-gearbox-policy"),
        fallback=h.get("x-gearbox-fallback"),
        note=h.get("x-gearbox-note"),
    )
