"""Dataset builder with fake source rows: no downloads."""

import json

import pytest

from gearbox.data import build as build_mod
from gearbox.data import sources
from gearbox.data.build import build, select
from gearbox.data.schema import PromptItem
from gearbox.data.sources import PICKS, Pick


def test_quotas_add_up_to_450_with_112_per_type():
    # MT-Bench contributes all 80 (10 per category); the rest have fixed quotas.
    fixed = {p.name: p.n for p in PICKS if p.n is not None}
    assert sum(fixed.values()) + 80 == 450


def test_mt_bench_first_turn_and_category_mapping():
    row = {
        "question_id": "119",
        "category": "math",
        "prompt": ["What is 2+2?", "And times 3?"],
        "reference": ["4", "12"],
    }
    item = sources.mt_bench(row, 0)
    assert item.prompt == "What is 2+2?"
    assert item.task_type == "reasoning"
    assert item.reference == "4"
    assert item.reference_kind == "gold"
    assert item.id == "mt-bench-119"


def test_mt_bench_without_reference():
    row = {"question_id": "81", "category": "writing", "prompt": ["Write a blog."]}
    item = sources.mt_bench(row, 0)
    assert item.reference is None and item.reference_kind is None


def test_gsm8k_strips_calculator_notes():
    row = {"question": "Q?", "answer": "48/2 = <<48/2=24>>24\n#### 72"}
    assert sources.gsm8k(row, 7).reference == "48/2 = 24\n#### 72"


def test_math500_level_sets_difficulty():
    row = {
        "problem": "p",
        "solution": "s",
        "answer": "3",
        "level": "5",
        "unique_id": "test/algebra/12.json",
    }
    item = sources.math500(row, 0)
    assert item.difficulty == "hard"
    assert item.id == "math500-algebra-12"
    assert item.reference.startswith("Final answer: 3")


def test_triviaqa_keeps_shortest_aliases():
    row = {
        "question_id": "qb_1",
        "question": "Capital of France?",
        "answer": {"value": "Paris", "aliases": ["Paris", "Paree", "City of Light of France", "P"]},
    }
    ref = sources.triviaqa(row, 0).reference
    assert ref.startswith("Paris (also accepted: P; Paree")


def test_dolly_filters_category_and_long_context():
    convert = sources.dolly("summarization")
    base = {"instruction": "Summarise:", "response": "Short.", "category": "summarization"}
    assert convert({**base, "context": "Some text."}, 3).prompt == "Summarise:\n\nSome text."
    assert convert({**base, "context": "x" * 5000}, 3) is None
    assert convert({**base, "context": "", "category": "open_qa"}, 3) is None
    assert convert({**base, "context": "", "response": " "}, 3) is None
    assert convert({**base, "context": ""}, 3).reference_kind == "example"


def test_overlong_prompt_skipped():
    row = {"question": "x" * (sources.MAX_PROMPT_CHARS + 1), "answer": "#### 1"}
    assert sources.gsm8k(row, 0) is None


def _rows(n):
    return [{"question": f"question {i}", "answer": f"#### {i}"} for i in range(n)]


def _pick(n, strata=None):
    return Pick("gsm8k", sources.GSM8K, n, sources.gsm8k, strata)


def test_select_is_seeded_and_skips_seen():
    a, usable = select(_pick(5), _rows(50), set(), seed=1)
    b, _ = select(_pick(5), _rows(50), set(), seed=1)
    c, _ = select(_pick(5), _rows(50), set(), seed=2)
    assert usable == 50
    assert [i.id for i in a] == [i.id for i in b]
    assert [i.id for i in a] != [i.id for i in c]

    seen = {"question 0", "question 1"}
    chosen, usable = select(_pick(None), _rows(10), seen, seed=1)
    assert usable == 8
    assert "question 0" not in {i.prompt for i in chosen}


def test_select_dedupes_within_source():
    rows = _rows(3) + [{"question": "Question 0", "answer": "#### 0"}]  # case-only dupe
    chosen, _ = select(_pick(None), rows, set(), seed=1)
    assert len(chosen) == 3


def test_select_strata_round_robin():
    def by_parity(item: PromptItem) -> str:
        return str(int(item.source_id) % 2)

    chosen, _ = select(_pick(4, by_parity), _rows(40), set(), seed=3)
    assert sorted(int(i.source_id) % 2 for i in chosen) == [0, 0, 1, 1]


def test_select_fails_loudly_when_pool_too_small():
    with pytest.raises(ValueError, match="wanted 10"):
        select(_pick(10), _rows(3), set())


def test_build_writes_reproducible_files(tmp_path, monkeypatch):
    # Two tiny picks over fake rows: checks file layout, manifest and determinism.
    picks = [_pick(3), Pick("mt-bench", sources.MT_BENCH, None, sources.mt_bench)]
    monkeypatch.setattr(build_mod, "PICKS", picks)
    mt_rows = [{"question_id": "1", "category": "coding", "prompt": ["Write fizzbuzz."]}]

    def loader(source):
        return mt_rows if source is sources.MT_BENCH else _rows(20)

    items = build(tmp_path / "v9", loader=loader)
    assert len(items) == 4
    first = (tmp_path / "v9" / "prompts.jsonl").read_bytes()
    manifest = json.loads((tmp_path / "v9" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "v9"
    assert manifest["by_task_type"] == {"coding": 1, "reasoning": 3}
    assert manifest["sources"][0]["revision"] == sources.GSM8K.revision

    build(tmp_path / "v9", loader=loader)
    assert (tmp_path / "v9" / "prompts.jsonl").read_bytes() == first
    for line in first.decode().splitlines():
        PromptItem.model_validate_json(line)


def test_committed_dataset_matches_manifest():
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "data" / "v1"
    body = (root / "prompts.jsonl").read_bytes()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(body).hexdigest() == manifest["prompts_sha256"]
    items = [PromptItem.model_validate_json(line) for line in body.decode().splitlines()]
    assert len(items) == manifest["total"] == 450
