"""The Try it page's client against a fake API (httpx.MockTransport): no network."""

import json

import httpx

from gearbox.dashboard.client import ask

URL = "http://gearbox.test"


def _ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["messages"] == [{"role": "user", "content": "What is 17 * 23?"}]
    return httpx.Response(
        200,
        json={
            "id": "gbx-1",
            "object": "chat.completion",
            "model": "anthropic/claude-haiku-5-5",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "391"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 25, "completion_tokens": 3, "total_tokens": 28},
        },
        headers={
            "x-gearbox-tier": "M",
            "x-gearbox-model": "anthropic/claude-haiku-5-5",
            "x-gearbox-cost": "0.000003",
            "x-gearbox-router": "v3-embed",
            "x-gearbox-reason": "embed: P(S)=0.21 P(M)=0.74 P(L)=0.05",
            "x-gearbox-cache": "miss",
            "x-gearbox-policy": "capped at M by x-gearbox-max-tier",
        },
    )


def test_successful_answer_reads_body_and_headers():
    r = ask(URL, "What is 17 * 23?", transport=httpx.MockTransport(_ok))
    assert r.ok and r.answer == "391"
    assert (r.tier, r.router, r.cost_gbp) == ("M", "v3-embed", 0.000003)
    assert r.reason.startswith("embed: P(S)")
    assert r.policy == "capped at M by x-gearbox-max-tier"
    assert r.fallback is None and r.cache_hit is False
    assert (r.tokens_in, r.tokens_out, r.finish_reason) == (25, 3, "stop")


def test_options_become_body_fields_and_header():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["max_tier"] = request.headers.get("x-gearbox-max-tier")
        return _ok(request)

    ask(
        URL,
        "What is 17 * 23?",
        model="L",
        max_tier="M",
        max_tokens=50,
        transport=httpx.MockTransport(handler),
    )
    assert seen["body"]["model"] == "L" and seen["body"]["max_tokens"] == 50
    assert seen["max_tier"] == "M"


def test_api_error_message_is_shown():
    def handler(request):
        return httpx.Response(503, json={"error": {"message": "no allowed tier answered"}})

    r = ask(URL, "hi", transport=httpx.MockTransport(handler))
    assert not r.ok and r.error == "HTTP 503: no allowed tier answered"


def test_api_not_running_says_how_to_start_it():
    def handler(request):
        raise httpx.ConnectError("refused")

    r = ask(URL, "hi", transport=httpx.MockTransport(handler))
    assert not r.ok and "py -m gearbox.service" in r.error
