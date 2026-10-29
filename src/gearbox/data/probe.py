"""The hard-prompt probe: does Sonnet (L) beat Haiku (M) on genuinely hard prompts?

Dataset v1 showed Haiku passing as often as Sonnet (94.5% each on the test split), so a
router has almost nothing to gain from ever picking L. This 30-prompt probe checks
whether a gap appears on harder material before spending on a full "hard slice":

- AIME 2025 (10): competition maths, integer answers.
- MMLU-Pro (10): expert multiple choice with 10 options (law, engineering, physics,
  chemistry: the categories with the most reasoning).
- SimpleQA (10): obscure facts, where a bigger model's extra knowledge should show.

All MIT licensed, every prompt has a gold reference. No code source: the hard-code
sets are 0.6-1.3 GB downloads, and GPQA is gated behind a terms acceptance.

    py -m gearbox.data.probe        # writes data/probe/prompts.jsonl (free)
Then answer and judge with the normal tools and --data data/probe.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

from gearbox.data.build import build
from gearbox.data.schema import PromptItem, TaskType
from gearbox.data.sources import Pick, SourceFile, _item, by_difficulty

AIME_I = SourceFile(
    "opencompass/AIME2025", "a6ad95f611d72cf628a80b58bd0432ef6638f958", "aime2025-I.jsonl", "MIT"
)
AIME_II = SourceFile(
    "opencompass/AIME2025", "a6ad95f611d72cf628a80b58bd0432ef6638f958", "aime2025-II.jsonl", "MIT"
)
MMLU_PRO = SourceFile(
    "TIGER-Lab/MMLU-Pro",
    "b189ec765aa7ed75c8acfea42df31fdae71f97be",
    "data/test-00000-of-00001.parquet",
    "MIT",
)
SIMPLEQA = SourceFile(
    "basicv8vc/SimpleQA",
    "e319282ab125c3dbd0c7fd00be2e4dd54e7e8f94",
    "simple_qa_test_set.csv",
    "MIT",
)


def aime(part: str):
    source = AIME_I if part == "i" else AIME_II

    def convert(row: dict[str, Any], index: int) -> PromptItem | None:
        prompt = f"{row['question']}\n\nGive the final answer as an integer."
        return _item(
            "aime2025",
            source,
            f"{part}-{index + 1}",
            prompt,
            "reasoning",
            "hard",
            row["answer"],
            "gold",
        )

    return convert


_MMLU_TASK: dict[str, TaskType] = {
    "law": "research",
    "engineering": "reasoning",
    "physics": "reasoning",
    "chemistry": "reasoning",
}
LETTERS = "ABCDEFGHIJ"


def mmlu_pro(row: dict[str, Any], _: int) -> PromptItem | None:
    if row["category"] not in _MMLU_TASK:
        return None
    options = row["options"]
    if isinstance(options, str):  # some exports store the list as its repr
        options = ast.literal_eval(options)
    lines = [f"{LETTERS[i]}) {opt}" for i, opt in enumerate(options)]
    prompt = (
        f"{row['question']}\n\n"
        + "\n".join(lines)
        + "\n\nAnswer with the letter of the correct option, then a short justification."
    )
    i = int(row["answer_index"])
    item = _item(
        "mmlu-pro",
        MMLU_PRO,
        row["question_id"],
        prompt,
        _MMLU_TASK[row["category"]],
        "hard",
        f"{LETTERS[i]}) {options[i]}",
        "gold",
    )
    if item is not None:
        # Spread the 10 picks over the four categories (round-robin via strata).
        item.difficulty = "hard"
        item.source_id = f"{row['category']}:{row['question_id']}"
    return item


def mmlu_category(item: PromptItem) -> str:
    return item.source_id.split(":", 1)[0]


def simpleqa(row: dict[str, Any], index: int) -> PromptItem | None:
    return _item(
        "simpleqa", SIMPLEQA, index, row["problem"], "research", "hard", row["answer"], "gold"
    )


PROBE_PICKS: list[Pick] = [
    Pick("aime2025-i", AIME_I, 5, aime("i")),
    Pick("aime2025-ii", AIME_II, 5, aime("ii")),
    Pick("mmlu-pro", MMLU_PRO, 10, mmlu_pro, strata=mmlu_category),
    Pick("simpleqa", SIMPLEQA, 10, simpleqa, strata=by_difficulty),
]


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path("data/probe")
    items = build(out, picks=PROBE_PICKS)
    print(f"{len(items)} probe prompts -> {out / 'prompts.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
