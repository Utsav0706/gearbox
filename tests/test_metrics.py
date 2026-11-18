from __future__ import annotations

import re

from gearbox.tiers import TierRequestError
from test_service import FakeCall, FakeRouter, _chat, _client, engine  # noqa: F401  (fixture)


def value(text: str, name: str, **labels: str) -> float:
    """The sample value of `name` with exactly these labels (any order)."""
    for line in text.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        found = dict(re.findall(r'(\w+)="([^"]*)"', line.split(" ")[0]))
        if found == labels:
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{name}{labels} not in metrics")


def test_requests_cost_tokens_and_latency_are_counted(engine) -> None:  # noqa: F811
    with _client(engine) as client:
        _chat(client)
        _chat(client, messages=[{"role": "user", "content": "And 3+3?"}])
        text = client.get("/metrics/").text
    labels = {
        "router": "fake-router",
        "tier": "M",
        "status": "ok",
        "cache_hit": "false",
        "fallback": "false",
        "escalated": "false",
    }
    assert value(text, "gearbox_requests_total", **labels) == 2
    assert (
        value(text, "gearbox_cost_gbp_total", tier="M", model="anthropic/claude-haiku-5-5")
        == 2 * 0.000123
    )
    assert value(text, "gearbox_tokens_total", tier="M", direction="in") == 24
    assert value(text, "gearbox_tokens_total", tier="M", direction="out") == 68
    assert value(text, "gearbox_request_latency_seconds_count", tier="M") == 2
    assert value(text, "gearbox_route_latency_seconds_count", router="fake-router") == 2


def test_failures_are_counted_too(engine) -> None:  # noqa: F811
    with _client(engine, call=FakeCall(TierRequestError("M", "bad request"))) as client:
        r = _chat(client)
        text = client.get("/metrics/").text
    assert r.status_code == 400
    error_rows = [
        line
        for line in text.splitlines()
        if line.startswith("gearbox_requests_total{") and 'status="error"' in line
    ]
    assert len(error_rows) == 1 and error_rows[0].endswith(" 1.0")


def test_month_spend_and_budget(engine) -> None:  # noqa: F811
    with _client(engine) as client:
        _chat(client)
        text = client.get("/metrics/").text
    assert value(text, "gearbox_month_spend_gbp") == 0.000123
    assert value(text, "gearbox_monthly_budget_gbp") == 5.0


def test_canary_split_shows_as_routers(engine) -> None:  # noqa: F811
    from gearbox.config import get_config

    config = get_config()
    data = config.model_dump()
    data["routing"]["canary"] = {"router": "v1-rules", "percent": 100}
    canary_config = type(config).model_validate(data)
    canary = FakeRouter("S")
    canary.name = "the-canary"
    with _client(engine, config=canary_config, canary=canary) as client:
        _chat(client)
        text = client.get("/metrics/").text
    assert 'router="the-canary"' in text  # all traffic went to the canary (100%)
    assert 'router="fake-router"' not in text.split("gearbox_requests_total")[1].split("# HELP")[0]
