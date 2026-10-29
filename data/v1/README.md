# Dataset v1: 450 prompts

`prompts.jsonl`: one prompt per line (schema: `src/gearbox/data/schema.py`).
`manifest.json`: the exact source commits, the seed, counts, and the file's sha256.
Rebuild byte-for-byte with `py -m gearbox.data.build` (needs `pip install -e ".[data]"`).

| task type | n | sources |
|---|---|---|
| coding | 112 | MT-Bench, HumanEval, MBPP |
| reasoning | 112 | MT-Bench, GSM8K, MATH-500 |
| research | 112 | MT-Bench, TriviaQA, TruthfulQA |
| writing | 114 | MT-Bench, Databricks Dolly 15k |

`difficulty` is a hint from the source, not the label. The label (cheapest tier whose
answer the judge scores 4 or more) is added in a later step.

## Licences

Each row carries its source's licence in its `licence` field.

| source | licence |
|---|---|
| [MT-Bench](https://huggingface.co/datasets/HuggingFaceH4/mt_bench_prompts) | Apache-2.0 |
| [HumanEval](https://huggingface.co/datasets/openai/openai_humaneval) | MIT |
| [MBPP (sanitized)](https://huggingface.co/datasets/google-research-datasets/mbpp) | CC-BY-4.0 |
| [GSM8K](https://huggingface.co/datasets/openai/gsm8k) | MIT |
| [MATH-500](https://huggingface.co/datasets/HuggingFaceH4/MATH-500) | MIT (from Hendrycks et al.'s MATH) |
| [TriviaQA](https://huggingface.co/datasets/mandarjoshi/trivia_qa) | Apache-2.0 |
| [TruthfulQA](https://huggingface.co/datasets/truthfulqa/truthful_qa) | Apache-2.0 |
| [Databricks Dolly 15k](https://huggingface.co/datasets/databricks/databricks-dolly-15k) | CC-BY-SA-3.0 |

The Dolly rows are share-alike: if you redistribute them, keep them under CC-BY-SA-3.0.

## Answers (M3)

`answers.jsonl`: one answer per prompt per tier (1,350 rows), sorted by id then tier.
Built with `py -m gearbox.data.answer` (tier S on a laptop GPU, tiers M and L through
Anthropic's Message Batches API at half price; `batches.json` lists the batch ids).
Every tier used the same `max_tokens` (3,000, from `config.yaml`) and default settings,
so these are the answers the router would get live.

| tier | model | avg output tokens | cut off at max_tokens | cost |
|---|---|---|---|---|
| S | llama3.2:3b (local) | 265 | 1 | £0 |
| M | claude-haiku-5-5 | 585 | 6 | £0.054 |
| L | claude-sonnet-5-5 | 491 | 3 | £0.911 |

Output tokens include thinking. One Haiku answer (`mbpp-438`) is empty: it spent all
3,000 tokens thinking. It is kept: a real failure is what the labels should record.

## Judgments, labels and split (M4)

`judgments.jsonl`: every answer scored 1-5 by the judge (`config.yaml` `judge`:
Claude Sonnet 5.5, low effort, rubric `prompts/judge_v1.md`), blind to which tier wrote
it, with the reference answer when one exists, as structured JSON with a one-line reason.
Two rules are applied in code: an empty answer scores 1 without a model call, and the
overall score never exceeds the correctness score. Cost: GBP 2.39 (Message Batches).

| tier | pass rate (score >= 4) | mean score |
|---|---|---|
| S (llama3.2:3b) | 46% | 3.04 |
| M (Haiku 5.5) | 93% | 4.68 |
| L (Sonnet 5.5) | 96% | 4.75 |

`labels.jsonl`: label = cheapest tier scoring >= 4, else L. Mix: S 208, M 215, L 27.
Only 15 prompts pass on L alone; the other 12 L labels are prompts no tier passed.

`split.json`: 359 train / 91 test, stratified by task type and label (seeded).

`hand_labels/`: 40 answers for a human to score blind (half judge-pass, half
judge-fail, spread over the tiers), to measure judge-human agreement with Cohen's kappa
(`py -m gearbox.data.labels kappa`).

`manifest.json` `files_sha256` holds a hash of every data file.

### Judge vs human (40 blind hand labels)

| measure | value |
|---|---|
| pass/fail agreement | 82.5% (33/40) |
| pass/fail Cohen's kappa | 0.65 (substantial) |
| 1-5 quadratic-weighted kappa | 0.78 |
| judge stricter / more lenient than the human | 13 / 2 |

All 7 pass/fail disagreements go the same way: the human passed the answer, the judge
failed it. Reading the judge's reasons:
- 3 are fair catches the human missed: "fewer than two paragraphs" answered in two
  (mt-bench-85); "one line per equation" ignored (mt-bench-139); a parity function that
  passes the given tests but checks odd/even instead of bit parity (mbpp-406).
- 2 are judgment calls on a 3-vs-4 border: small factual slips in a travel list
  (dolly-1227) and plot holes in a story (mt-bench-87).
- 2 are judge errors: its reason says the answer is correct, yet it scored 2
  (math500-geometry-434, mt-bench-125). A minor wording slip was treated as a wrong answer.

So the judge errs on the strict side. For routing that is the safe direction: a too
strict label sends a prompt to a bigger tier than needed (costs a little more), never to
a smaller one (worse answers). The headline saving is therefore, if anything,
understated. Rubric v1 is kept; a v2 that stops penalising correct answers for wording
slips is a candidate for Dyno's judge calibration.
