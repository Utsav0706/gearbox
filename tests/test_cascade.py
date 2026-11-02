"""Cascade logic and experiment maths on synthetic data: no network, no cost."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gearbox.cascade import CHECK_SCHEMA, check_params, escalate, free_signal, parse_check
from gearbox.config import load_config
from gearbox.eval.cascade import CheckRow, cascade_strategy, detection, row_from_result, tune
from gearbox.eval.offline import Dataset

CONFIG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")


@pytest.mark.parametrize(
    ("text", "finish", "expected"),
    [
        ("", "stop", "empty answer"),
        ("   \n", "stop", "empty answer"),
        ("Half an ans", "length", "cut off at the token limit"),
        ("I'm not sure, but maybe 1999.", "stop", "answer hedges"),
        ("I can't verify that figure.", "stop", "answer hedges"),
        ("I don't have reliable information on that.", "stop", "answer hedges"),
        ("Canberra is the capital of Australia.", "stop", None),
        ("The court was not sure how to rule.", "stop", None),  # not the model hedging
    ],
)
def test_free_signal(text, finish, expected):
    got = free_signal(text, finish)
    assert (got is None) if expected is None else got.startswith(expected)


def test_check_params_use_config_and_schema():
    p = check_params("What is 2+2?", "4", CONFIG, "RUBRIC")
    assert p["model"] == "claude-haiku-5-5"
    assert p["system"] == "RUBRIC"
    assert p["messages"][0]["content"].endswith("<answer>\n4\n</answer>")
    assert p["output_config"]["format"]["schema"] is CHECK_SCHEMA
    assert p["output_config"]["effort"] == CONFIG.cascade.effort


def test_parse_check():
    assert parse_check('{"reason": "ok", "score": 4}').score == 4
    assert parse_check('{"reason": "x", "score": 9}') is None
    assert parse_check("not json") is None


def test_escalate_rule():
    assert escalate(None, "empty answer", 4)  # a free signal always escalates
    assert escalate(3, None, 4) and not escalate(4, None, 4)
    assert not escalate(None, None, 4)  # a failed check is no evidence either way


# --- experiment maths ---------------------------------------------------------------------


IDS = ["a", "b", "c", "d"]


def _ds():
    # M passes a and b, fails c and d; L passes everything; S fails everything.
    scores, costs = {}, {}
    for i in IDS:
        scores[(i, "S")] = 1
        scores[(i, "M")] = 5 if i in "ab" else 2
        scores[(i, "L")] = 5
        costs[(i, "S")], costs[(i, "M")], costs[(i, "L")] = 0.0, 0.01, 0.2
    return Dataset(IDS, dict.fromkeys(IDS, "M"), scores, costs)


def _checks(m_scores, s_score=1):
    rows = {}
    for i, sc in zip(IDS, m_scores, strict=True):
        rows[(i, "M")] = CheckRow(
            id=i, tier="M", score=sc, reason="r", signal=None, cost_gbp=0.0005, via="batch"
        )
        rows[(i, "S")] = CheckRow(
            id=i, tier="S", score=s_score, reason="r", signal=None, cost_gbp=0.0005, via="batch"
        )
    return rows


def test_detection_precision_recall():
    # checker gives the failing c, d low scores, but also wrongly doubts b
    checks = _checks([5, 3, 2, 2])
    d = detection(checks, _ds(), IDS, "M", min_score=4)
    assert (d["precision"], d["recall"]) == (pytest.approx(2 / 3, abs=1e-3), 1.0)
    assert d["escalation_rate"] == 0.75 and d["failing_share"] == 0.5
    best, table = tune(checks, _ds(), IDS, "M")
    assert best == 3  # 3 escalates only c and d: perfect F1
    assert [r["min_score"] for r in table] == [2, 3, 4, 5]


def test_cascade_strategy_climbs_and_counts_costs():
    ds = _ds()
    checks = _checks([5, 5, 2, 2])
    s = cascade_strategy("M->L", ds, checks, dict.fromkeys(IDS, "M"), {"M": 3, "S": 3}, 1)
    assert s.picks == {"a": "M", "b": "M", "c": "L", "d": "L"}
    # every prompt pays one check (list price = 2x batch); escalated ones also paid M
    assert s.routing_cost["a"] == pytest.approx(0.001)
    assert s.routing_cost["c"] == pytest.approx(0.001 + 0.01)

    s2 = cascade_strategy("S->M->L", ds, checks, dict.fromkeys(IDS, "S"), {"M": 3, "S": 3}, 2)
    assert s2.picks == {"a": "M", "b": "M", "c": "L", "d": "L"}  # S always fails its check
    assert s2.routing_cost["c"] == pytest.approx(0.001 + 0.0 + 0.001 + 0.01)


def test_row_from_batch_result():
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps({"reason": "ok", "score": 2}))],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        stop_reason="end_turn",
    )
    res = SimpleNamespace(
        custom_id="gsm8k-1__M", result=SimpleNamespace(type="succeeded", message=msg)
    )
    row = row_from_result(res, CONFIG, "b1")
    assert (row.id, row.tier, row.score) == ("gsm8k-1", "M", 2)
    # Haiku: (1000 * 0.10 + 200 * 0.50) / 1M USD, half price, in GBP
    assert row.cost_gbp == pytest.approx(0.0002 * 0.5 * 0.79)
    failed = SimpleNamespace(custom_id="x-1__S", result=SimpleNamespace(type="errored"))
    assert row_from_result(failed, CONFIG, "b1").score is None
