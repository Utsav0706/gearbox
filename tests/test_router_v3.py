"""Router v3 contract. Any rewrite of gearbox/routers/embed.py must keep these passing.

Uses a fake embedder (no model download), so it runs in CI in milliseconds.
"""

import asyncio
import hashlib

import numpy as np
import pytest

from gearbox.routers.embed import (
    EmbedRouter,
    Weights,
    cross_validate,
    decide,
    fit,
    load_artifact,
    routing_cost,
    save_artifact,
)


class FakeEmbedder:
    """Deterministic 8-dim unit vectors; prompts mentioning 'hard' point one way."""

    name = "fake"

    def encode(self, texts):
        rows = []
        for t in texts:
            seed = int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)
            v = np.random.default_rng(seed).normal(size=8) * 0.1
            v[0] += 3.0 if "hard" in t else -3.0 if "easy" in t else 0.0
            rows.append(v / np.linalg.norm(v))
        return np.asarray(rows)


def test_decide_cheapest_tier_reaching_threshold():
    p = np.array([0.55, 0.35, 0.10])
    assert decide(p, 0.5) == 0  # P(S) alone is enough
    assert decide(p, 0.6) == 1  # needs S+M = 0.90
    assert decide(p, 0.95) == 2
    assert decide(np.array([0.0, 0.0, 1.0]), 0.5) == 2


def test_higher_threshold_never_routes_lower():
    rng = np.random.default_rng(0)
    for _ in range(200):
        p = rng.dirichlet([1, 1, 1])
        assert decide(p, 0.4) <= decide(p, 0.6) <= decide(p, 0.8)


def test_proba_matches_sklearn():
    from sklearn.linear_model import LogisticRegression

    rng = np.random.default_rng(1)
    x = rng.normal(size=(60, 5))
    y = np.array([0, 1, 2] * 20)
    model = LogisticRegression(max_iter=2000).fit(x, y)
    w = Weights(model.coef_, model.intercept_, 0.5)
    assert np.allclose(w.proba(x), model.predict_proba(x), atol=1e-8)


def test_routing_cost_weights_under_routing_double():
    y = np.array([1, 1, 1, 1])
    assert routing_cost(y, np.array([1, 1, 1, 1])) == 0
    assert routing_cost(y, np.array([2, 1, 1, 1])) == pytest.approx(0.25)  # one over
    assert routing_cost(y, np.array([0, 1, 1, 1])) == pytest.approx(0.5)  # one under


def _data(n=30):
    texts = [f"easy question {i}" for i in range(n)] + [f"medium question {i}" for i in range(n)]
    texts += [f"hard question {i}" for i in range(n)]
    y = np.array([0] * n + [1] * n + [2] * n)
    return texts, y


def test_learns_separable_data_and_routes():
    texts, y = _data()
    emb = FakeEmbedder()
    w = fit(emb.encode(texts), y, 1.0, None)
    router = EmbedRouter(emb, w)
    assert asyncio.run(router.route("an easy one")).tier == "S"
    route = asyncio.run(router.route("a hard one"))
    assert route.tier == "L"
    assert route.reason.startswith("embed: P(S)=")


def test_cross_validation_ranks_by_cost():
    texts, y = _data(15)
    results = cross_validate(FakeEmbedder().encode(texts), y)
    costs = [r["cost"] for r in results]
    assert costs == sorted(costs)
    assert results[0]["cost"] < 0.2


def test_artifact_round_trip(tmp_path):
    w = Weights(np.arange(6, dtype=float).reshape(3, 2) / 7, np.array([0.1, -0.2, 0.3]), 0.6)
    path = tmp_path / "r.json"
    save_artifact(path, w, {"embedding_model": "fake"})
    loaded, meta = load_artifact(path)
    assert meta["embedding_model"] == "fake"
    assert loaded.threshold == 0.6
    assert np.allclose(loaded.coef, w.coef, atol=1e-6)
    path.write_text('{"format": "something-else"}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a router v3 artifact"):
        load_artifact(path)
