"""Router v1: hand-written rules. Free, instant, explainable, and the baseline to beat.

Rules written from what the M4 labels taught (looking at the training split only):
- The small local model (S) fails most factual questions: it hallucinates trivia and
  repeats common misconceptions, so factual questions go to M.
- It also fails tricky code and multi-step maths, which tend to say so ("implement",
  "O(n)", "prove", "probability"), so those go to M.
- Proofs and long multi-part requests are where even M fails, so they go to L.
- Everything else (simple writing, simple maths, short code) stays on S.
Prompt length alone is not one of the rules: in the labels, prompts that only L
answers well are, if anything, shorter than the rest.
"""

from __future__ import annotations

import re

from gearbox.config import AppConfig
from gearbox.routers import Route, register

# Each rule: (pattern, tier, reason). The first match wins; order = most specific first.
_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b(prove|proof|show that|irrational)\b", re.I), "L", "asks for a proof"),
    (
        re.compile(r"\b(implement|O\([^)]*\)|algorithm|optimi[sz]e|recursive|class \w+)", re.I),
        "M",
        "non-trivial code (implement / algorithm / complexity)",
    ),
    (
        re.compile(
            r"\b(probability|integer[s]? (?:such|that)|remainder|polynomial|"
            r"how many ways|matrix|derivative|integral)\b",
            re.I,
        ),
        "M",
        "multi-step maths",
    ),
    (
        re.compile(
            r"^\s*(who|which|when|where|in what|what (?:is|was|are|were|happens|"
            r"do|does|did))\b",
            re.I,
        ),
        "M",
        "factual question (the small model often hallucinates facts)",
    ),
    (
        re.compile(r"\b(is it true|myth|really|actually|scientifically|according to)\b", re.I),
        "M",
        "fact check (the small model repeats misconceptions)",
    ),
]
_MANY_PARTS = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s")
MULTI_PART_MIN = 4  # a request listing this many numbered/bulleted parts goes to L


class RulesRouter:
    name = "v1-rules"

    async def route(self, prompt: str) -> Route:
        for pattern, tier, reason in _RULES:
            if pattern.search(prompt):
                return Route(tier, f"rule: {reason}")  # type: ignore[arg-type]
        if len(_MANY_PARTS.findall(prompt)) >= MULTI_PART_MIN:
            return Route("L", "rule: long multi-part request")
        return Route("S", "rule: no sign of difficulty, small model is enough")


@register("v1-rules")
def _build(config: AppConfig) -> RulesRouter:
    return RulesRouter()
