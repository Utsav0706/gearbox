"""Eval maths, chart and dashboard data on small synthetic data: no network, no cost."""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import pytest

from gearbox.eval import chart
from gearbox.eval.live import LiveAnswer, summarise
from gearbox.eval.offline import Dataset, Strategy, evaluate, headline

IDS = [f"p{i}" for i in range(10)]


def _dataset():
    # L always scores 5 and costs 1.0; M scores 4 and costs 0.1; S scores 2 and is free.
    scores, costs = {}, {}
    for i in IDS:
        for tier, s, c in (("S", 2, 0.0), ("M", 4, 0.1), ("L", 5, 1.0)):
            scores[(i, tier)] = s
            costs[(i, tier)] = c
    return Dataset(IDS, dict.fromkeys(IDS, "M"), scores, costs)


def _fixed(tier):
    return Strategy(f"always-{tier}", dict.fromkeys(IDS, tier), {}, [])


def test_evaluate_quality_cost_and_intervals():
    ds = _dataset()
    router = Strategy(
        "r",
        {i: ("M" if n < 5 else "L") for n, i in enumerate(IDS)},
        dict.fromkeys(IDS, 0.01),
        [5.0] * 10,
    )
    res = evaluate(ds, [_fixed("S"), _fixed("M"), _fixed("L"), router])
    assert res["always-L"]["quality_pct"] == 100.0 and res["always-L"]["saved_pct"] == 0.0
    assert res["always-M"]["quality_pct"] == 80.0  # 4 / 5
    assert res["always-M"]["cost_pct"] == 10.0
    assert res["always-M"]["pass_rate"] == 1.0  # a 4 passes
    assert res["always-S"]["pass_rate"] == 0.0
    # router: half M (4), half L (5) -> 90% quality; cost (5*0.1 + 5*1.0 + 10*0.01) / 10
    assert res["r"]["quality_pct"] == 90.0
    assert res["r"]["cost_pct"] == pytest.approx(56.0)
    assert res["r"]["picks"] == {"S": 0, "M": 5, "L": 5}
    lo, hi = res["r"]["quality_pct_ci95"]
    assert lo <= 90.0 <= hi
    assert res["r"]["route_p50_ms"] == 5.0


def test_evaluate_is_reproducible():
    ds = _dataset()
    strategies = [_fixed("M"), _fixed("L")]
    assert evaluate(ds, strategies) == evaluate(ds, strategies)


def test_headline_wording():
    ds = _dataset()
    res = evaluate(ds, [_fixed("M"), _fixed("L")])
    text = headline(res, "always-M", len(IDS))
    assert text.startswith("Gearbox (always-M) cuts LLM cost by 90% at 80% of quality")
    assert "10 held-out prompts" in text


def test_chart_is_valid_svg_with_every_strategy_labelled():
    ds = _dataset()
    res = evaluate(ds, [_fixed("S"), _fixed("M"), _fixed("L")])
    svg = chart.cost_quality_svg(res, len(IDS))
    root = ET.fromstring(svg)
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert {"always-S", "always-M", "always-L"} <= set(texts)
    assert "prefers-color-scheme: dark" in svg  # follows the viewer's theme


def test_labels_avoid_points_and_each_other():
    # two points almost on top of each other, like always-M and v3 in the real data
    points = [("always-M", 300.0, 150.0), ("v3-embed", 312.0, 150.0), ("oracle", 380.0, 120.0)]
    placed = chart.place_labels(points)
    boxes = []
    for name, (x, y, anchor) in placed.items():
        w = chart.CHAR_W * len(name)
        x0 = {"start": x, "end": x - w, "middle": x - w / 2}[anchor]
        boxes.append((name, (x0, y - 11, x0 + w, y + 2)))
    for i, (a, box_a) in enumerate(boxes):
        for b, box_b in boxes[i + 1 :]:
            assert not chart._overlaps(box_a, box_b), (a, b)
        for name, px, py in points:
            dot = (px - 5, py - 5, px + 5, py + 5)
            if name != a:
                assert not chart._overlaps(box_a, dot), (a, name)


