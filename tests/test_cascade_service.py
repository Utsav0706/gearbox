"""Quality mode (the cascade) through the API, with a fake checker: no network, no cost."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gearbox.cascade import CheckResult, run_check
from gearbox.config import get_config
from gearbox.routers import Route
from gearbox.service.app import create_app
from gearbox.service.cache import MemoryCache, NoCache
from gearbox.service.db import Base, make_engine
from gearbox.tiers import TierResult, TierUnavailableError, Usage


class Router:
    name = "fake-router"

    async def route(self, prompt):
        return Route("M", "fake")


class Call:
    def __init__(self, texts=None, down=()):
        self.texts = texts or {}
        self.down = set(down)
        self.tiers = []

    async def __call__(self, tier, messages, *, config, max_tokens=None):
        self.tiers.append(tier)
        if tier in self.down:
            raise TierUnavailableError(tier, "down")
        text = self.texts.get(tier, f"answer from {tier}")
        cost = {"S": 0.0, "M": 0.001, "L": 0.02}[tier]
        return TierResult(tier, config.tiers[tier].model, text, "stop", Usage(10, 20), cost, 1)


class Check:
    def __init__(self, score=5):
        self.score = score
        self.calls = []

    async def __call__(self, prompt, answer):
        self.calls.append((prompt, answer))
        return CheckResult(self.score, "checked the arithmetic", 0.0002)


@pytest.fixture
def engine():
    engine = make_engine(os.environ.get("TEST_DATABASE_URL", "sqlite://"))
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _client(engine, call=None, check=None, cache=None, enabled=False):
    config = get_config().model_copy(deep=True)
    config.fallback.retry_delay_s = 0
    config.cascade.enabled = enabled
    return TestClient(
        create_app(
            config=config,
            router=Router(),
            engine=engine,
            call=call or Call(),
            warm_up=False,
            cache=cache or NoCache(),
            check=check or Check(),
        )
    )


def _chat(client, headers=None, **body):
    payload = {"messages": [{"role": "user", "content": "What is 17 * 23?"}], **body}
    return client.post("/v1/chat/completions", json=payload, headers=headers or {})


ON = {"x-gearbox-cascade": "on"}


def test_off_by_default_no_check(engine):
    check = Check()
    with _client(engine, check=check) as c:
        r = _chat(c)
    assert r.headers["x-gearbox-tier"] == "M"
    assert "x-gearbox-cascade" not in r.headers
    assert check.calls == []


def test_passing_check_keeps_the_answer_and_adds_its_cost(engine):
    check = Check(score=5)
    with _client(engine, check=check) as c:
        r = _chat(c, headers=ON)
    assert r.headers["x-gearbox-tier"] == "M"
    assert r.headers["x-gearbox-cascade"] == "M passed check (5/5: checked the arithmetic)"
    assert float(r.headers["x-gearbox-cost"]) == pytest.approx(0.001 + 0.0002)
    assert check.calls == [("What is 17 * 23?", "answer from M")]


def test_failing_check_escalates_and_is_logged(engine):
    call = Call()
    with _client(engine, call=call, check=Check(score=2)) as c:
        r = _chat(c, headers=ON)
        stats = c.get("/stats").json()
    assert r.headers["x-gearbox-tier"] == "L"
    assert r.json()["choices"][0]["message"]["content"] == "answer from L"
    assert r.headers["x-gearbox-cascade"].startswith("M->L (2/5")
    assert float(r.headers["x-gearbox-cost"]) == pytest.approx(0.001 + 0.0002 + 0.02)
    assert call.tiers == ["M", "L"]
    assert stats["escalations"] == 1 and stats["by_tier"]["L"] == 1
    assert "cascade: M->L" in r.headers["x-gearbox-reason"]


def test_free_signal_escalates_without_a_check(engine):
    check = Check(score=5)
    with _client(engine, call=Call(texts={"M": "  "}), check=check) as c:
        r = _chat(c, headers=ON)
    assert r.headers["x-gearbox-tier"] == "L"
    assert r.headers["x-gearbox-cascade"] == "M->L (empty answer)"
    assert check.calls == []


def test_cap_stops_the_cascade(engine):
    check = Check(score=1)
    with _client(engine, check=check) as c:
        r = _chat(c, headers={**ON, "x-gearbox-max-tier": "M"})
    assert r.headers["x-gearbox-tier"] == "M"
    assert r.headers["x-gearbox-cascade"] == "M not checked (no higher tier allowed)"
    assert check.calls == []


def test_escalation_target_down_keeps_the_first_answer(engine):
    with _client(engine, call=Call(down={"L"}), check=Check(score=1)) as c:
        r = _chat(c, headers=ON)
    assert r.status_code == 200 and r.headers["x-gearbox-tier"] == "M"
    assert "L unavailable, kept M" in r.headers["x-gearbox-cascade"]


def test_config_default_and_header_opt_out(engine):
    check = Check(score=5)
    with _client(engine, check=check, enabled=True) as c:
        on = _chat(c)
        off = _chat(c, headers={"x-gearbox-cascade": "off"})
        bad = _chat(c, headers={"x-gearbox-cascade": "maybe"})
    assert "x-gearbox-cascade" in on.headers
    assert "x-gearbox-cascade" not in off.headers
    assert bad.status_code == 400 and "on or off" in bad.json()["error"]["message"]
    assert len(check.calls) == 1


def test_quality_and_normal_answers_are_cached_separately(engine):
    call = Call()
    with _client(engine, call=call, check=Check(score=5), cache=MemoryCache(60, 10)) as c:
        _chat(c)
        q = _chat(c, headers=ON)
        q2 = _chat(c, headers=ON)
    assert q.headers["x-gearbox-cache"] == "miss"
    assert q2.headers["x-gearbox-cache"] == "hit"
    assert call.tiers == ["M", "M"]


# --- the live checker, with a fake Anthropic client ---------------------------------------


class FakeMessages:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.kwargs = reply, error, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.reply


def _msg(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
        stop_reason=stop,
    )


def test_run_check_parses_and_prices():
    messages = FakeMessages(_msg(json.dumps({"reason": "ok", "score": 3})))
    r = asyncio.run(run_check("q", "a", get_config(), "k", SimpleNamespace(messages=messages)))
    assert (r.score, r.reason) == (3, "ok")
    # Haiku list price: (1000 * 0.10 + 100 * 0.50) / 1M USD, in GBP (not halved: live)
    assert r.cost_gbp == pytest.approx(0.00015 * 0.79)
    assert messages.kwargs["output_config"]["format"]["type"] == "json_schema"


def test_run_check_failures_never_escalate():
    broken = FakeMessages(error=RuntimeError("down"))
    r = asyncio.run(run_check("q", "a", get_config(), "k", SimpleNamespace(messages=broken)))
    assert r.score is None and r.cost_gbp == 0.0
    cut = FakeMessages(_msg("{", stop="max_tokens"))
    r2 = asyncio.run(run_check("q", "a", get_config(), "k", SimpleNamespace(messages=cut)))
    assert r2.score is None and r2.cost_gbp > 0
