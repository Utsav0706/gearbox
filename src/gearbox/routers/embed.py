"""Router v3: sentence embedding + logistic regression. The expected winner.

How a prompt is routed:
1. Embed it with a small sentence-transformer (bge-small-en-v1.5: 384 numbers, ~20 ms
   on CPU, free, runs locally, already used by Scout).
2. Logistic regression turns those 384 numbers into P(S), P(M), P(L).
3. Pick the cheapest tier whose cumulative probability reaches the threshold:
   S if P(S) >= t, else M if P(S) + P(M) >= t, else L. A higher t sends more prompts to
   bigger tiers: fewer under-routes (bad answers), more over-routes (wasted money).

Why logistic regression and not a fine-tuned BERT: 359 training examples are far too
few to fine-tune a transformer, LR trains in a second, its probabilities are usable for
the threshold above, and its weights are a small, readable JSON file. (Pitwall will try
a fine-tuned small LLM later and has to beat this.)

Training picks C, class weighting and the threshold by 5-fold cross-validation on the
training split only, scoring each setting by routing cost: an under-route counts as two
mistakes, an over-route as one, because a bad answer is worse than a wasted penny.

    py -m gearbox.routers.embed train      # writes models/router_v3.json

tests/test_router_v3.py is the contract: any rewrite of this module must keep it passing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from gearbox.config import TIERS, AppConfig
from gearbox.routers import Route, register

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
UNDER_ROUTE_COST = 2.0  # an under-route counts as this many mistakes; an over-route as 1
GRID = {
    "C": [0.1, 0.3, 1.0, 3.0, 10.0],
    "class_weight": [None, "balanced"],
    "threshold": [0.4, 0.5, 0.6, 0.7],
}
FOLDS = 5
SEED = 20261029


class Embedder(Protocol):
    name: str

    def encode(self, texts: list[str]) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    """Loads the model on first use (a second or two), then ~20 ms per prompt on CPU."""

    def __init__(self, name: str = EMBEDDING_MODEL) -> None:
        self.name = name
        self._model: Any = None

    def encode(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.name, device="cpu")
        # normalised, so every prompt is a unit vector and long prompts don't dominate
        return np.asarray(
            self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        )


# --- the model ----------------------------------------------------------------------------


@dataclass
class Weights:
    coef: np.ndarray  # (3, dim): one row per tier, in TIERS order (S, M, L)
    intercept: np.ndarray  # (3,)
    threshold: float

    def proba(self, x: np.ndarray) -> np.ndarray:
        """Softmax of the linear scores: the same numbers sklearn's predict_proba gives."""
        z = x @ self.coef.T + self.intercept
        z = z - z.max(axis=-1, keepdims=True)  # numerical stability
        e = np.exp(z)
        return e / e.sum(axis=-1, keepdims=True)


def decide(p: np.ndarray, threshold: float) -> int:
    """Index of the cheapest tier whose cumulative probability reaches the threshold."""
    cumulative = np.cumsum(p)
    return int(np.argmax(cumulative >= threshold - 1e-12))


class EmbedRouter:
    name = "v3-embed"

    def __init__(self, embedder: Embedder, weights: Weights) -> None:
        self._embedder = embedder
        self._weights = weights

    def route_sync(self, prompt: str) -> Route:
        x = self._embedder.encode([prompt])[0]
        p = self._weights.proba(x)
        tier = TIERS[decide(p, self._weights.threshold)]
        probs = " ".join(f"P({t})={v:.2f}" for t, v in zip(TIERS, p, strict=True))
        return Route(tier, f"embed: {probs}, threshold {self._weights.threshold}")

    async def route(self, prompt: str) -> Route:
        # Embedding is CPU-bound: run it off the event loop so other requests keep moving.
        return await asyncio.to_thread(self.route_sync, prompt)


# --- artifact (JSON, not pickle: loading it cannot run code, and it diffs in git) ---------


