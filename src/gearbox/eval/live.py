"""Live eval: the test prompts through the running API, end to end, then judged.

The offline eval replays stored answers; this checks that the real service (router,
policy, fallback, cache, the HTTP layer) gives the same picture. It sends every test
prompt to POST /v1/chat/completions exactly as a client would, reads the tier and cost
from the x-gearbox-* headers, then scores the answers with the same judge and rubric
as the dataset, in one Message Batch.

Costs money (the answers at list price, plus judging at half price), so it only sends
with --yes, after printing an estimate, and refuses anything over --cap-gbp.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from gearbox.config import AppConfig, Tier
from gearbox.data.answer import AnswerRow, _client, load_prompts
from gearbox.data.judge import (
    PASS_SCORE,
    Judgment,
    judge_request,
    judgment_from_result,
    load_judgments,
)
from gearbox.data.schema import PromptItem


@dataclass
class LiveAnswer:
    id: str
    tier: Tier
    router: str
    text: str
    cost_gbp: float
    latency_ms: int
    cache_hit: bool
    fallback: str | None


def ask(client: httpx.Client, base_url: str, item: PromptItem) -> LiveAnswer:
    start = time.perf_counter()
    r = client.post(
        f"{base_url}/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": item.prompt}]},
        timeout=300,
    )
    r.raise_for_status()
    body = r.json()
    return LiveAnswer(
        id=item.id,
        tier=r.headers["x-gearbox-tier"],  # type: ignore[arg-type]
        router=r.headers["x-gearbox-router"],
        text=body["choices"][0]["message"]["content"],
        cost_gbp=float(r.headers["x-gearbox-cost"]),
        latency_ms=int((time.perf_counter() - start) * 1000),
        cache_hit=r.headers.get("x-gearbox-cache") == "hit",
        fallback=r.headers.get("x-gearbox-fallback"),
    )


def estimate_gbp(n: int, offline: dict[str, Any], router: str, data_dir: Path) -> float:
    """Answers: the offline cost of this router's picks. Judging: the measured average
    cost of a judgment in this dataset."""
    answers = offline[router]["cost_gbp"]
    paid = [j for j in load_judgments(data_dir) if j.via == "batch"]
    per_judgment = sum(j.cost_gbp for j in paid) / len(paid)
    return answers + n * per_judgment


def judge_all(
    answers: list[LiveAnswer], prompts: dict[str, PromptItem], config: AppConfig, poll_s: int = 30
) -> list[Judgment]:
    rubric = config.judge.prompt.read_text(encoding="utf-8")
    rows = [
        AnswerRow(
            id=a.id,
            tier=a.tier,
            model=config.tiers[a.tier].model,
            text=a.text,
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            cost_gbp=a.cost_gbp,
            latency_ms=a.latency_ms,
            via="live",
        )
        for a in answers
        if a.text.strip()
    ]
    client = _client()
    batch = client.messages.batches.create(
        requests=[judge_request(prompts[r.id], r, config, rubric) for r in rows]
    )
    print(f"judging {len(rows)} answers in batch {batch.id}...")
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        time.sleep(poll_s)
    judged = []
    for result in client.messages.batches.results(batch.id):
        j = judgment_from_result(result, config, config.judge.prompt.stem, batch.id)
        if j is not None:
            judged.append(j)
    return judged


def summarise(
    answers: list[LiveAnswer], judged: list[Judgment], offline: dict[str, Any]
) -> dict[str, Any]:
    scores = {j.id: j.score for j in judged}
    for a in answers:  # an empty answer fails, as in the dataset
        scores.setdefault(a.id, 1)
    n = len(answers)
    mean = sum(scores.values()) / n
    cost = sum(a.cost_gbp for a in answers)
    base = offline["always-L"]
    lat = sorted(a.latency_ms for a in answers)
    return {
        "prompts": n,
        "quality_pct": round(100 * mean / base["mean_score"], 1),
        "mean_score": round(mean, 3),
        "pass_rate": round(sum(s >= PASS_SCORE for s in scores.values()) / n, 4),
        "answer_cost_gbp": round(cost, 5),
        "saved_pct": round(100 * (1 - cost / base["cost_gbp"]), 1),
        "judge_cost_gbp": round(sum(j.cost_gbp for j in judged), 5),
        "picks": {t: sum(a.tier == t for a in answers) for t in ("S", "M", "L")},
        "cache_hits": sum(a.cache_hit for a in answers),
        "fallbacks": sum(a.fallback is not None for a in answers),
        "latency_p50_ms": lat[n // 2],
        "latency_p95_ms": lat[min(n - 1, int(0.95 * n))],
    }


def run(
    base_url: str, data_dir: Path, config: AppConfig, ids: list[str], out: Path
) -> dict[str, Any]:
    prompts = {p.id: p for p in load_prompts(data_dir)}
    answers = []
    with httpx.Client() as client:
        for n, pid in enumerate(ids, 1):
            answers.append(ask(client, base_url, prompts[pid]))
            if n % 20 == 0:
                print(f"  {n}/{len(ids)} answered")
    judged = judge_all(answers, prompts, config)
    raw = [
        a.__dict__ | {"score": next((j.score for j in judged if j.id == a.id), 1)} for a in answers
    ]
    (out / "eval_live_answers.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in raw), encoding="utf-8", newline="\n"
    )
    return {"answers": answers, "judged": judged}
