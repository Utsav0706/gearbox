"""The OpenAI chat-completions request and response shapes Gearbox accepts and returns.

Only the subset Gearbox can honour is accepted. Anything that would silently change
the meaning of a request is rejected with a clear 400 instead of being ignored:
streaming, tools/function calling, n > 1, and non-text content (spec: out of scope).
Sampling settings (temperature, top_p) are accepted but not forwarded: the Claude tiers
have rejected non-default sampling values since 2026-10-21, so passing them on would
turn a harmless setting into a failed request. The response says so in x-gearbox-note.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# model = "auto" lets the router decide; a tier name pins the request to that tier.
AUTO_MODELS = {"auto", "gearbox/auto", "gearbox"}
PINNED_MODELS = {"S": "S", "M": "M", "L": "L", "gearbox/S": "S", "gearbox/M": "M", "gearbox/L": "L"}


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[ContentPart]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if any(p.type != "text" for p in self.content):
            raise ValueError("only text content is supported (no images or files)")
        return "".join(p.text or "" for p in self.content)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # unknown OpenAI fields (user, seed...) ignored

    model: str = "auto"
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False
    n: int = 1
    temperature: float | None = None
    top_p: float | None = None

    @model_validator(mode="after")
    def supported(self) -> ChatCompletionRequest:
        if self.stream:
            raise ValueError("stream=true is not supported")
        if self.n != 1:
            raise ValueError("n must be 1")
        extra = self.model_extra or {}
        for field in ("tools", "functions", "tool_choice", "function_call"):
            if extra.get(field):
                raise ValueError(f"{field} is not supported (no tool calling)")
        if self.model not in AUTO_MODELS and self.model not in PINNED_MODELS:
            raise ValueError(f"model must be 'auto' or a tier (S, M, L), got {self.model!r}")
        for m in self.messages:
            m.text()  # raises for non-text content
        return self

    @property
    def pinned_tier(self) -> str | None:
        return PINNED_MODELS.get(self.model)

    @property
    def token_limit(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens

    def plain_messages(self) -> list[dict[str, str]]:
        # "developer" is OpenAI's newer name for system instructions.
        return [
            {"role": "system" if m.role == "developer" else m.role, "content": m.text()}
            for m in self.messages
        ]

    def routing_text(self) -> str:
        """What the router reads: the last user message (the actual request)."""
        for m in reversed(self.messages):
            if m.role == "user":
                return m.text()
        return self.messages[-1].text()


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str  # the model that actually answered, e.g. anthropic/claude-haiku-5-5
    choices: list[Choice]
    usage: Usage


def error_body(message: str, type_: str, code: str) -> dict[str, Any]:
    """OpenAI-style error JSON, so OpenAI client libraries surface the message."""
    return {"error": {"message": message, "type": type_, "code": code}}
