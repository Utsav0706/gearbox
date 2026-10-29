"""Cache, policy, fallback and canary, through the API with fakes: no network, no cost."""

import os
from datetime import UTC, datetime

import fakeredis
import pytest
from fastapi.testclient import TestClient

from gearbox.config import CanaryConfig, get_config
from gearbox.routers import Route
from gearbox.service import policy
from gearbox.service.app import create_app
from gearbox.service.cache import MemoryCache, NoCache, RedisCache, cache_key
from gearbox.service.db import Base, RequestLog, make_engine, make_sessions, month_spend
from gearbox.service.pipeline import fallback_order, in_canary
from gearbox.service.schemas import ChatCompletionRequest
from gearbox.tiers import TierRequestError, TierResult, TierUnavailableError, Usage


class FakeRouter:
    def __init__(self, tier="M", name="fake-router"):
        self.tier, self.name = tier, name

    async def route(self, prompt):
        return Route(self.tier, f"{self.name}: {self.tier}")


class FakeCall:
    """Answers from every tier, except tiers listed in `down` (raise unavailable)."""

    def __init__(self, down=(), finish="stop", error_cls=TierUnavailableError):
        self.down, self.finish, self.error_cls = set(down), finish, error_cls
        self.tiers: list[str] = []

    async def __call__(self, tier, messages, *, config, max_tokens=None):
        self.tiers.append(tier)
        if tier in self.down:
            raise self.error_cls(tier, "down")
        return TierResult(
            tier=tier,
            model=config.tiers[tier].model,
            text=f"answer from {tier}",
            finish_reason=self.finish,
            usage=Usage(10, 20),
            cost_gbp=0.001,
            latency_ms=1,
        )


@pytest.fixture
def engine():
    engine = make_engine(os.environ.get("TEST_DATABASE_URL", "sqlite://"))
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _client(engine, router=None, call=None, config=None, cache=None, canary=None):
    config = config or get_config().model_copy(deep=True)
    config.fallback.retry_delay_s = 0  # don't slow the tests down
    return TestClient(
        create_app(
            config=config,
            router=router or FakeRouter(),
            engine=engine,
            call=call or FakeCall(),
            warm_up=False,
            cache=cache or NoCache(),
            canary=canary,
        )
    )


def _chat(client, content="What is 2+2?", headers=None, **body):
    payload = {"model": "auto", "messages": [{"role": "user", "content": content}], **body}
    return client.post("/v1/chat/completions", json=payload, headers=headers or {})


# --- cache --------------------------------------------------------------------------------


def _req(content, **kw):
    return ChatCompletionRequest(messages=[{"role": "user", "content": content}], **kw)


def test_cache_key_normalises_whitespace_only():
    assert cache_key(_req("What is  2+2?\n")) == cache_key(_req("What is 2+2?"))
    assert cache_key(_req("what is 2+2?")) != cache_key(_req("What is 2+2?"))  # case kept
    assert cache_key(_req("hi", model="L")) != cache_key(_req("hi"))
    assert cache_key(_req("hi", max_tokens=10)) != cache_key(_req("hi"))


def test_second_identical_request_is_free(engine):
    call = FakeCall()
    with _client(engine, call=call, cache=MemoryCache(60, 10)) as client:
        first = _chat(client)
        second = _chat(client, content="What is 2+2?  ")
        stats = client.get("/stats").json()
    assert first.headers["x-gearbox-cache"] == "miss"
    assert second.headers["x-gearbox-cache"] == "hit"
    assert second.headers["x-gearbox-cost"] == "0.000000"
    assert second.json()["choices"][0]["message"]["content"] == "answer from M"
    assert call.tiers == ["M"]  # the model was called once
    assert stats["cache_hits"] == 1 and stats["cost_gbp"] == pytest.approx(0.001)


def test_cut_off_answers_are_not_cached(engine):
    call = FakeCall(finish="length")
    with _client(engine, call=call, cache=MemoryCache(60, 10)) as client:
        _chat(client)
        assert _chat(client).headers["x-gearbox-cache"] == "miss"
    assert call.tiers == ["M", "M"]


def test_memory_cache_evicts_least_recently_used():
    c = MemoryCache(ttl_s=60, max_entries=2)
    c.set("a", {"v": 1})
    c.set("b", {"v": 2})
    c.get("a")  # a is now the most recent
    c.set("c", {"v": 3})
    assert c.get("b") is None and c.get("a") == {"v": 1} and c.get("c") == {"v": 3}


def test_redis_cache_round_trip_with_ttl():
    client = fakeredis.FakeRedis()
    c = RedisCache(client, ttl_s=120)
    c.set("k", {"text": "hi"})
    assert c.get("k") == {"text": "hi"}
    assert 0 < client.ttl("k") <= 120
    assert c.get("missing") is None


# --- policy -------------------------------------------------------------------------------


def test_policy_rules():
    assert policy.apply("L", "M", 0, None).tier == "M"
    assert policy.apply("S", "M", 0, None).notes == []  # already under the cap
    d = policy.apply("L", None, 5.0, 5.0)
    assert (d.tier, d.cap) == ("S", "S") and "monthly budget reached" in d.notes[0]
    assert policy.apply("M", None, 4.99, 5.0).tier == "M"
    assert policy.parse_max_tier(" m ") == "M"
    with pytest.raises(ValueError):
        policy.parse_max_tier("XL")


