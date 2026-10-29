"""Offline eval: the headline numbers, replayed from stored data. Free and exact.

Every test prompt already has an answer from every tier (M3) and a judge score for each
answer (M4). A routing strategy is just a choice of tier per prompt, so its outcome is
a lookup: the picked tier's judge score and that answer's cost. That makes the eval
cost GBP 0 and gives identical numbers on every run, which is what "reproducible with
one command" needs. The live eval (gearbox.eval.live) checks the replay against the
running service.

Per strategy (always-S/M/L, the oracle, each router):
- quality: mean judge score as a % of always-L's mean score (the spec's measure), and
  the share of answers the judge passes;
- cost: answers at list price plus any cost of routing itself (v2's classifier call),
  as a % of always-L, and per 1,000 requests;
- routing latency p50/p95 (measured when the predictions were made);
- a 95% bootstrap interval for quality % and cost saved, since 119 test prompts is a
  small sample and the headline should say how sure it is.
"""

from __future__ import annotations

import json
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gearbox.config import TIERS, Tier
from gearbox.data.answer import BATCH_DISCOUNT, load_answers
from gearbox.data.judge import PASS_SCORE, load_judgments

BOOTSTRAP_SAMPLES = 2000
SEED = 20261029


@dataclass
class Strategy:
    name: str
    picks: dict[str, Tier]  # prompt id -> tier
    routing_cost: dict[str, float]  # prompt id -> GBP spent deciding (v2 only)
    latency_ms: list[float]  # routing decision times; empty for fixed strategies


@dataclass
class Dataset:
    ids: list[str]
    labels: dict[str, Tier]
    scores: dict[tuple[str, str], int]
    costs: dict[tuple[str, str], float]  # list price, GBP


def load_dataset(data_dir: Path) -> Dataset:
    split = json.loads((data_dir / "split.json").read_text(encoding="utf-8"))
    labels = {}
    with (data_dir / "labels.jsonl").open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            labels[row["id"]] = row["label"]
    return Dataset(
        ids=split["test"],
        labels=labels,
        scores={(j.id, j.tier): j.score for j in load_judgments(data_dir)},
        # Live traffic pays list price; batch answers were billed at half of it.
        costs={
            (a.id, a.tier): a.cost_gbp / (BATCH_DISCOUNT if a.via == "batch" else 1)
            for a in load_answers(data_dir)
        },
    )


def load_strategies(ds: Dataset, predictions: Path) -> list[Strategy]:
    fixed = [Strategy(f"always-{t}", dict.fromkeys(ds.ids, t), {}, []) for t in TIERS]
    oracle = Strategy("oracle", {i: ds.labels[i] for i in ds.ids}, {}, [])
    by_router: dict[str, Strategy] = {}
    with predictions.open(encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            s = by_router.setdefault(p["router"], Strategy(p["router"], {}, {}, []))
            s.picks[p["id"]] = p["tier"]
            s.routing_cost[p["id"]] = p["cost_gbp"]
            s.latency_ms.append(p["latency_ms"])
    for s in by_router.values():
        missing = set(ds.ids) - set(s.picks)
        if missing:
            raise ValueError(f"{s.name}: no prediction for {len(missing)} test prompts")
    return [*fixed, oracle, *sorted(by_router.values(), key=lambda s: s.name)]


def _totals(ds: Dataset, s: Strategy, ids: list[str]) -> tuple[float, float, float]:
    """(sum of judge scores, number passing, GBP cost) over ids."""
    score = passed = cost = 0.0
    for i in ids:
        key = (i, s.picks[i])
        score += ds.scores[key]
        passed += ds.scores[key] >= PASS_SCORE
        cost += ds.costs[key] + s.routing_cost.get(i, 0.0)
    return score, passed, cost


def _pct(lo_hi: list[float]) -> list[float]:
    lo_hi.sort()
    n = len(lo_hi)
    return [round(lo_hi[int(0.025 * n)], 1), round(lo_hi[int(0.975 * n) - 1], 1)]


def evaluate(ds: Dataset, strategies: list[Strategy]) -> dict[str, Any]:
    base = next(s for s in strategies if s.name == "always-L")
    n = len(ds.ids)
    base_score, _, base_cost = _totals(ds, base, ds.ids)

    rng = random.Random(SEED)
    samples = [[rng.choice(ds.ids) for _ in range(n)] for _ in range(BOOTSTRAP_SAMPLES)]
    base_samples = [_totals(ds, base, ids) for ids in samples]

    results: dict[str, Any] = {}
    for s in strategies:
        score, passed, cost = _totals(ds, s, ds.ids)
        quality_ci, saved_ci = [], []
        for ids, (b_score, _, b_cost) in zip(samples, base_samples, strict=True):
            sc, _, co = _totals(ds, s, ids)
            quality_ci.append(100 * sc / b_score)
            saved_ci.append(100 * (1 - co / b_cost))
        lat = sorted(s.latency_ms)
        results[s.name] = {
            "quality_pct": round(100 * score / base_score, 1),
            "quality_pct_ci95": _pct(quality_ci),
            "mean_score": round(score / n, 3),
            "pass_rate": round(passed / n, 4),
            "cost_gbp": round(cost, 5),
            "cost_per_1k_requests_gbp": round(1000 * cost / n, 3),
            "cost_pct": round(100 * cost / base_cost, 1),
            "saved_pct": round(100 * (1 - cost / base_cost), 1),
            "saved_pct_ci95": _pct(saved_ci),
            "route_p50_ms": round(statistics.median(lat), 1) if lat else 0.0,
            "route_p95_ms": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1) if lat else 0.0,
            "picks": {t: sum(1 for i in ds.ids if s.picks[i] == t) for t in TIERS},
        }
    return results


def headline(results: dict[str, Any], router: str, n: int) -> str:
    r = results[router]
    return (
        f"Gearbox ({router}) cuts LLM cost by {r['saved_pct']:.0f}% at "
        f"{r['quality_pct']:.0f}% of quality, against always using the large tier, "
        f"on {n} held-out prompts (95% CI: saved {r['saved_pct_ci95'][0]:.0f}-"
        f"{r['saved_pct_ci95'][1]:.0f}%, quality {r['quality_pct_ci95'][0]:.0f}-"
        f"{r['quality_pct_ci95'][1]:.0f}%)."
    )


def by_task_type(ds: Dataset, data_dir: Path, s: Strategy) -> dict[str, dict[str, float]]:
    """Where a strategy wins or loses: pass rate and cost per task type."""
    types = {}
    with (data_dir / "prompts.jsonl").open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            types[row["id"]] = row["task_type"]
    groups: dict[str, list[str]] = defaultdict(list)
    for i in ds.ids:
        groups[types[i]].append(i)
    out = {}
    for t, ids in sorted(groups.items()):
        _, passed, cost = _totals(ds, s, ids)
        out[t] = {"n": len(ids), "pass_rate": round(passed / len(ids), 3), "cost_gbp": cost}
    return out
