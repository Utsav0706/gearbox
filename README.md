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

Status: **M2, dataset built** (450 prompts in [`data/v1`](data/v1)); tier clients working. Part of a four-project portfolio:
[Scout](https://github.com/Utsav0706/scout) (research agent), Gearbox (router),
Dyno (eval harness) and Pitwall (LLMOps).

## Run it locally (Windows PowerShell)

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env    # then paste your keys into .env
.venv\Scripts\python.exe -m gearbox.config    # validates config.yaml
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest
```

On Linux/CI the same steps are `make install` and `make check`.

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