def test_max_tier_header_caps_the_route(engine):
    with _client(engine, FakeRouter("L")) as client:
        r = _chat(client, headers={"x-gearbox-max-tier": "M"})
        bad = _chat(client, headers={"x-gearbox-max-tier": "huge"})
    assert r.headers["x-gearbox-tier"] == "M"
    assert "x-gearbox-max-tier" in r.headers["x-gearbox-policy"]
    assert bad.status_code == 400 and "x-gearbox-max-tier" in bad.json()["error"]["message"]


def test_budget_reached_downgrades_to_s(engine):
    config = get_config().model_copy(deep=True)
    config.policy.monthly_budget_gbp = 0.01
    with make_sessions(engine).begin() as s:  # this month's spend is already at the cap
        s.add(
            RequestLog(
                request_id="old",
                created_at=datetime.now(UTC),
                status="ok",
                router="x",
                reason="x",
                cost_gbp=0.02,
                latency_ms=1,
                route_ms=0,
            )
        )
    with _client(engine, FakeRouter("L"), config=config) as client:
        r = _chat(client)
    assert r.headers["x-gearbox-tier"] == "S"
    assert "monthly budget reached" in r.headers["x-gearbox-policy"]


def test_month_spend_counts_only_this_month(engine):
    sessions = make_sessions(engine)
    with sessions.begin() as s:
        for rid, when, cost in [
            ("a", datetime(2026, 9, 30, 23, 59, tzinfo=UTC), 1.0),
            ("b", datetime(2026, 10, 1, 0, 0, tzinfo=UTC), 0.25),
            ("c", datetime(2026, 10, 20, tzinfo=UTC), 0.5),
        ]:
            s.add(
                RequestLog(
                    request_id=rid,
                    created_at=when,
                    status="ok",
                    router="x",
                    reason="x",
                    cost_gbp=cost,
                    latency_ms=1,
                    route_ms=0,
                )
            )
    assert month_spend(sessions, now=datetime(2026, 10, 29, tzinfo=UTC)) == pytest.approx(0.75)


# --- fallback -----------------------------------------------------------------------------


def test_fallback_order():
    assert fallback_order("S", "L") == ["S", "S", "M", "L"]
    assert fallback_order("M", "L") == ["M", "M", "L", "S"]
    assert fallback_order("L", "L") == ["L", "L", "M", "S"]
    assert fallback_order("M", "M") == ["M", "M", "S"]  # never above the cap
    assert fallback_order("S", "S") == ["S", "S"]


def test_retry_then_next_tier_up(engine):
    call = FakeCall(down={"M"})
    with _client(engine, call=call) as client:
        r = _chat(client)
        stats = client.get("/stats").json()
    assert r.status_code == 200
    assert call.tiers == ["M", "M", "L"]
    assert r.headers["x-gearbox-tier"] == "L"
    assert r.headers["x-gearbox-fallback"] == "M->M->L"
    assert stats["fallbacks"] == 1 and stats["by_tier"]["L"] == 1


def test_fallback_respects_the_cap(engine):
    call = FakeCall(down={"M"})
    with _client(engine, call=call) as client:
        r = _chat(client, headers={"x-gearbox-max-tier": "M"})
    assert call.tiers == ["M", "M", "S"]  # never L
    assert r.headers["x-gearbox-tier"] == "S"


def test_all_tiers_down_is_503_and_logged(engine):
    call = FakeCall(down={"S", "M", "L"})
    with _client(engine, call=call) as client:
        r = _chat(client)
        stats = client.get("/stats").json()
    assert r.status_code == 503
    assert "no allowed tier answered" in r.json()["error"]["message"]
    assert stats["errors"] == 1 and stats["fallbacks"] == 1


def test_rejected_request_is_not_retried_elsewhere(engine):
    call = FakeCall(down={"M"}, error_cls=TierRequestError)
    with _client(engine, call=call) as client:
        assert _chat(client).status_code == 400
    assert call.tiers == ["M"]


# --- canary -------------------------------------------------------------------------------


def test_in_canary_is_deterministic_and_close_to_percent():
    ids = [f"gbx-{i}" for i in range(5000)]
    share = sum(in_canary(i, 10) for i in ids) / len(ids)
    assert 0.08 < share < 0.12
    assert [in_canary(i, 10) for i in ids[:50]] == [in_canary(i, 10) for i in ids[:50]]


def test_canary_router_gets_its_share(engine):
    config = get_config().model_copy(deep=True)
    config.routing.canary = CanaryConfig(router="fake-canary", percent=50)
    with _client(
        engine, FakeRouter("M"), config=config, canary=FakeRouter("S", name="fake-canary")
    ) as client:
        routers = [_chat(client).headers["x-gearbox-router"] for _ in range(40)]
    assert set(routers) == {"fake-router", "fake-canary"}


class BrokenCache:
    name = "broken"

    def ping(self):
        return False

    def get(self, key):
        raise ConnectionError("redis down")

    def set(self, key, value):
        raise ConnectionError("redis down")


def test_cache_outage_does_not_fail_requests(engine):
    with _client(engine, cache=BrokenCache()) as client:
        r = _chat(client)
        health = client.get("/health")
    assert r.status_code == 200 and r.headers["x-gearbox-cache"] == "miss"
    assert health.status_code == 503
    assert health.json()["cache"] == {"backend": "broken", "ok": False}


def test_ollama_base_url_override():
    from gearbox.config import Secrets, with_env_overrides

    config = with_env_overrides(
        get_config(), Secrets(_env_file=None, ollama_base_url="http://host.docker.internal:11434")
    )
    assert config.tiers["S"].api_base == "http://host.docker.internal:11434"
    assert config.tiers["M"].api_base is None
    assert get_config().tiers["S"].api_base == "http://localhost:11434"  # original untouched