def test_live_summary():
    offline = {"always-L": {"mean_score": 5.0, "cost_gbp": 1.0}}
    answers = [
        LiveAnswer("a", "M", "v3-embed", "x", 0.01, 900, False, None),
        LiveAnswer("b", "L", "v3-embed", "y", 0.09, 2100, False, "M->M->L"),
        LiveAnswer("c", "M", "cache", "", 0.0, 10, True, None),  # empty answer -> scores 1
    ]

    class J:
        def __init__(self, id_, score):
            self.id, self.score, self.cost_gbp = id_, score, 0.002

    out = summarise(answers, [J("a", 5), J("b", 3)], offline)
    assert out["mean_score"] == pytest.approx(3.0)
    assert out["quality_pct"] == 60.0
    assert out["pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert out["saved_pct"] == 90.0
    assert out["picks"] == {"S": 0, "M": 2, "L": 1}
    assert out["cache_hits"] == 1 and out["fallbacks"] == 1


# --- dashboard data -----------------------------------------------------------------------


def test_dashboard_data(tmp_path):
    from gearbox.dashboard.data import cost_over_time, load_eval, load_requests, route_split
    from gearbox.dashboard.data import summary as dash_summary
    from gearbox.service.db import Base, RequestLog, make_engine, make_sessions

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    rows = [
        ("a", now - timedelta(hours=1), "ok", "M", 0.01, False),
        ("b", now - timedelta(hours=2), "ok", "S", 0.0, True),
        ("c", now - timedelta(hours=2), "error", None, 0.0, False),
        ("d", now - timedelta(days=3), "ok", "L", 0.5, False),  # outside a 24 h window
    ]
    with make_sessions(engine).begin() as s:
        for rid, when, status, tier, cost, hit in rows:
            s.add(
                RequestLog(
                    request_id=rid,
                    created_at=when,
                    status=status,
                    router="r",
                    tier=tier,
                    reason="x",
                    cost_gbp=cost,
                    latency_ms=100,
                    route_ms=1,
                    cache_hit=hit,
                )
            )
    df = load_requests(engine, 24, now=now)
    assert len(df) == 3
    sm = dash_summary(df)
    assert sm["requests"] == 3 and sm["errors"] == 1 and sm["cache_hits"] == 1
    assert sm["cost_gbp"] == pytest.approx(0.01)
    split = route_split(df).set_index("tier")["requests"].to_dict()
    assert split == {"S": 1, "M": 1, "L": 0}
    assert cost_over_time(df, 24)["cost_gbp"].sum() == pytest.approx(0.01)
    assert dash_summary(df.iloc[0:0])["requests"] == 0

    assert load_eval(tmp_path / "missing.json") is None
    (tmp_path / "eval.json").write_text(
        '{"results": {"always-L": {"cost_pct": 100}, "oracle": {"cost_pct": 10}, '
        '"v3-embed": {"cost_pct": 7}}}',
        encoding="utf-8",
    )
    kinds = load_eval(tmp_path / "eval.json").set_index("strategy")["kind"].to_dict()
    assert kinds == {"always-L": "fixed tier", "oracle": "oracle", "v3-embed": "Gearbox router"}


def test_dashboard_page_renders(tmp_path, monkeypatch):
    """The Streamlit page runs end to end on an empty log (AppTest, no browser)."""
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from gearbox.config import get_secrets
    from gearbox.service.db import Base, make_engine

    db = tmp_path / "log.db"
    Base.metadata.create_all(make_engine(f"sqlite:///{db}"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.chdir(tmp_path)  # no reports/eval.json here
    get_secrets.cache_clear()
    app_path = Path(__file__).resolve().parents[1] / "src/gearbox/dashboard/app.py"
    try:
        at = AppTest.from_file(str(app_path), default_timeout=30).run()
    finally:
        get_secrets.cache_clear()
    assert not at.exception
    assert at.title[0].value == "Gearbox"
    assert [m.value for m in at.metric][0] == "0"
    assert any("No requests" in i.value for i in at.info)
