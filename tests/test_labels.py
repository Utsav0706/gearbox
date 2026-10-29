"""Labels, split and agreement maths: no network."""

import pytest

from gearbox.data.judge import Judgment
from gearbox.data.labels import (
    LabelRow,
    cohen_kappa,
    label_for,
    pick_hand_items,
    split,
    weighted_kappa,
)


@pytest.mark.parametrize(
    ("scores", "label"),
    [
        ({"S": 4, "M": 5, "L": 5}, "S"),  # cheapest passing tier wins
        ({"S": 3, "M": 4, "L": 5}, "M"),
        ({"S": 3, "M": 3, "L": 4}, "L"),
        ({"S": 5, "M": 2, "L": 2}, "S"),  # S passing is enough, whatever bigger tiers did
        # nobody passes: the best score, the cheapest tier on a tie
        ({"S": 2, "M": 3, "L": 3}, "M"),
        ({"S": 1, "M": 2, "L": 3}, "L"),
        ({"S": 3, "M": 3, "L": 3}, "S"),
    ],
)
def test_label_is_cheapest_passing_tier(scores, label):
    assert label_for(scores) == label


def _row(i: int, task_type: str, label: str) -> LabelRow:
    return LabelRow(
        id=f"{task_type}-{label.lower()}-{i}",
        task_type=task_type,
        difficulty="easy",
        label=label,
        scores={"S": 1, "M": 1, "L": 1},
    )


def test_split_is_stratified_disjoint_and_seeded():
    rows = [_row(i, t, lab) for t in ("coding", "writing") for lab in "SML" for i in range(10)]
    parts = split(rows)
    assert len(parts["test"]) == 12  # 20% of each of the 6 groups of 10
    assert not set(parts["train"]) & set(parts["test"])
    assert len(parts["train"]) + len(parts["test"]) == len(rows)
    test_rows = [r for r in rows if r.id in set(parts["test"])]
    assert all(
        sum(r.task_type == t and r.label == lab for r in test_rows) == 2
        for t in ("coding", "writing")
        for lab in "SML"
    )
    assert split(rows) == parts
    assert split(rows, seed=1) != parts


def _judgment(i: int, tier: str, score: int) -> Judgment:
    return Judgment(
        id=f"p-{i}",
        tier=tier,
        score=score,
        correctness=score,
        completeness=score,
        clarity=score,
        reason="r",
        judge_model="m",
        rubric="judge_v1",
        cost_gbp=0,
        via="batch",
    )


def test_hand_items_cover_tiers_and_pass_fail():
    judgments = [
        _judgment(i, tier, score) for i in range(30) for tier in "SML" for score in (2,)
    ] + [_judgment(i + 100, tier, 5) for i in range(30) for tier in "SML"]
    items = pick_hand_items(judgments, n=12)
    assert len(items) == len(set(items)) == 12
    assert {t for _, t in items} == {"S", "M", "L"}
    passed = sum(int(pid.split("-")[1]) >= 100 for pid, _ in items)
    assert passed == 6  # half passes, half fails
    assert pick_hand_items(judgments, n=12) == items


def test_reads_excel_cp1252_csv(tmp_path):
    from gearbox.data.labels import read_csv_text

    path = tmp_path / "scores.csv"
    path.write_bytes("item,score,notes\n1,5,“ok” at 28°\n".encode("cp1252"))
    assert "“ok” at 28°" in read_csv_text(path)
    path.write_bytes("﻿item,score\n".encode())  # UTF-8 with BOM, also from Excel
    assert read_csv_text(path).startswith("item,score")


def test_cohen_kappa_known_values():
    assert cohen_kappa([1, 1, 0, 0], [1, 1, 0, 0]) == 1.0
    # Always-"pass" vs a rater with 80% passes: 80% agreement, kappa 0.
    a = [True] * 10
    b = [True] * 8 + [False] * 2
    assert cohen_kappa(a, b) == pytest.approx(0.0)
    # Textbook example: 20 items, observed 0.7, chance 0.5 -> kappa 0.4.
    x = [1] * 10 + [0] * 10
    y = [1] * 7 + [0] * 3 + [1] * 3 + [0] * 7
    assert cohen_kappa(x, y) == pytest.approx(0.4)


def test_weighted_kappa_penalises_far_misses_more():
    human = [1, 2, 3, 4, 5, 5, 4, 3]
    near = [2, 2, 3, 4, 4, 5, 4, 3]  # off by one twice
    far = [5, 2, 3, 4, 1, 5, 4, 3]  # off by four twice
    assert weighted_kappa(human, human) == 1.0
    assert weighted_kappa(human, near) > weighted_kappa(human, far)
