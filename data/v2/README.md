# Dataset v2: 600 prompts (v1's 450 + a 150-prompt hard slice)

Built with `py -m gearbox.data.v2`. v1 showed Haiku (M) passing as often as Sonnet (L),
so the router never had a reason to pick L. The probe (`reports/probe.md`) found where
Sonnet is worth its price, and v2 adds those prompts:

| hard slice | n | licence |
|---|---|---|
| AIME 2025 I + II (all problems) | 30 | MIT |
| MMLU-Pro (law, engineering, physics, chemistry) | 40 | MIT |
| SimpleQA (niche facts) | 80 | MIT |

Every answer and judgment that already existed (from `data/v1` and `data/probe`) was
copied in rather than paid for again. Same tiers, `max_tokens` (3,000), judge and
rubric as v1. New spend: GBP 0.95 (answers 0.36, judging 0.59).

| tier | pass rate over all 600 |
|---|---|
| S (llama3.2:3b) | 35% |
| M (Haiku 5.5) | 81% |
| L (Sonnet 5.5) | 87% |

Labels: the cheapest tier scoring >= 4; if no tier passes, the tier with the best
score (cheapest on a tie). The spec's rule was "else L", which gave S 212 / M 277 / L 111,
where 65 of the L labels were prompts no tier passes (50 of them SimpleQA facts no model
knows). Trained on those, v3 sent such questions to Sonnet: on the test split it reached
an 81.8% pass rate at GBP 0.139, against 81.0% at GBP 0.044 with the rule used here, so
the old rule mostly bought expensive failures. Current mix: S 261 / M 287 / L 52.
Split: 481 train / 119 test, stratified by task type and label.

Prompt sources and licences per row are as in `data/v1/README.md`, plus the table
above; all source files are pinned in `manifest.json`.
