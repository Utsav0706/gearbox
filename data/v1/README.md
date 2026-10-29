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
