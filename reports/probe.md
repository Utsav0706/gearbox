# Hard-prompt probe: does Sonnet (L) beat Haiku (M) on hard prompts?

Why: on dataset v1's test split Haiku and Sonnet both passed 94.5% of prompts, so a
router had almost nothing to gain from ever choosing L (see `routers.md`). Before
spending on more data, this probe checks whether a gap appears on harder material.

30 prompts (`data/probe`, built with `py -m gearbox.data.probe`), answered by both tiers
through the normal pipeline (same `max_tokens` 3,000, same judge and rubric):

| source (10 each) | Haiku passes | Sonnet passes | only Sonnet | only Haiku |
|---|---|---|---|---|
| SimpleQA (obscure facts) | 1 | 6 | 5 | 0 |
| AIME 2025 (competition maths) | 8 | 10 | 2 | 0 |
| MMLU-Pro (expert multiple choice) | 9 | 9 | 1 | 1 |
| **total** | **18 (60%)** | **25 (83%)** | **8** | **1** |

Both "only Sonnet" AIME cases are Haiku spending all 3,000 tokens thinking and
returning no answer. Cost of the probe: GBP 0.20 (answers 0.106, judging 0.097).

Finding: the gap is mostly knowledge (niche facts), plus a little on very hard maths;
expert multiple choice shows none. That is something a router can learn from the
prompt itself ("a niche factual question -> L"), unlike v1's main question (whether
the 3B model gets lucky), which was close to unpredictable.
