"""call_tier: the single place Gearbox calls a model.

Everything else (the dataset runner, the judge, the API) asks for a tier, never a
model: the tier's model, token cap, timeout and price come from config.yaml.

Why LiteLLM: tier S is a local Ollama model and M/L are Anthropic models. LiteLLM
gives one OpenAI-shaped call for both, so the rest of Gearbox never branches on
provider. It is pinned to an exact, two-week-old release in pyproject.toml (its PyPI
package was briefly compromised in March 2026).

Errors are split in two, because the fallback layer (M7) treats them differently:
- TierUnavailableError: this tier can't serve the request right now (down, slow,
  rate-limited, no key, prompt too long for it). Another tier might.
- TierRequestError: the request itself is wrong. Another tier would fail the same way.

Smoke test one tier:  py -m gearbox.tiers M "Say hello in five words."
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

import litellm

from gearbox.config import AppConfig, ModelPrice, Secrets, Tier, get_config, get_secrets
from gearbox.log import setup_logging

litellm.suppress_debug_info = True  # its "Give Feedback" banners would break JSON logs

log = logging.getLogger(__name__)

# Errors after which another tier might still succeed. ContextWindowExceededError is a
# BadRequestError, so it is checked before the request errors below.
_UNAVAILABLE = (
    litellm.ContextWindowExceededError,
    litellm.Timeout,
    litellm.APIConnectionError,
    litellm.RateLimitError,
    litellm.AuthenticationError,
    litellm.PermissionDeniedError,
    litellm.NotFoundError,  # e.g. the Ollama model was never pulled
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.BadGatewayError,
    litellm.APIError,
)
_REQUEST = (
    litellm.BadRequestError,
    litellm.UnprocessableEntityError,
    litellm.UnsupportedParamsError,
)


class TierError(Exception):
    def __init__(self, tier: Tier, message: str) -> None:
        super().__init__(f"tier {tier}: {message}")
        self.tier = tier


class TierUnavailableError(TierError):
    """Down, slow, rate-limited, unauthorised or too small for this prompt."""


class TierRequestError(TierError):
    """The request is malformed; retrying on another tier won't help."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class TierResult:
    tier: Tier
    model: str  # the configured model id, e.g. anthropic/claude-haiku-5-5
    text: str
    # "stop", or "length" when max_tokens cut the answer short. Returned rather than
    # raised: a truncated answer is still an answer, and the judge will score it.
    finish_reason: str
    usage: Usage
    cost_gbp: float
    latency_ms: int


def cost_gbp(usage: Usage, price: ModelPrice, usd_to_gbp: float) -> float:
    usd = (usage.input_tokens * price.input + usage.output_tokens * price.output) / 1_000_000
    return usd * usd_to_gbp


def _api_key(model: str, secrets: Secrets) -> str | None:
    if model.startswith("anthropic/"):
        if secrets.anthropic_api_key is None:
            return None
        return secrets.anthropic_api_key.get_secret_value()
    return None  # local models need no key


async def call_tier(
    tier: Tier,
    messages: list[dict[str, Any]],
    *,
    config: AppConfig | None = None,
    secrets: Secrets | None = None,
    max_tokens: int | None = None,
) -> TierResult:
    """One model call on `tier`. max_tokens can lower the tier's cap, never raise it."""
    config = config or get_config()
    secrets = secrets or get_secrets()
    t = config.tiers[tier]

    api_key = _api_key(t.model, secrets)
    if t.model.startswith("anthropic/") and api_key is None:
        raise TierUnavailableError(tier, "ANTHROPIC_API_KEY is not set (add it to .env)")

    kwargs: dict[str, Any] = {
        "model": t.model,
        "messages": messages,
        "max_tokens": min(max_tokens or t.max_tokens, t.max_tokens),
        "timeout": t.timeout_s,
        # No hidden retries: the fallback layer decides what happens after a failure,
        # and a silent retry would double the latency it measures.
        "num_retries": 0,
        **t.params,
    }
    if t.api_base:
        kwargs["api_base"] = t.api_base
    if api_key:
        kwargs["api_key"] = api_key

    start = time.perf_counter()
    try:
        resp = await litellm.acompletion(**kwargs)
    except _UNAVAILABLE as exc:
        raise TierUnavailableError(tier, f"{type(exc).__name__}: {exc}") from None
    except _REQUEST as exc:
        raise TierRequestError(tier, f"{type(exc).__name__}: {exc}") from None
    latency_ms = int((time.perf_counter() - start) * 1000)

    choice = resp.choices[0]
    u = resp.usage
    usage = Usage(input_tokens=u.prompt_tokens or 0, output_tokens=u.completion_tokens or 0)
    cost = cost_gbp(usage, config.prices_usd_per_mtok[t.model], config.usd_to_gbp)
    log.info(
        "tier call",
        extra={
            "tier": tier,
            "model": t.model,
            "finish_reason": choice.finish_reason,
            "in": usage.input_tokens,
            "out": usage.output_tokens,
            "cost_gbp": round(cost, 6),
            "ms": latency_ms,
        },
    )
    return TierResult(
        tier=tier,
        model=t.model,
        text=choice.message.content or "",
        finish_reason=choice.finish_reason or "stop",
        usage=usage,
        cost_gbp=cost,
        latency_ms=latency_ms,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="py -m gearbox.tiers", description=__doc__)
    parser.add_argument("tier", choices=["S", "M", "L"])
    parser.add_argument("prompt")
    parser.add_argument("--max-tokens", type=int, default=None)
    args = parser.parse_args(argv)

    setup_logging(get_secrets().log_level)
    messages = [{"role": "user", "content": args.prompt}]
    try:
        r = asyncio.run(call_tier(args.tier, messages, max_tokens=args.max_tokens))
    except TierError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"\n{r.text}\n")
    print(
        f"[{r.tier} {r.model}] {r.usage.input_tokens} in / {r.usage.output_tokens} out, "
        f"£{r.cost_gbp:.5f}, {r.latency_ms} ms, finish={r.finish_reason}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
