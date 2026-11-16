from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gearbox.config import get_config
from gearbox.routers import available, create_router
from gearbox.routers.pitwall import PitwallRouter


def router(handler, model: str = "production") -> PitwallRouter:
    return PitwallRouter(
        "v4-finetuned", model, "http://pitwall", 5.0, transport=httpx.MockTransport(handler)
    )


def test_asks_pitwall_for_the_named_model() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"tier": "S", "model_version": 2, "reason": "ft: P(S)=0.8", "request_id": "pw1"},
        )

    route = asyncio.run(router(handler, "candidate").route("Write a haiku"))
    assert seen == {"prompt": "Write a haiku", "model": "candidate"}
    assert route.tier == "S"
    assert route.reason == "pitwall v2: ft: P(S)=0.8; pitwall request pw1"


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(503, json={"detail": "no model"}),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"no": "tier"}),
        lambda r: httpx.Response(200, json={"tier": "XL"}),
    ],
)
def test_fails_safe_to_m(handler) -> None:
    route = asyncio.run(router(handler).route("x"))
    assert route.tier == "M" and "using M" in route.reason


def test_pitwall_down_is_m() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    route = asyncio.run(router(handler).route("x"))
    assert (route.tier, route.reason) == ("M", "pitwall unavailable (ConnectError), using M")


def test_registered_off_by_default_and_needs_config() -> None:
    assert {"v4-finetuned", "v4-candidate"} <= set(available())
    config = get_config()
    assert config.routing.pitwall is None
    with pytest.raises(ValueError, match="routing.pitwall"):
        create_router("v4-finetuned", config)
    data = config.model_dump()
    data["routing"]["pitwall"] = {"url": "http://p:8003", "timeout_s": 2}
    enabled = type(config).model_validate(data)
    assert create_router("v4-candidate", enabled).name == "v4-candidate"
