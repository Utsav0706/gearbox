"""Tracing with an in-memory exporter: what a request's trace contains. No network."""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from gearbox.config import Secrets, get_config
from gearbox.routers import Route
from gearbox.service.app import create_app
from gearbox.service.cache import NoCache
from gearbox.service.db import Base, make_engine
from gearbox.telemetry import langfuse_settings, make_provider
from gearbox.tiers import TierResult, TierUnavailableError, Usage

EXPORTER = InMemorySpanExporter()
# The global provider can be set once per process; every test here shares it.
trace.set_tracer_provider(make_provider(EXPORTER, SimpleSpanProcessor))


class Router:
    name = "fake-router"

    async def route(self, prompt):
        return Route("M", "fake")


class Call:
    def __init__(self, down=()):
        self.down = set(down)

    async def __call__(self, tier, messages, *, config, max_tokens=None):
        if tier in self.down:
            raise TierUnavailableError(tier, "down")
        return TierResult(tier, config.tiers[tier].model, "hi", "stop", Usage(10, 20), 0.79, 1)


@pytest.fixture
def client():
    EXPORTER.clear()
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    config = get_config().model_copy(deep=True)
    config.fallback.retry_delay_s = 0

    def make(down=()):
        app = create_app(
            config=config,
            router=Router(),
            engine=engine,
            call=Call(down),
            warm_up=False,
            cache=NoCache(),
        )
        return TestClient(app)

    yield make
    engine.dispose()


def _chat(c):
    return c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}]})


def test_request_trace_has_root_and_generation(client):
    with client() as c:
        assert _chat(c).status_code == 200
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    root, gen = spans["gearbox.request"], spans["call M"]
    assert gen.parent.span_id == root.context.span_id
    assert root.attributes["gearbox.tier"] == "M"
    assert root.attributes["gearbox.router"] == "fake-router"
    assert root.attributes["gearbox.cost_gbp"] == pytest.approx(0.79)
    assert gen.attributes["langfuse.observation.type"] == "generation"
    assert gen.attributes["gen_ai.usage.output_tokens"] == 20
    # GBP 0.79 at usd_to_gbp 0.79 is USD 1.00
    assert json.loads(gen.attributes["langfuse.observation.cost_details"]) == {"total": 1.0}
    # metadata only: the prompt and the answer never go into a span
    assert all(v != "q" and v != "hi" for s in spans.values() for v in s.attributes.values())


def test_fallback_shows_every_attempt(client):
    with client(down={"M"}) as c:
        assert _chat(c).status_code == 200
    names = [s.name for s in EXPORTER.get_finished_spans()]
    assert names.count("call M") == 2 and names.count("call L") == 1
    root = next(s for s in EXPORTER.get_finished_spans() if s.name == "gearbox.request")
    assert root.attributes["gearbox.fallback"] == "M->M->L"


def test_failed_request_marks_the_trace(client):
    with client(down={"S", "M", "L"}) as c:
        assert _chat(c).status_code == 503
    root = next(s for s in EXPORTER.get_finished_spans() if s.name == "gearbox.request")
    assert root.attributes["gearbox.status"] == "error"
    assert not root.status.is_ok


def test_other_libraries_spans_are_not_exported():
    EXPORTER.clear()
    with trace.get_tracer("some.library").start_as_current_span("noise"):
        pass
    assert EXPORTER.get_finished_spans() == ()


def test_langfuse_settings():
    assert langfuse_settings(Secrets(_env_file=None)) is None
    secrets = Secrets(
        _env_file=None,
        langfuse_public_key=SecretStr("pk"),
        langfuse_secret_key=SecretStr("sk"),
        langfuse_host="https://cloud.langfuse.com/",
    )
    endpoint, headers = langfuse_settings(secrets)
    assert endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pk:sk").decode()
