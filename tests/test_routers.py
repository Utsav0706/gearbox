"""Router registry, v1 rules and v2 parsing: no network."""

import asyncio

import pytest

from gearbox.routers import Route, available, create_router, register
from gearbox.routers.llm import LLMRouter, parse
from gearbox.routers.rules import RulesRouter
from gearbox.tiers import TierUnavailableError


def _route(router, prompt):
    return asyncio.run(router.route(prompt))


def test_builtin_routers_registered():
    assert {"v1-rules", "v2-llm", "v3-embed"} <= set(available())
    assert isinstance(create_router("v1-rules"), RulesRouter)
    with pytest.raises(KeyError, match="unknown router"):
        create_router("v9-nope")


def test_register_new_router_by_name():
    # How Pitwall's "v4" plugs in: register a factory under a new name.
    class Always:
        name = "test-always-l"

        async def route(self, prompt):
            return Route("L", "test")

    register("test-always-l")(lambda config: Always())
    assert _route(create_router("test-always-l"), "hi").tier == "L"
    with pytest.raises(ValueError, match="already registered"):
        register("test-always-l")(lambda config: Always())


@pytest.mark.parametrize(
    ("prompt", "tier"),
    [
        ("Prove that the square root of 2 is irrational.", "L"),
        ("Implement an LRU cache with O(1) get and put.", "M"),
        ("What is the probability of rolling two sixes?", "M"),
        ("Who wrote Pride and Prejudice?", "M"),
        ("Is it true that we only use 10% of our brains?", "M"),
        ("Write a short poem about the rain.", "S"),
        ("Rewrite politely: send me the report now.", "S"),
        ("Plan my trip:\n1. flights\n2. hotel\n3. food\n4. budget\n", "L"),
    ],
)
def test_rules(prompt, tier):
    route = _route(RulesRouter(), prompt)
    assert route.tier == tier
    assert route.reason.startswith("rule: ")


@pytest.mark.parametrize(
    ("text", "tier"),
    [
        ('{"reason": "simple fact", "tier": "M"}', "M"),
        ('Sure!\n```json\n{"reason": "easy", "tier": "S"}\n```', "S"),
        ('{"tier": "L"}', "L"),
    ],
)
def test_llm_parse(text, tier):
    assert parse(text).tier == tier


@pytest.mark.parametrize("text", ["", "S", '{"tier": "XL"}', "{not json}", '["S"]'])
def test_llm_parse_rejects_bad_output(text):
    assert parse(text) is None


def test_llm_router_fails_safe_to_m(monkeypatch):
    from gearbox.config import get_config
    from gearbox.routers import llm

    async def broken(*args, **kwargs):
        raise TierUnavailableError("M", "down")

    monkeypatch.setattr(llm, "call_tier", broken)
    route = _route(LLMRouter(get_config(), "M", "system"), "hi")
    assert route.tier == "M" and "failed" in route.reason
