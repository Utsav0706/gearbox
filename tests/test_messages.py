"""POST /v1/messages against a fake Anthropic upstream: no network, no cost."""

import asyncio
import json
import os

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from gearbox.config import Secrets, get_config
from gearbox.routers import Route
from gearbox.service.app import create_app
from gearbox.service.cache import NoCache
from gearbox.service.db import Base, make_engine

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


class Router:
    name = "fake-router"

    def __init__(self, tier="L"):
        self.tier = tier
        self.seen = []

    async def route(self, prompt):
        self.seen.append(prompt)
        return Route(self.tier, f"fake: {self.tier}")


class Upstream:
    """A fake Anthropic API: records requests; replies with `statuses` in turn."""

    def __init__(self, statuses=(200,), text="hello", stop="end_turn"):
        self.statuses = list(statuses)
        self.text, self.stop = text, stop
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "type": "error",
                    "error": {
                        "type": "overloaded_error" if status == 529 else "invalid_request_error",
                        "message": f"upstream said {status}",
                    },
                },
            )
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": self.text}],
                "stop_reason": self.stop,
                "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            },
        )

    def sent(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


@pytest.fixture
def engine():
    engine = make_engine(os.environ.get("TEST_DATABASE_URL", "sqlite://"))
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _app(engine, upstream, router=None, key="sk-gearbox"):
    config = get_config().model_copy(deep=True)
    config.fallback.retry_delay_s = 0
    secrets = Secrets(
        _env_file=None,
        anthropic_api_key=SecretStr(key) if key else None,
        anthropic_base_url="https://anthropic.test",
    )
    return create_app(
        config=config,
        secrets=secrets,
        router=router or Router(),
        engine=engine,
        warm_up=False,
        cache=NoCache(),
        upstream=httpx.MockTransport(upstream),
    )


def _post(client, model=SONNET, headers=None, beta=False, **extra):
    body = {
        "model": model,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Summarise this."}],
        **extra,
    }
    url = "/v1/messages" + ("?beta=true" if beta else "")
    return client.post(url, json=body, headers=headers or {})


def test_task_type_picks_haiku_and_strips_the_sonnet_only_fallback(engine):
    up = Upstream()
    with TestClient(_app(engine, up)) as c:
        r = _post(
            c,
            beta=True,
            fallbacks="default",
            output_config={"effort": "low"},
            tools=[{"name": "t", "input_schema": {"type": "object"}}],
            headers={
                "x-scout-task-type": "extract",
                "x-api-key": "callers-key",
                "anthropic-beta": "server-side-fallback-2026-07-01,other-beta",
            },
        )
    assert r.status_code == 200
    assert r.json()["model"] == HAIKU
    req = up.requests[0]
    assert str(req.url) == "https://anthropic.test/v1/messages?beta=true"
    assert req.headers["x-api-key"] == "sk-gearbox"  # Gearbox's key, never the caller's
    assert req.headers["anthropic-beta"] == "other-beta"
    sent = up.sent()
    assert sent["model"] == HAIKU and "fallbacks" not in sent
    assert sent["output_config"] == {"effort": "low"} and sent["tools"][0]["name"] == "t"
    assert r.headers["x-gearbox-tier"] == "M" and r.headers["x-gearbox-router"] == "task-type"
    # Haiku: (1000 * 0.10 + 200 * 0.50) / 1M USD, in GBP
    assert float(r.headers["x-gearbox-cost"]) == pytest.approx(0.0002 * 0.79)


def test_write_stays_on_sonnet_with_its_fallback(engine):
    up = Upstream()
    with TestClient(_app(engine, up)) as c:
        _post(
            c,
            fallbacks="default",
            headers={"x-scout-task-type": "write", "anthropic-beta": "server-side-fallback-x"},
        )
    assert up.sent()["model"] == SONNET and up.sent()["fallbacks"] == "default"
    assert up.requests[0].headers["anthropic-beta"] == "server-side-fallback-x"


def test_requested_model_is_the_ceiling(engine):
    up = Upstream()
    with TestClient(_app(engine, up)) as c:
        r = _post(c, model=HAIKU, headers={"x-scout-task-type": "write"})
    assert up.sent()["model"] == HAIKU
    assert "capped at M" in r.headers["x-gearbox-reason"]


def test_untagged_requests_use_the_router(engine):
    router, up = Router("M"), Upstream()
    with TestClient(_app(engine, up, router)) as c:
        r = _post(c)
    assert router.seen == ["Summarise this."]
    assert up.sent()["model"] == HAIKU and r.headers["x-gearbox-router"] == "fake-router"


def test_router_choosing_s_is_lifted_to_the_cheapest_anthropic_tier(engine):
    up = Upstream()
    with TestClient(_app(engine, up, Router("S"))) as c:
        r = _post(c)
    assert up.sent()["model"] == HAIKU
    assert "not served on /v1/messages" in r.headers["x-gearbox-policy"]


def test_overloaded_retries_then_falls_back(engine):
    up = Upstream(statuses=[529, 529, 200])
    with TestClient(_app(engine, up)) as c:
        r = _post(c, headers={"x-scout-task-type": "write"})
        stats = c.get("/stats").json()
    assert r.status_code == 200
    assert [json.loads(q.content)["model"] for q in up.requests] == [SONNET, SONNET, HAIKU]
    assert r.headers["x-gearbox-fallback"] == "L->L->M"
    assert stats["fallbacks"] == 1


def test_anthropic_errors_pass_through_unchanged(engine):
    up = Upstream(statuses=[400])
    with TestClient(_app(engine, up)) as c:
        r = _post(c)
        stats = c.get("/stats").json()
    assert r.status_code == 400 and r.json()["error"]["message"] == "upstream said 400"
    assert len(up.requests) == 1  # a bad request is not retried
    assert stats["errors"] == 1


def test_unreachable_upstream_is_503(engine):
    def down(request):
        raise httpx.ConnectError("no route")

    with TestClient(_app(engine, down)) as c:
        r = _post(c)
    assert r.status_code == 503 and r.json()["type"] == "error"


@pytest.mark.parametrize(
    ("kwargs", "status", "message"),
    [
        ({"model": "claude-opus-5-5"}, 400, "not served by Gearbox"),
        ({"stream": True}, 400, "stream=true"),
        ({"headers": {"x-gearbox-max-tier": "S"}}, 503, "no model allowed"),
    ],
)
def test_requests_gearbox_refuses(engine, kwargs, status, message):
    with TestClient(_app(engine, Upstream())) as c:
        r = _post(c, **kwargs)
    assert r.status_code == status and message in r.json()["error"]["message"]


def test_no_key_configured(engine):
    with TestClient(_app(engine, Upstream(), key=None)) as c:
        r = _post(c)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["error"]["message"]


def test_the_real_anthropic_sdk_works_through_gearbox(engine):
    """Scout's own client calls (beta.messages.create and .parse with a Pydantic
    output_format), pointed at Gearbox, parse Gearbox's replies."""
    from anthropic import AsyncAnthropic

    class Plan(BaseModel):
        steps: list[str]

    up = Upstream(text=json.dumps({"steps": ["search", "read"]}))
    app = _app(engine, up)

    async def run():
        # Anthropic's SDK 1.x runs on httpx2 (and refuses httpx objects).
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gearbox") as http:
            sdk = AsyncAnthropic(api_key="unused", base_url="http://gearbox", http_client=http)
            msg = await sdk.beta.messages.create(
                model=SONNET,
                max_tokens=50,
                messages=[{"role": "user", "content": "hi"}],
                extra_headers={"x-scout-task-type": "extract"},
            )
            parsed = await sdk.beta.messages.parse(
                model=SONNET,
                max_tokens=50,
                output_format=Plan,
                messages=[{"role": "user", "content": "plan"}],
                extra_headers={"x-scout-task-type": "plan"},
            )
            return msg, parsed

    with TestClient(app):  # runs startup (lifespan) for the app
        msg, parsed = asyncio.run(run())
    assert msg.model == HAIKU and msg.content[0].text.startswith("{")
    assert parsed.parsed_output == Plan(steps=["search", "read"])
