# Scout through Gearbox: first real run

Scout (the research agent) calls Claude through the Anthropic SDK. With Gearbox's
`/v1/messages` endpoint, pointing Scout at Gearbox is one line of Scout config
(`llm.base_url: http://127.0.0.1:8002`); no Scout code changed. Gearbox then picks the
model per call from Scout's `x-scout-task-type` tag (`routing.task_types`: plan,
extract, verify -> Haiku; write -> Sonnet), never above the model Scout asked for.

In practice one thing changes: Scout's **planner** (Sonnet) now runs on Haiku. Every
other node already used the model the mapping gives it.

## Same 3 questions, direct and through Gearbox

Three stable web questions from Scout's golden set, each run once directly and once
through Gearbox (Scout CLI, web search only, 2 November 2026).

| | Direct | Through Gearbox | Change |
|---|---|---|---|
| Cost per report (mean) | GBP 0.0268 | GBP 0.0135 | **-50%** |
| Time per report (mean) | 45.9 s | 30.3 s | -34% |
| Model calls per report (mean) | 22.7 | 15.3 | -33% |
| Claims per report (total over 3) | 26 | 20 | -23% |
| Scout's claim support rate (mean) | 0.92 | 0.89 | -0.03 |

Golden key points covered:

| Question | Key points | Direct | Through Gearbox |
|---|---|---|---|
| When did the UK minimum wage start, at what rate? | date; GBP 3.60 | date only | date only |
| When did the Bank of England get rate independence, by which law? | May 1997; 1998 Act; MPC | all three | all three |
| When was the NHS founded, and who led it? | 5 July 1948; Bevan | both | both |

Gearbox's request log and Scout's own cost tracking agree: 50 calls through Gearbox,
GBP 0.0405 in Gearbox's log against GBP 0.0404 summed from Scout's reports (45 calls on
Haiku, 5 on Sonnet). Spend for this comparison: GBP 0.12.

## Reading it

- Most of the saving is not the planner call itself (about GBP 0.003 a report): it is
  what the Haiku planner does. It splits questions into fewer sub-questions, so Scout
  searches, reads and verifies less. Reports were shorter (20 claims against 26) and
  finished faster.
- On these three questions the shorter reports covered the same key points, and both
  setups missed the same one (the 1999 rate). Three questions cannot show whether
  smaller plans hurt harder, multi-part questions (Scout's weakest category is questions
  needing both its documents and the web). That needs Scout's full 50-question golden
  set, an evaluation-harness job, before routing Scout's planner to Haiku by default.

## Reproduce

From `D:\AI Engine\scout`, with Gearbox running on port 8002: copy `config.yaml` to a
file outside git with `llm.base_url: http://127.0.0.1:8002`, then run
`py -m scout.agent.run "<question>" --user you@example.com --create-user --database-url
sqlite:///runs/e2.db --no-docs --json out.json` with `CONFIG_PATH` set to that file, and
again without it for the direct run.
