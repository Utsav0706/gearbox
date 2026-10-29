"""Router v2: ask a model to classify the prompt (prompts/classifier_v1.md).

Smarter than rules, but every route now costs a model call: money (a fraction of a
penny on Haiku) and latency (about a second), paid on every request before the real
answer even starts. That trade-off is what the M5 comparison measures.

Fails safe: if the classifier errors or returns something unreadable, route to M.
M passes 93% of prompts, so it is the best guess when we know nothing; S would risk a
bad answer and L would waste money.
"""

from __future__ import annotations

import json
import re

from gearbox.config import AppConfig, Tier
from gearbox.routers import Route, register
from gearbox.tiers import TierError, call_tier

FALLBACK: Tier = "M"
_JSON = re.compile(r"\{.*\}", re.S)


def parse(text: str) -> Route | None:
    match = _JSON.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    tier = data.get("tier") if isinstance(data, dict) else None
    if tier not in ("S", "M", "L"):
        return None
    return Route(tier, f"llm: {str(data.get('reason', '')).strip()[:200]}")


class LLMRouter:
    name = "v2-llm"

    def __init__(self, config: AppConfig, classifier_tier: Tier, prompt: str) -> None:
        self._config = config
        self._tier = classifier_tier
        self._system = prompt
        self.last_cost_gbp = 0.0  # read by the comparison script

    async def route(self, prompt: str) -> Route:
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": f"<request>\n{prompt}\n</request>"},
        ]
        self.last_cost_gbp = 0.0
        try:
            r = await call_tier(self._tier, messages, config=self._config, max_tokens=1000)
        except TierError as exc:
            return Route(FALLBACK, f"llm: classifier failed ({type(exc).__name__}), using M")
        self.last_cost_gbp = r.cost_gbp
        return parse(r.text) or Route(FALLBACK, "llm: unreadable classifier output, using M")


@register("v2-llm")
def _build(config: AppConfig) -> LLMRouter:
    settings = config.routing.v2_llm
    return LLMRouter(config, settings.tier, settings.prompt.read_text(encoding="utf-8"))
