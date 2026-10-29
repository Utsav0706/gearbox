# FAQ

## What is Gearbox?

An API that stands in for a language model. You send it the same request you would
send to OpenAI's chat completions endpoint; it decides which of three models should
answer (a free local one, Claude Haiku 5.5 or Claude Sonnet 5.5), calls it, and tells
you in response headers which model answered, why, and what it cost.

## Does it actually save money?

On 119 held-out test prompts it cost 93% less than always using Claude Sonnet 5.5,
and its answers scored 95% as well. Both numbers come with 95% intervals (91-94% saved,
90-99% quality) in [`reports/eval.md`](../reports/eval.md). The honest catch is that
always using Claude Haiku 5.5 scores the same on these prompts: most of the saving comes
from Haiku being good enough so often, and the router mostly learns to pick it.

## How do I run it?

Python 3.12+, an Anthropic API key for the two Claude tiers, and
[Ollama](https://ollama.com) with `llama3.2:3b` for the free tier. The README has the
exact commands. Short version: install, copy `.env.example` to `.env` and add the key,
run the migration, start `py -m gearbox.service`, and point an OpenAI client at
`http://127.0.0.1:8002/v1` with `model="auto"`. With Docker, `docker compose up --build`
starts it with Postgres and Redis.

## Can I force a particular model?

Yes. `model="S"`, `"M"` or `"L"` pins a tier and skips the router. The
`x-gearbox-max-tier` header caps a request instead, for example `M` for a feature that
must never use the expensive model.

## What does it cost to run?

The service itself is free; you pay for the Claude calls it makes, at Anthropic's list
price. On the test mix that came to about GBP 0.32 per 1,000 requests, against about
GBP 4.52 for always using Sonnet. A monthly budget in `config.yaml` (GBP 5 by default)
caps spend: once it is reached, every request goes to the free local model until the
month ends, and responses say so.

Building the dataset, labels and evals cost about GBP 4.85 in API calls in total, most
of it judging 1,800 answers.

## How was "quality" measured, and can the judge be trusted?

Each answer was scored 1 to 5 by Claude Sonnet 5.5 acting as a judge, with the
dataset's reference answer when it had one, and without knowing which model wrote the
answer. On 40 answers also scored blind by a person, the judge agreed on pass/fail
82.5% of the time (Cohen's kappa 0.65). Where they disagreed, the judge was always the
stricter one. The full comparison is in [`data/v1/README.md`](../data/v1/README.md).

## Is my data stored?

The request log, the logs and the traces hold metadata only: which model, why, tokens,
cost and timing. They never contain prompt or answer text. The answer cache does hold
answer text (in memory, or in Redis when configured) for one day, under a hash of the
request, so that an identical request can be answered for free. Turn it off with
`cache.enabled: false` in `config.yaml`. Prompts sent to tiers M and L go to Anthropic's
API; tier S runs entirely on your machine.

## What happens when a model is down?

Gearbox retries once, then tries bigger tiers, then smaller ones, never going above the
caller's cap or the budget's. The `x-gearbox-fallback` header shows the path (for
example `M->M->L`). If nothing answers, the response is a 503 with an OpenAI-style error.
If the cache (Redis) is down, requests carry on uncached.

## What doesn't it do?

- Streaming responses, tool or function calling, images, and `n > 1` are rejected with
  a clear 400 error rather than silently ignored.
- `temperature` and `top_p` are accepted but not passed on (the Claude tiers reject
  non-default sampling settings); the `x-gearbox-note` header says so.
- The cache matches identical requests only, not similar ones.
- The router reads only the last user message.

## What are the known limits of the result?

- The test prompts come from public benchmarks, not real application traffic. A
  different workload could favour the router more or less.
- 119 test prompts is a small sample, which is why the intervals are wide on quality.
- Which prompts need the large model turned out to be hard to predict from the prompt
  alone. A perfect router would reach 98% quality at 13% of the cost; the trained one
  does not get close to that.
- Prices are set in `config.yaml` and need updating when Anthropic changes them.
- The monthly budget is global, not per user.

## Where do I look next?

- [`docs/design.md`](design.md): how a request is handled and why it is built that way.
- [`reports/eval.md`](../reports/eval.md): the headline numbers and how to reproduce them.
- [`reports/routers.md`](../reports/routers.md) and [`reports/probe.md`](../reports/probe.md):
  how the routers compare and where the large model is worth its price.
