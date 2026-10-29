"""The API end to end with a fake router and fake model calls: no network, no cost.

The database is in-memory SQLite locally and real Postgres in CI (TEST_DATABASE_URL).
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gearbox.config import get_config
from gearbox.routers import Route
from gearbox.service.app import create_app
from gearbox.service.db import Base, make_engine
from gearbox.tiers import TierRequestError, TierResult, TierUnavailableError, Usage

ROOT = Path(__file__).resolve().parents[1]


class FakeRouter:
    name = "fake-router"

    def __init__(self, tier="M"):
        self.tier = tier
        self.seen: list[str] = []

    async def route(self, prompt):
        self.seen.append(prompt)
        return Route(self.tier, "fake: always " + self.tier)


class FakeCall:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, tier, messages, *, config, max_tokens=None):
        self.calls.append({"tier": tier, "messages": messages, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        return TierResult(
            tier=tier,
            model=config.tiers[tier].model,
            text=f"answer from {tier}",
            finish_reason="stop",
            usage=Usage(12, 34),
            cost_gbp=0.000123,
            latency_ms=5,
        )


@pytest.fixture
def engine():
    engine = make_engine(os.environ.get("TEST_DATABASE_URL", "sqlite://"))
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)  # CI shares one Postgres database between tests
    engine.dispose()


def _client(engine, router=None, call=None):
    app = create_app(
        config=get_config(),
        router=router or FakeRouter(),
        engine=engine,
        call=call or FakeCall(),
        warm_up=False,
    )
    return TestClient(app)


def _chat(client, **body):
    payload = {"model": "auto", "messages": [{"role": "user", "content": "What is 2+2?"}]}
    return client.post("/v1/chat/completions", json={**payload, **body})


def test_completion_shape_and_headers(engine):
    with _client(engine) as client:
        r = _chat(client)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "anthropic/claude-haiku-5-5"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "answer from M"}
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}
    assert r.headers["x-gearbox-tier"] == "M"
    assert r.headers["x-gearbox-model"] == "anthropic/claude-haiku-5-5"
    assert r.headers["x-gearbox-cost"] == "0.000123"
    assert r.headers["x-gearbox-router"] == "fake-router"
    assert r.headers["x-request-id"] == body["id"]


def test_router_reads_last_user_message_and_tier_gets_all(engine):
    router, call = FakeRouter("S"), FakeCall()
    messages = [
        {"role": "developer", "content": "Be brief."},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": [{"type": "text", "text": "follow-up"}]},
    ]
    with _client(engine, router, call) as client:
        assert _chat(client, messages=messages, max_tokens=50).status_code == 200
    assert router.seen == ["follow-up"]
    sent = call.calls[0]
    assert sent["tier"] == "S" and sent["max_tokens"] == 50
    assert sent["messages"][0] == {"role": "system", "content": "Be brief."}
    assert sent["messages"][-1] == {"role": "user", "content": "follow-up"}


def test_pinned_tier_skips_the_router(engine):
    router = FakeRouter("S")
    with _client(engine, router) as client:
        r = _chat(client, model="L")
    assert r.headers["x-gearbox-tier"] == "L"
    assert r.headers["x-gearbox-router"] == "pinned"
    assert router.seen == []


def test_sampling_settings_noted_not_forwarded(engine):
    with _client(engine) as client:
        r = _chat(client, temperature=0.2)
    assert r.status_code == 200
    assert "not forwarded" in r.headers["x-gearbox-note"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"stream": True}, "stream"),
        ({"n": 2}, "n must be 1"),
        ({"tools": [{"type": "function"}]}, "tools"),
        ({"model": "gpt-4o"}, "model must be"),
        ({"messages": []}, "at least 1"),
        (
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
            "only text",
        ),
    ],
)
def test_unsupported_requests_get_openai_style_400(engine, body, message):
    with _client(engine) as client:
        r = _chat(client, **body)
    assert r.status_code == 400
    assert message in r.json()["error"]["message"]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (TierUnavailableError("M", "down"), 503),
        (TierRequestError("M", "bad"), 400),
    ],
)
def test_tier_errors_map_to_http_and_are_logged(engine, error, status):
    with _client(engine, call=FakeCall(error)) as client:
        r = _chat(client)
        assert r.status_code == status
        assert "x-request-id" in r.headers
        stats = client.get("/stats").json()
    assert stats["requests"] == 1 and stats["errors"] == 1


def test_stats_counts_cost_and_route_split(engine):
    with _client(engine, FakeRouter("S")) as client:
        for _ in range(3):
            _chat(client)
        _chat(client, model="L")
        stats = client.get("/stats?hours=1").json()
    assert stats["requests"] == 4
    assert stats["by_tier"] == {"S": 3, "M": 0, "L": 1}
    assert stats["by_router"] == {"fake-router": 3, "pinned": 1}
    assert stats["cost_gbp"] == pytest.approx(4 * 0.000123)


def test_health(engine):
    with _client(engine) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] is True
    assert body["router"] == "fake-router"
    assert set(body["tiers"]) == {"S", "M", "L"}


def test_migrations_match_models(tmp_path):
    """alembic upgrade head builds the same schema the models describe (no drift)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'm.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    try:
        command.check(cfg)  # raises if the models need a migration that doesn't exist
    finally:
        command.downgrade(cfg, "base")
