"""The dashboard's pages in Streamlit's AppTest (no browser), with the API faked."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from gearbox.dashboard import client
from gearbox.dashboard.client import TryResult

DASH = Path(__file__).resolve().parents[1] / "src/gearbox/dashboard"


@pytest.fixture
def fake_api(monkeypatch):
    calls = []

    def ask(base_url, prompt, model="auto", max_tier=None, max_tokens=None, transport=None):
        calls.append(
            {"prompt": prompt, "model": model, "max_tier": max_tier, "max_tokens": max_tokens}
        )
        if prompt == "break":
            return TryResult(ok=False, error="HTTP 503: no allowed tier answered")
        return TryResult(
            ok=True,
            answer="**391** <script>alert(1)</script>",
            tier="M",
            model="anthropic/claude-haiku-5-5",
            router="v3-embed",
            reason="embed: P(S)=0.21 P(M)=0.74 P(L)=0.05",
            cost_gbp=0.000003,
            seconds=1.2,
            tokens_in=25,
            tokens_out=3,
            finish_reason="stop",
            policy="capped at M by x-gearbox-max-tier",
        )

    monkeypatch.setattr(client, "ask", ask)
    return calls


def _send(at, prompt, model=None, max_tier=None):
    at.text_area[0].input(prompt)
    if model:
        at.radio[0].set_value(model)
    if max_tier:
        at.selectbox[0].set_value(max_tier)
    return at.button[0].click().run()


def test_try_page_shows_answer_and_routing(fake_api):
    at = AppTest.from_file(str(DASH / "try_it.py"), default_timeout=30).run()
    assert at.title[0].value == "Try Gearbox"
    at = _send(at, "What is 17 * 23?", model="L (Sonnet)", max_tier="M")
    assert not at.exception
    assert fake_api == [
        {"prompt": "What is 17 * 23?", "model": "L", "max_tier": "M", "max_tokens": 1000}
    ]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {"Tier": "M", "Cost": "£0.000003", "Time": "1.2 s", "Cache": "miss"}
    assert at.code[0].value.startswith("embed: P(S)")
    assert any("capped at M" in i.value for i in at.info)
    # the answer goes through st.markdown, which never renders raw HTML
    assert "**391**" in at.markdown[0].value


def test_try_page_shows_errors_and_history(fake_api):
    at = AppTest.from_file(str(DASH / "try_it.py"), default_timeout=30).run()
    at = _send(at, "break")
    assert at.error[0].value == "HTTP 503: no allowed tier answered"
    at = _send(at, "What is 17 * 23?")
    rows = at.dataframe[0].value
    assert list(rows["tier"]) == ["M", "error"]  # newest first


def test_empty_prompt_is_not_sent(fake_api):
    at = AppTest.from_file(str(DASH / "try_it.py"), default_timeout=30).run()
    at = _send(at, "   ")
    assert fake_api == []
    assert "Type a prompt" in at.warning[0].value