def save_artifact(path: Path, weights: Weights, meta: dict[str, Any]) -> None:
    artifact = {
        "format": "gearbox-router-v3/1",
        "classes": list(TIERS),
        "threshold": weights.threshold,
        "intercept": [round(float(v), 6) for v in weights.intercept],
        "coef": [[round(float(v), 6) for v in row] for row in weights.coef],
        **meta,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8", newline="\n")


def load_artifact(path: Path) -> tuple[Weights, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != "gearbox-router-v3/1" or data.get("classes") != list(TIERS):
        raise ValueError(f"{path} is not a router v3 artifact for tiers {TIERS}")
    weights = Weights(
        coef=np.asarray(data["coef"], dtype=float),
        intercept=np.asarray(data["intercept"], dtype=float),
        threshold=float(data["threshold"]),
    )
    return weights, data


@register("v3-embed")
def _build(config: AppConfig) -> EmbedRouter:
    weights, meta = load_artifact(config.routing.v3_embed.artifact)
    return EmbedRouter(SentenceTransformerEmbedder(meta["embedding_model"]), weights)


# --- training -----------------------------------------------------------------------------


def routing_cost(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean cost per prompt: 0 if right, 1 if over-routed, UNDER_ROUTE_COST if under."""
    under = (y_pred < y_true).sum()
    over = (y_pred > y_true).sum()
    return float((UNDER_ROUTE_COST * under + over) / len(y_true))


def fit(x: np.ndarray, y: np.ndarray, c: float, class_weight: str | None) -> Weights:
    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(C=c, class_weight=class_weight, max_iter=5000)
    model.fit(x, y)
    if list(model.classes_) != [0, 1, 2]:
        raise ValueError("training data must contain all three tiers")
    return Weights(model.coef_.copy(), model.intercept_.copy(), threshold=0.5)


def cross_validate(x: np.ndarray, y: np.ndarray) -> list[dict[str, Any]]:
    """Every grid setting, scored by mean routing cost over stratified folds."""
    from sklearn.model_selection import StratifiedKFold

    folds = list(StratifiedKFold(FOLDS, shuffle=True, random_state=SEED % 2**32).split(x, y))
    results = []
    for c, cw in itertools.product(GRID["C"], GRID["class_weight"]):
        fold_probs = []
        for train_idx, val_idx in folds:
            w = fit(x[train_idx], y[train_idx], c, cw)
            fold_probs.append((val_idx, w.proba(x[val_idx])))
        for t in GRID["threshold"]:
            pred = np.empty_like(y)
            for val_idx, p in fold_probs:
                pred[val_idx] = [decide(row, t) for row in p]
            results.append(
                {
                    "C": c,
                    "class_weight": cw,
                    "threshold": t,
                    "cost": round(routing_cost(y, pred), 4),
                    "accuracy": round(float((pred == y).mean()), 4),
                    "under_rate": round(float((pred < y).mean()), 4),
                }
            )
    # Lowest cost wins; ties go to the simpler (more regularised) model.
    return sorted(results, key=lambda r: (r["cost"], r["C"]))


def embed_cached(texts: list[str], embedder: Embedder, cache_dir: Path) -> np.ndarray:
    """Embeddings for a list of texts, cached on disk by content hash (runs/, not git)."""
    key = hashlib.sha256((embedder.name + "\x00" + "\x00".join(texts)).encode()).hexdigest()
    path = cache_dir / f"emb-{key[:16]}.npy"
    if path.exists():
        return np.load(path)
    x = embedder.encode(texts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.save(path, x)
    return x


def load_split(data_dir: Path, part: str) -> tuple[list[str], list[str], np.ndarray]:
    """(ids, prompt texts, labels as 0/1/2) for the train or test split."""
    prompts = {}
    with (data_dir / "prompts.jsonl").open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            prompts[row["id"]] = row["prompt"]
    labels = {}
    with (data_dir / "labels.jsonl").open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            labels[row["id"]] = TIERS.index(row["label"])
    ids = json.loads((data_dir / "split.json").read_text(encoding="utf-8"))[part]
    return ids, [prompts[i] for i in ids], np.asarray([labels[i] for i in ids])


def train(
    data_dir: Path, out: Path, embedder: Embedder, cache_dir: Path = Path("runs/cache")
) -> dict[str, Any]:
    _, texts, y = load_split(data_dir, "train")
    x = embed_cached(texts, embedder, cache_dir)
    grid = cross_validate(x, y)
    best = grid[0]
    weights = fit(x, y, best["C"], best["class_weight"])
    weights.threshold = best["threshold"]
    labels_sha = hashlib.sha256((data_dir / "labels.jsonl").read_bytes()).hexdigest()
    meta = {
        "embedding_model": embedder.name,
        "trained_on": {
            "data": data_dir.as_posix(),
            "split": "train",
            "examples": int(len(y)),
            "labels_sha256": labels_sha,
        },
        "hyperparameters": {"C": best["C"], "class_weight": best["class_weight"]},
        "cross_validation": {
            "folds": FOLDS,
            "under_route_cost": UNDER_ROUTE_COST,
            "best": best,
            "top5": grid[:5],
        },
    }
    save_artifact(out, weights, meta)
    return meta


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="py -m gearbox.routers.embed",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("cmd", choices=["train"])
    parser.add_argument("--data", type=Path, default=Path("data/v1"))
    parser.add_argument("--out", type=Path, default=Path("models/router_v3.json"))
    args = parser.parse_args(argv)
    meta = train(args.data, args.out, SentenceTransformerEmbedder())
    best = meta["cross_validation"]["best"]
    print(f"wrote {args.out}")
    print(f"chosen by {FOLDS}-fold CV on {meta['trained_on']['examples']} training prompts:")
    for k in ("C", "class_weight", "threshold", "cost", "accuracy", "under_rate"):
        print(f"  {k:<13} {best[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
