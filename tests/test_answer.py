"""Answer runner with fake batch results: no network, no cost."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gearbox.config import load_config
from gearbox.data.answer import (
    BATCH_DISCOUNT,
    AnswerRow,
    BatchRecord,
    batch_request,
    estimate_gbp,
    load_answers,
    paid_so_far,
    pilot_ids,
    row_from_result,
    save_answers,
    todo,
)
from gearbox.data.schema import PromptItem

CONFIG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def _prompt(i: int, task_type: str = "coding") -> PromptItem:
    return PromptItem(
        id=f"p-{i}",
        prompt=f"prompt {i}",
        task_type=task_type,
        source="test",
        source_id=str(i),
        difficulty="easy",
        licence="MIT",
    )


def _answer(id_: str, tier: str = "M", out: int = 100, cost: float = 0.001) -> AnswerRow:
    return AnswerRow(
        id=id_,
        tier=tier,
        model="m",
        text="a",
        finish_reason="stop",
        input_tokens=10,
        output_tokens=out,
        cost_gbp=cost,
        latency_ms=None,
        via="batch",
    )


def test_save_merges_replaces_and_sorts(tmp_path):
    save_answers(tmp_path, [_answer("p-2"), _answer("p-1", "L")])
    newer = _answer("p-2", out=999)
    total = save_answers(tmp_path, [newer, _answer("p-1", "S")])
    rows = load_answers(tmp_path)
    assert total == 3
    assert [(r.id, r.tier) for r in rows] == [("p-1", "L"), ("p-1", "S"), ("p-2", "M")]
    assert rows[2].output_tokens == 999  # the newer answer replaced the old one


def test_todo_skips_answered_and_open_batches():
    prompts = [_prompt(i) for i in range(4)]
    answers = [_answer("p-0", "M"), _answer("p-1", "L")]
    assert [p.id for p in todo(prompts, answers, "M")] == ["p-1", "p-2", "p-3"]
    assert [p.id for p in todo(prompts, answers, "M", only={"p-0", "p-3"})] == ["p-3"]
    open_batch = BatchRecord(batch_id="b", tier="M", model="m", requests=1, estimate_gbp=0.1)
    with pytest.raises(RuntimeError, match="collect"):
        todo(prompts, answers, "M", [open_batch])
    assert todo(prompts, answers, "L", [open_batch])  # other tiers unaffected


def test_answers_cut_by_a_lower_cap_are_redone():
    prompts = [_prompt(0), _prompt(1)]
    cut = _answer("p-0", "L", out=1500).model_copy(update={"finish_reason": "length"})
    answers = [cut, _answer("p-1", "L")]
    assert todo(prompts, answers, "L") == []  # no cap given: keep what we have
    assert todo(prompts, answers, "L", max_tokens=1500) == []  # same cap: same cut-off
    assert [p.id for p in todo(prompts, answers, "L", max_tokens=3000)] == ["p-0"]


def test_pilot_is_per_task_type_and_stable():
    prompts = [_prompt(i, t) for t in ("coding", "writing") for i in range(20 * len(t))]
    ids = pilot_ids(prompts, per_type=3)
    assert len(ids) == 6
    assert pilot_ids(prompts, per_type=3) == ids


def test_estimate_worst_case_then_measured():
    items = [_prompt(i) for i in range(10)]
    worst, basis = estimate_gbp(items, "L", CONFIG, [])
    assert "worst case" in basis
    # 10 answers x max_tokens output tokens at $10/M, halved, in GBP (input is tiny)
    cap = CONFIG.tiers["L"].max_tokens
    assert worst == pytest.approx(10 * cap * 10 / 1e6 * BATCH_DISCOUNT * 0.79, rel=0.01)
    out = CONFIG.tiers["L"].max_tokens // 10
    measured, basis = estimate_gbp(items, "L", CONFIG, [_answer("x", "L", out=out)])
    assert "measured" in basis
    assert measured == pytest.approx(worst / 10, rel=0.05)


def test_paid_so_far_counts_open_batch_estimates():
    batches = [
        BatchRecord(batch_id="a", tier="M", model="m", requests=1, estimate_gbp=0.5),
        BatchRecord(batch_id="b", tier="L", model="m", requests=1, estimate_gbp=9, collected=True),
    ]
    assert paid_so_far([_answer("p", cost=0.25)], batches) == pytest.approx(0.75)


def test_batch_request_matches_call_tier():
    req = batch_request(_prompt(7), "L", CONFIG)
    assert req["custom_id"] == "p-7"
    assert req["params"] == {
        "model": "claude-sonnet-5-5",
        "max_tokens": CONFIG.tiers["L"].max_tokens,
        "messages": [{"role": "user", "content": "prompt 7"}],
    }
    with pytest.raises(ValueError, match="not an Anthropic model"):
        batch_request(_prompt(7), "S", CONFIG)


def _result(type_="succeeded", stop="end_turn"):
    msg = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="Hello"),
            SimpleNamespace(type="text", text=" world"),
        ],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=2000),
        stop_reason=stop,
    )
    return SimpleNamespace(custom_id="p-1", result=SimpleNamespace(type=type_, message=msg))


def test_row_from_result_text_cost_and_finish():
    row = row_from_result(_result(), "L", CONFIG, "batch_1")
    assert row.text == "Hello world"
    assert row.finish_reason == "stop"
    assert row.via == "batch" and row.batch_id == "batch_1"
    # (1000 * $2 + 2000 * $10) / 1M = $0.022, halved, in GBP
    assert row.cost_gbp == pytest.approx(0.022 * 0.5 * 0.79)
    assert row_from_result(_result(stop="max_tokens"), "L", CONFIG, "b").finish_reason == "length"


@pytest.mark.parametrize("type_", ["errored", "canceled", "expired"])
def test_failed_results_are_skipped(type_):
    assert row_from_result(_result(type_), "M", CONFIG, "b") is None
