"""Data for the dashboard, as plain functions over the request log and eval results.

Kept out of the Streamlit file so it is tested without a browser, and so the page code
is only layout.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select

from gearbox.service.db import RequestLog

COLUMNS = [
    "created_at",
    "status",
    "router",
    "tier",
    "model",
    "reason",
    "tokens_in",
    "tokens_out",
    "cost_gbp",
    "latency_ms",
    "route_ms",
    "cache_hit",
    "fallback_used",
]


def load_requests(engine: Engine, hours: int, now: datetime | None = None) -> pd.DataFrame:
    since = (now or datetime.now(UTC)) - timedelta(hours=hours)
    cols = [getattr(RequestLog, c) for c in COLUMNS]
    with engine.connect() as conn:
        rows = conn.execute(
            select(*cols).where(RequestLog.created_at >= since).order_by(RequestLog.created_at)
        ).all()
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    return df


def summary(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return {
            "requests": 0,
            "cost_gbp": 0.0,
            "errors": 0,
            "cache_hits": 0,
            "fallbacks": 0,
            "p50_latency_ms": 0.0,
        }
    return {
        "requests": len(df),
        "cost_gbp": float(df["cost_gbp"].sum()),
        "errors": int((df["status"] == "error").sum()),
        "cache_hits": int(df["cache_hit"].sum()),
        "fallbacks": int(df["fallback_used"].sum()),
        "p50_latency_ms": float(df["latency_ms"].median()),
    }


def cost_over_time(df: pd.DataFrame, hours: int) -> pd.DataFrame:
    """GBP per hour (windows up to 2 days) or per day, with empty buckets as 0."""
    if df.empty:
        return pd.DataFrame(columns=["time", "cost_gbp"])
    freq = "h" if hours <= 48 else "D"
    out = df.set_index("created_at")["cost_gbp"].resample(freq).sum().reset_index()
    return out.rename(columns={"created_at": "time"})


def route_split(df: pd.DataFrame) -> pd.DataFrame:
    """Requests and cost per tier (errors without a tier excluded)."""
    ok = df[df["tier"].notna()]
    out = ok.groupby("tier").agg(requests=("tier", "size"), cost_gbp=("cost_gbp", "sum"))
    return out.reindex(["S", "M", "L"], fill_value=0).reset_index()


def load_eval(path: Path) -> pd.DataFrame | None:
    """reports/eval.json as one row per strategy, or None if the eval hasn't been run."""
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    rows = [{"strategy": name, **r} for name, r in data["results"].items()]
    df = pd.DataFrame(rows)
    df["kind"] = df["strategy"].map(
        lambda s: (
            "fixed tier"
            if s.startswith("always-")
            else "oracle"
            if s == "oracle"
            else "Gearbox router"
        )
    )
    return df
