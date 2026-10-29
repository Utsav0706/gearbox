"""Where the 450 prompts come from, and how each source row becomes a PromptItem.

Each Pick takes n rows from one public dataset file, pinned to an exact commit so the
build gives the same prompts next year. Quotas give ~112 prompts per task type and mix
easy and hard on purpose: a router can only learn "small model is enough" if the data
has prompts where it is, and "big model needed" if it has prompts where it isn't.

- coding (112): MT-Bench coding 10, HumanEval 51, MBPP 51
- reasoning (112): MT-Bench math + reasoning 20, GSM8K 46, MATH-500 46 (even across
  easy/medium/hard)
- research (112): MT-Bench stem + humanities 20, TriviaQA 46, TruthfulQA 46
- writing (114): MT-Bench writing + roleplay + extraction 30, Dolly creative writing,
  brainstorming and summarisation 28 each

The converters are pure functions (row in, PromptItem or None out), so they are tested
without downloading anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gearbox.data.schema import Difficulty, PromptItem, TaskType

# Longer prompts are skipped: they would dominate the cost of answering and judging.
MAX_PROMPT_CHARS = 6000


@dataclass(frozen=True)
class SourceFile:
    repo: str  # Hugging Face dataset repo
    revision: str  # exact commit, so the build is reproducible
    file: str
    licence: str


@dataclass(frozen=True)
class Pick:
    name: str
    source: SourceFile
    n: int | None  # None = every row the converter accepts
    convert: Callable[[dict[str, Any], int], PromptItem | None]
    # Rows are drawn round-robin across these groups (e.g. easy/medium/hard maths).
    strata: Callable[[PromptItem], str] | None = None


def _id(*parts: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(str(p) for p in parts).lower()).strip("-")


def _item(
    pick: str,
    source: SourceFile,
    source_id: object,
    prompt: str,
    task_type: TaskType,
    difficulty: Difficulty,
    reference: str | None = None,
    reference_kind: str | None = None,
) -> PromptItem | None:
    prompt = prompt.strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        return None
    return PromptItem(
        id=_id(pick, source_id),
        prompt=prompt,
        task_type=task_type,
        source=pick,
        source_id=str(source_id),
        difficulty=difficulty,
        reference=reference.strip() if reference else None,
        reference_kind=reference_kind if reference else None,  # type: ignore[arg-type]
        licence=source.licence,
    )


# --- MT-Bench: 80 multi-turn questions in 8 categories (first turn only) ---------------

MT_BENCH = SourceFile(
    "HuggingFaceH4/mt_bench_prompts",
    "e3a795c5e9a82ee40611c416b8a7786c73198991",
    "raw/question.jsonl",
    "Apache-2.0",
)
_MT_TASK: dict[str, TaskType] = {
    "coding": "coding",
    "math": "reasoning",
    "reasoning": "reasoning",
    "stem": "research",
    "humanities": "research",
    "writing": "writing",
    "roleplay": "writing",
    "extraction": "writing",
}


def mt_bench(row: dict[str, Any], _: int) -> PromptItem | None:
    refs = row.get("reference") or []
    # Written to be hard for chat models: every category counts as "hard".
    return _item(
        "mt-bench",
        MT_BENCH,
        row["question_id"],
        row["prompt"][0],
        _MT_TASK[row["category"]],
        "hard",
        refs[0] if refs else None,
        "gold",
    )


# --- Coding -----------------------------------------------------------------------------

HUMANEVAL = SourceFile(
    "openai/openai_humaneval",
    "7dce6050a7d6d172f3cc5c32aa97f52fa1a2e544",
    "openai_humaneval/test-00000-of-00001.parquet",
    "MIT",
)


def humaneval(row: dict[str, Any], _: int) -> PromptItem | None:
    prompt = (
        "Complete the following Python function. Return the whole function.\n\n"
        f"```python\n{row['prompt']}```"
    )
    return _item(
        "humaneval",
        HUMANEVAL,
        row["task_id"].split("/")[-1],
        prompt,
        "coding",
        "medium",
        row["prompt"] + row["canonical_solution"],
        "gold",
    )


MBPP = SourceFile(
    "google-research-datasets/mbpp",
    "4bb6404fdc6cacfda99d4ac4205087b89d32030c",
    "sanitized/test-00000-of-00001.parquet",
    "CC-BY-4.0",
)


def mbpp(row: dict[str, Any], _: int) -> PromptItem | None:
    # The tests pin the function name and signature, as in the original benchmark.
    tests = "\n".join(row["test_list"])
    prompt = f"{row['prompt']}\n\nYour code should pass these tests:\n{tests}"
    return _item("mbpp", MBPP, row["task_id"], prompt, "coding", "easy", row["code"], "gold")


# --- Reasoning / maths ------------------------------------------------------------------

GSM8K = SourceFile(
    "openai/gsm8k",
    "740312add88f781978c0658806c59bc2815b9866",
    "main/test-00000-of-00001.parquet",
    "MIT",
)


def gsm8k(row: dict[str, Any], index: int) -> PromptItem | None:
    # Drop the <<48/2=24>> calculator notes; keep the worked answer ending "#### 72".
    answer = re.sub(r"<<[^>]*>>", "", row["answer"])
    return _item("gsm8k", GSM8K, index, row["question"], "reasoning", "medium", answer, "gold")


MATH500 = SourceFile(
    "HuggingFaceH4/MATH-500",
    "6e4ed1a2a79af7d8630a6b768ec859cb5af4d3be",
    "test.jsonl",
    "MIT",  # subset of Hendrycks et al.'s MATH, released under MIT
)
_MATH_LEVEL: dict[int, Difficulty] = {1: "easy", 2: "easy", 3: "medium", 4: "hard", 5: "hard"}


def math500(row: dict[str, Any], _: int) -> PromptItem | None:
    reference = f"Final answer: {row['answer']}\n\n{row['solution']}"
    return _item(
        "math500",
        MATH500,
        row["unique_id"].removeprefix("test/").removesuffix(".json"),
        row["problem"],
        "reasoning",
        _MATH_LEVEL[int(row["level"])],
        reference,
        "gold",
    )


def by_difficulty(item: PromptItem) -> str:
    return item.difficulty


# --- Research / factual -----------------------------------------------------------------

TRIVIAQA = SourceFile(
    "mandarjoshi/trivia_qa",
    "0f7faf33a3908546c6fd5b73a660e0f8ff173c2f",
    "rc.web.nocontext/validation-00000-of-00001.parquet",
    "Apache-2.0",  # per the TriviaQA repo (the Hugging Face card says "unknown")
)


def triviaqa(row: dict[str, Any], _: int) -> PromptItem | None:
    answer = row["answer"]
    # The alias lists are scraped and noisy ("Independence Day of Ecuador" for Ecuador);
    # the shortest few are the real alternative spellings.
    aliases = sorted({a for a in answer.get("aliases", []) if a != answer["value"]}, key=len)[:4]
    reference = answer["value"] + (f" (also accepted: {'; '.join(aliases)})" if aliases else "")
    return _item(
        "triviaqa",
        TRIVIAQA,
        row["question_id"],
        row["question"],
        "research",
        "easy",
        reference,
        "gold",
    )


TRUTHFULQA = SourceFile(
    "truthfulqa/truthful_qa",
    "741b8276f2d1982aa3d5b832d3ee81ed3b896490",
    "generation/validation-00000-of-00001.parquet",
    "Apache-2.0",
)


def truthfulqa(row: dict[str, Any], index: int) -> PromptItem | None:
    # Questions built to catch common misconceptions: hard for small models on purpose.
    reference = (
        f"Best answer: {row['best_answer']}\n"
        f"Also correct: {'; '.join(row['correct_answers'])}\n"
        f"Common wrong answers: {'; '.join(row['incorrect_answers'])}"
    )
    return _item(
        "truthfulqa",
        TRUTHFULQA,
        index,
        row["question"],
        "research",
        "hard",
        reference,
        "gold",
    )


# --- Writing ----------------------------------------------------------------------------

DOLLY = SourceFile(
    "databricks/databricks-dolly-15k",
    "bdd27f4d94b9c1f951818a7da7fd7aeea5dbff1a",
    "databricks-dolly-15k.jsonl",
    "CC-BY-SA-3.0",
)
_DOLLY_DIFFICULTY: dict[str, Difficulty] = {
    "creative_writing": "medium",
    "brainstorming": "easy",
    "summarization": "easy",
}
MAX_DOLLY_CONTEXT_CHARS = 2500


def dolly(category: str) -> Callable[[dict[str, Any], int], PromptItem | None]:
    def convert(row: dict[str, Any], index: int) -> PromptItem | None:
        if row["category"] != category or not row["response"].strip():
            return None
        context = row["context"].strip()
        if len(context) > MAX_DOLLY_CONTEXT_CHARS:
            return None
        prompt = row["instruction"] + (f"\n\n{context}" if context else "")
        # Written by Databricks staff: one acceptable answer, not the only right one.
        return _item(
            "dolly",
            DOLLY,
            index,
            prompt,
            "writing",
            _DOLLY_DIFFICULTY[category],
            row["response"],
            "example",
        )

    return convert


PICKS: list[Pick] = [
    Pick("mt-bench", MT_BENCH, None, mt_bench),  # all 80
    Pick("humaneval", HUMANEVAL, 51, humaneval),
    Pick("mbpp", MBPP, 51, mbpp),
    Pick("gsm8k", GSM8K, 46, gsm8k),
    Pick("math500", MATH500, 46, math500, strata=by_difficulty),
    Pick("triviaqa", TRIVIAQA, 46, triviaqa),
    Pick("truthfulqa", TRUTHFULQA, 46, truthfulqa),
    Pick("dolly-creative", DOLLY, 28, dolly("creative_writing")),
    Pick("dolly-brainstorming", DOLLY, 28, dolly("brainstorming")),
    Pick("dolly-summarization", DOLLY, 28, dolly("summarization")),
]
