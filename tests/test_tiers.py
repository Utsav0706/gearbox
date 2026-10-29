"""call_tier with LiteLLM faked out: no network, no cost."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
from pydantic import SecretStr

from gearbox import tiers
from gearbox.config import Secrets, load_config
from gearbox.tiers import (
    TierRequestError,
    TierUnavailableError,
    Usage,
    call_tier,
    cost_gbp,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml")
WITH_KEY = Secrets(_env_file=None, anthropic_api_key=SecretStr("sk-test"))
NO_KEY = Secrets(_env_file=None)
MESSAGES = [{"role": "user", "content": "hi"}]


def _response(text="hello", finish="stop", prompt_tokens=1000, completion_tokens=2000):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


@pytest.fixture
def fake(monkeypatch):
    """Records the kwargs of each call; set .result to a response or an exception."""
    state = SimpleNamespace(calls=[], result=_response())

    async def acompletion(**kwargs):
        state.calls.append(kwargs)
        if isinstance(state.result, Exception):
            raise state.result
        return state.result

    monkeypatch.setattr(tiers.litellm, "acompletion", acompletion)
    return state


def _call(tier="M", secrets=WITH_KEY, **kw):
    return asyncio.run(call_tier(tier, MESSAGES, config=CONFIG, secrets=secrets, **kw))


def test_result_and_cost(fake):
    r = _call("L")
    assert r.text == "hello"
    assert r.model == "anthropic/claude-sonnet-5-5"
    assert r.usage == Usage(1000, 2000)
    # Sonnet: $2/M in, $10/M out -> (0.002 + 0.02) USD * 0.79
    assert r.cost_gbp == pytest.approx(0.022 * 0.79)


def test_request_uses_tier_config(fake):
    _call("M")
    kw = fake.calls[0]
    assert kw["model"] == "anthropic/claude-haiku-5-5"
    assert kw["max_tokens"] == CONFIG.tiers["M"].max_tokens
    assert kw["timeout"] == CONFIG.tiers["M"].timeout_s
    assert kw["num_retries"] == 0
    assert kw["api_key"] == "sk-test"
    assert "api_base" not in kw


def test_local_tier_gets_api_base_and_no_key(fake):
    r = _call("S", secrets=NO_KEY)
    kw = fake.calls[0]
    assert kw["api_base"] == "http://localhost:11434"
    assert "api_key" not in kw
    assert r.cost_gbp == 0


def test_tier_params_passed_through(fake):
    config = CONFIG.model_copy(deep=True)
    config.tiers["S"].params = {"think": False}
    asyncio.run(call_tier("S", MESSAGES, config=config, secrets=NO_KEY))
    assert fake.calls[0]["think"] is False


def test_max_tokens_can_lower_but_not_raise_cap(fake):
    _call(max_tokens=100)
    _call(max_tokens=999_999)
    assert fake.calls[0]["max_tokens"] == 100
    assert fake.calls[1]["max_tokens"] == CONFIG.tiers["M"].max_tokens


def test_truncation_is_returned_not_raised(fake):
    fake.result = _response(finish="length")
    assert _call().finish_reason == "length"


def test_missing_key_fails_before_calling(fake):
    with pytest.raises(TierUnavailableError, match="ANTHROPIC_API_KEY"):
        _call("M", secrets=NO_KEY)
    assert fake.calls == []


@pytest.mark.parametrize(
    "exc",
    [
        litellm.Timeout("slow", model="m", llm_provider="anthropic"),
        litellm.RateLimitError("429", llm_provider="anthropic", model="m"),
        litellm.APIConnectionError("down", llm_provider="ollama", model="m"),
        litellm.ContextWindowExceededError("too long", model="m", llm_provider="ollama"),
        litellm.NotFoundError("model not pulled", model="m", llm_provider="ollama"),
    ],
)
def test_errors_another_tier_might_survive(fake, exc):
    fake.result = exc
    with pytest.raises(TierUnavailableError) as info:
        _call()
    assert info.value.tier == "M"


def test_bad_request_is_a_request_error(fake):
    fake.result = litellm.BadRequestError("bad param", model="m", llm_provider="anthropic")
    with pytest.raises(TierRequestError):
        _call()


def test_importing_litellm_does_not_load_env_file(tmp_path, monkeypatch):
    # LiteLLM's DEV mode would copy .env into os.environ; gearbox/__init__ turns it off.
    import os
    import subprocess
    import sys

    (tmp_path / ".env").write_text("GEARBOX_LEAK_CHECK=leaked\n", encoding="utf-8")
    code = "import gearbox.tiers, os; print(os.environ.get('GEARBOX_LEAK_CHECK'))"
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_MODE"}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert out.stdout.strip() == "None", out.stderr


def test_cost_formula():
    price = CONFIG.prices_usd_per_mtok["anthropic/claude-haiku-5-5"]
    assert cost_gbp(Usage(1_000_000, 0), price, 1.0) == pytest.approx(0.10)
    assert cost_gbp(Usage(0, 1_000_000), price, 1.0) == pytest.approx(0.50)
