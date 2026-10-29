# Gearbox

[![CI](https://github.com/Utsav0706/gearbox/actions/workflows/ci.yml/badge.svg)](https://github.com/Utsav0706/gearbox/actions/workflows/ci.yml)

An LLM auto-router. An app sends a chat request to Gearbox as it would to any model;
Gearbox predicts the **cheapest model tier that will still answer it well**, calls that
model, handles failures, and logs the cost, latency and reason for every route.

> Headline (coming in M8): **"Gearbox cuts LLM cost by X% at Y% of quality"**,
> reproducible with one command.

| Tier | Model | Cost |
|---|---|---|
| S | small local model via Ollama | free |
| M | Claude Haiku 5.5 | cheap |
| L | Claude Sonnet 5.5 | frontier |

Model names live in [`config.yaml`](config.yaml), so a tier can be swapped without
code changes.

Status: **M6, the API runs** (OpenAI-compatible, request log); routers compared in [`reports/routers.md`](reports/routers.md). One of four connected projects:
[Scout](https://github.com/Utsav0706/scout) (research agent), Gearbox (router),
Dyno (eval harness) and Pitwall (LLMOps).

## Run it locally (Windows PowerShell)

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env    # then paste your Anthropic key into .env
.venv\Scripts\python.exe -m alembic upgrade head    # creates the local request log
.venv\Scripts\python.exe -m gearbox.service         # http://127.0.0.1:8002
```

Tier S needs [Ollama](https://ollama.com) with `ollama pull llama3.2:3b`.

Then point any OpenAI client at it:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8002/v1", api_key="unused")
reply = client.chat.completions.create(
    model="auto",  # or "S", "M", "L" to pin a tier
    messages=[{"role": "user", "content": "What is 17 * 23?"}],
)
```

Every answer carries `x-gearbox-tier`, `x-gearbox-model`, `x-gearbox-cost` (GBP) and
`x-gearbox-router` headers. `GET /health` and `GET /stats?hours=24` (cost and route
split) are also served. Tests: `.venv\Scripts\python.exe -m pytest`.

## Plan

| Milestone | What |
|---|---|
| M0 | Skeleton: config, logging, tests, CI |
| M1 | Tier clients (Ollama, Haiku, Sonnet) with cost tracking |
| M2 | 450-prompt dataset across coding, research, writing, reasoning |
| M3 | Answers from all three tiers |
| M4 | LLM judge, labels (cheapest passing tier), hand-label check (Cohen's kappa) |
| M5 | Three routers: rules, LLM classifier, embeddings + logistic regression |
| M6 | OpenAI-compatible API, request log |
| M7 | Cache, policy (max tier, budget cap), fallback, canary split, tracing, Docker Compose |
| M8 | `make eval` headline numbers + Streamlit dashboard |
| M9 | Docs |
