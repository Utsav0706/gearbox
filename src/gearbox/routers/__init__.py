"""Routers: every router answers one question, "which tier should answer this prompt?"

All routers share one interface, `await router.route(prompt) -> Route(tier, reason)`,
and register under a name. The service picks one by name from config.yaml, so a new
router (Pitwall's fine-tuned "v4") plugs in without changing the service.

    from gearbox.routers import create_router
    router = create_router("v3-embed")
    route = await router.route("What is 17 * 23?")   # Route(tier="S", reason="...")

async because v2 calls a model; the others simply return at once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from gearbox.config import AppConfig, Tier, get_config


@dataclass(frozen=True)
class Route:
    tier: Tier
    reason: str  # one line, logged with every request so each route is explainable


class Router(Protocol):
    name: str

    async def route(self, prompt: str) -> Route: ...


Factory = Callable[[AppConfig], Router]
_REGISTRY: dict[str, Factory] = {}


def register(name: str) -> Callable[[Factory], Factory]:
    """Decorator: @register("v1-rules") on a function that builds the router."""

    def wrap(factory: Factory) -> Factory:
        if name in _REGISTRY:
            raise ValueError(f"router {name!r} is already registered")
        _REGISTRY[name] = factory
        return factory

    return wrap


def available() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def create_router(name: str, config: AppConfig | None = None) -> Router:
    _load_builtins()
    if name not in _REGISTRY:
        raise KeyError(f"unknown router {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](config or get_config())


def _load_builtins() -> None:
    # Imported on first use: importing the package must not load torch (v3) for
    # callers that only want the rules router.
    from gearbox.routers import embed, llm, rules  # noqa: F401
