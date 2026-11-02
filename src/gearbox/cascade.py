"""Cascade: check an answer before sending it; escalate to the next tier if it fails.

Two layers, cheapest first:
1. Free signals, no model call: an empty answer, one cut off at the token limit, or one
   that says it is unsure. Haiku's real failures in the dataset were mostly the first
   kind (all tokens spent thinking, no answer).
2. A checker call (prompts/check_v1.md, structured JSON): a model reads the request and
   the answer, with no reference, and scores 1-5 how sure it is the answer is right.

Shared by the experiment (gearbox.eval.cascade) and, if it earns its place, the service.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from gearbox.config import AppConfig

CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},  # first: say what was checked, then commit to a number
        "score": {"type": "integer"},
    },
    "required": ["reason", "score"],
    "additionalProperties": False,
}

# Phrases an answer uses when it doesn't know. Matched case-insensitively.
_HEDGES = re.compile(
    r"\b(i'?m not (?:sure|certain)|i (?:can(?:no|')t|could ?n[o']t) (?:verify|confirm|find)|"
    r"i don'?t (?:know|have (?:reliable|specific|accurate) information)|"
    r"i (?:do not|don't) have (?:access|enough information)|unable to (?:verify|confirm))\b",
    re.I,
)


class CheckOutput(BaseModel):
    reason: str
    score: int = Field(ge=1, le=5)


def free_signal(text: str, finish_reason: str) -> str | None:
    """Why the answer fails without asking any model, or None if nothing obvious."""
    if not text.strip():
        return "empty answer"
    if finish_reason == "length":
        return "cut off at the token limit"
    m = _HEDGES.search(text)
    if m:
        return f"answer hedges: '{m.group(0)}'"
    return None


def check_message(prompt: str, answer: str) -> str:
    return f"<request>\n{prompt}\n</request>\n\n<answer>\n{answer}\n</answer>"


def check_params(prompt: str, answer: str, config: AppConfig, rubric: str) -> dict[str, Any]:
    """Messages API parameters for one check (used in a batch now, live later)."""
    c = config.cascade
    return {
        "model": c.model.removeprefix("anthropic/"),
        "max_tokens": c.max_tokens,
        "system": rubric,
        "messages": [{"role": "user", "content": check_message(prompt, answer)}],
        "output_config": {
            "effort": c.effort,
            "format": {"type": "json_schema", "schema": CHECK_SCHEMA},
        },
    }


def parse_check(text: str) -> CheckOutput | None:
    try:
        return CheckOutput.model_validate_json(text)
    except ValidationError:
        return None


def escalate(score: int | None, signal: str | None, min_score: int) -> bool:
    """Escalate on any free signal, or when the checker's score is below min_score.
    A missing score (the check itself failed) does not escalate: no evidence either way."""
    if signal is not None:
        return True
    return score is not None and score < min_score
