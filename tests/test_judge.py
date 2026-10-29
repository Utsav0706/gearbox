"""Judge runner with fake batch results: no network, no cost."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gearbox.config import load_config
from gearbox.data.answer import AnswerRow
from gearbox.data.judge import (
    JUDGE_SCHEMA,
    JudgeBatch,
    Judgment,
    custom_id,
    judge_request,
    judgment_from_result,
    load_judgments,
    rule_judgment,
    save_judgments,
    split_custom_id,
    todo,
    user_message,
)
from gearbox.data.schema import PromptItem

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml")


def _item(reference=None, kind=None) -> PromptItem:
    return PromptItem(
        id="gsm8k-1",
        prompt="What is 2+2?",
        task_type="reasoning",
        source="gsm8k",
        source_id="1",
        difficulty="easy",
        reference=reference,
        reference_kind=kind,
        licence="MIT",
    )


def _answer(text="4", tier="S", id_="gsm8k-1") -> AnswerRow:
    return AnswerRow(
        id=id_,
        tier=tier,
        model="m",
        text=text,
        finish_reason="stop",
        input_tokens=1,
        output_tokens=1,
        cost_gbp=0,
        latency_ms=1,
        via="live",
    )


def _judgment(id_="gsm8k-1", tier="S", score=4) -> Judgment:
    return Judgment(
        id=id_,
        tier=tier,
        score=score,
        correctness=score,
        completeness=score,
        clarity=score,
        reason="r",
        judge_model="m",
        rubric="judge_v1",
        cost_gbp=0.001,
        via="batch",
    )


def test_custom_id_round_trip_and_api_limits():
    cid = custom_id("math500-intermediate-algebra-1234", "L")
    assert split_custom_id(cid) == ("math500-intermediate-algebra-1234", "L")
    assert len(cid) <= 64 and all(c.isalnum() or c in "-_" for c in cid)


def test_user_message_is_blind_and_includes_reference():
    msg = user_message(_item("4", "gold"), _answer(tier="L"))
    assert '<reference kind="gold">\n4\n</reference>' in msg
    assert msg.endswith("<answer>\n4\n</answer>")
    assert "tier" not in msg.lower() and "sonnet" not in msg.lower()
    assert "<reference" not in user_message(_item(), _answer())


def test_judge_request_uses_config_and_schema():
    req = judge_request(_item(), _answer(tier="M"), CONFIG, "RUBRIC")
    assert req["custom_id"] == "gsm8k-1__M"
    p = req["params"]
    assert p["model"] == "claude-sonnet-5-5"
    assert p["system"] == "RUBRIC"
    assert p["max_tokens"] == CONFIG.judge.max_tokens
    assert p["output_config"]["effort"] == CONFIG.judge.effort
    assert p["output_config"]["format"]["schema"] is JUDGE_SCHEMA


def test_empty_answer_scored_by_rule():
    assert rule_judgment(_answer("   "), CONFIG, "judge_v1").score == 1
    assert rule_judgment(_answer("4"), CONFIG, "judge_v1") is None


def _result(payload, type_="succeeded", stop="end_turn", cid="gsm8k-1__M"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        stop_reason=stop,
    )
    return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type=type_, message=msg))


GOOD = {"reason": "Right.", "correctness": 5, "completeness": 4, "clarity": 5, "score": 5}


def test_result_parsed_and_costed():
    j = judgment_from_result(_result(GOOD), CONFIG, "judge_v1", "b1")
    assert (j.id, j.tier, j.score, j.completeness) == ("gsm8k-1", "M", 5, 4)
    # (1000 * $2 + 200 * $10) / 1M = $0.004, halved, in GBP
    assert j.cost_gbp == pytest.approx(0.004 * 0.5 * 0.79)


def test_overall_score_capped_at_correctness():
    payload = {**GOOD, "correctness": 2, "score": 4}
    assert judgment_from_result(_result(payload), CONFIG, "judge_v1", "b").score == 2


@pytest.mark.parametrize(
    "result",
    [
        _result(GOOD, type_="errored"),
        _result(GOOD, stop="max_tokens"),
        _result(GOOD, stop="refusal"),
        _result("not json"),
        _result({**GOOD, "score": 7}),  # out of range
    ],
)
def test_unusable_results_are_dropped(result):
    assert judgment_from_result(result, CONFIG, "judge_v1", "b") is None


def test_todo_and_save(tmp_path):
    answers = [_answer(tier="S"), _answer(tier="M"), _answer(tier="L", id_="other-1")]
    assert len(todo(answers, [_judgment(tier="S")], [])) == 2
    assert [a.id for a in todo(answers, [], [], only={"other-1"})] == ["other-1"]
    with pytest.raises(RuntimeError, match="collect"):
        todo(answers, [], [JudgeBatch(batch_id="b", requests=1, estimate_gbp=0.1)])

    save_judgments(tmp_path, [_judgment(tier="M"), _judgment(tier="S")])
    save_judgments(tmp_path, [_judgment(tier="M", score=2)])
    rows = load_judgments(tmp_path)
    assert [(r.tier, r.score) for r in rows] == [("M", 2), ("S", 4)]
