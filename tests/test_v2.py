"""Dataset v2 reuses earlier answers and judgments only for prompts it contains."""

from gearbox.data import v2
from gearbox.data.answer import AnswerRow, load_answers, save_answers
from gearbox.data.judge import Judgment, load_judgments, save_judgments
from gearbox.data.schema import PromptItem


def _item(id_: str) -> PromptItem:
    return PromptItem(
        id=id_,
        prompt=id_,
        task_type="coding",
        source="t",
        source_id="1",
        difficulty="easy",
        licence="MIT",
    )


def _answer(id_: str) -> AnswerRow:
    return AnswerRow(
        id=id_,
        tier="M",
        model="m",
        text="a",
        finish_reason="stop",
        input_tokens=1,
        output_tokens=1,
        cost_gbp=0.001,
        latency_ms=None,
        via="batch",
    )


def _judgment(id_: str) -> Judgment:
    return Judgment(
        id=id_,
        tier="M",
        score=4,
        correctness=4,
        completeness=4,
        clarity=4,
        reason="r",
        judge_model="m",
        rubric="judge_v1",
        cost_gbp=0.001,
        via="batch",
    )


def test_build_v2_copies_matching_rows_only(tmp_path, monkeypatch):
    old = tmp_path / "v1"
    old.mkdir()
    save_answers(old, [_answer("keep-1"), _answer("gone-1")])
    save_judgments(old, [_judgment("keep-1"), _judgment("gone-1")])
    monkeypatch.setattr(v2, "build", lambda out, picks: [_item("keep-1"), _item("new-1")])

    out = tmp_path / "v2"
    out.mkdir()
    counts = v2.build_v2(out, reuse_from=[old])
    assert counts == {"prompts": 2, "answers_reused": 1, "judgments_reused": 1}
    assert [a.id for a in load_answers(out)] == ["keep-1"]
    assert [j.id for j in load_judgments(out)] == ["keep-1"]
