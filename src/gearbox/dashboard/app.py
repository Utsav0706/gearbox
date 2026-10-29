"""Gearbox dashboard: what the service spent, where requests went, and the eval result.

    py -m streamlit run src/gearbox/dashboard/app.py --server.port 8502
    (or scripts/dev_dashboard.py, which starts it from the repo root)

Reads the same DATABASE_URL as the API (request log) and reports/eval.json.
Read-only: the dashboard never writes to the log.
"""

from __future__ import annotations

from pathlib import Path

import altair as alt
import streamlit as st

from gearbox.config import get_secrets
from gearbox.dashboard.data import (
    cost_over_time,
    load_eval,
    load_requests,
    route_split,
    summary,
)
from gearbox.service.db import make_engine

# Categorical slots 1-3 of the reference palette, fixed per entity (never by rank).
TIER_COLOURS = {"S": "#2a78d6", "M": "#eb6834", "L": "#1baf7a"}
KIND_COLOURS = {"Gearbox router": "#2a78d6", "fixed tier": "#eb6834", "oracle": "#1baf7a"}
MUTED_INK = "#898781"  # palette muted ink: same on light and dark surfaces
WINDOWS = {"Last 24 hours": 24, "Last 7 days": 24 * 7, "Last 30 days": 24 * 30}


@st.cache_resource
def engine():
    return make_engine(get_secrets().database_url)


st.set_page_config(page_title="Gearbox", layout="wide")
st.title("Gearbox")
window = st.radio("Window", list(WINDOWS), horizontal=True, key="window")
hours = WINDOWS[window]
df = load_requests(engine(), hours)
s = summary(df)

cols = st.columns(5)
cols[0].metric("Requests", f"{s['requests']:,}")
cols[1].metric("Spend", f"£{s['cost_gbp']:.4f}")
cols[2].metric("Cache hits", f"{s['cache_hits']:,}")
cols[3].metric("Fallbacks", f"{s['fallbacks']:,}")
cols[4].metric("Errors", f"{s['errors']:,}")

if df.empty:
    st.info("No requests in this window yet. Send some to the API on port 8002.")
else:
    left, right = st.columns(2)
    with left:
        st.subheader("Spend over time")
        line = (
            alt.Chart(cost_over_time(df, hours))
            .mark_line(strokeWidth=2, color=TIER_COLOURS["S"])
            .encode(
                x=alt.X("time:T", title=None),
                y=alt.Y("cost_gbp:Q", title="GBP"),
                tooltip=[alt.Tooltip("time:T"), alt.Tooltip("cost_gbp:Q", format=".5f")],
            )
        )
        st.altair_chart(line, use_container_width=True)
    with right:
        st.subheader("Requests per tier")
        split = route_split(df)
        bars = (
            alt.Chart(split)
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                x=alt.X("requests:Q", title="requests"),
                y=alt.Y("tier:N", sort=["S", "M", "L"], title=None),
                color=alt.Color(
                    "tier:N",
                    scale=alt.Scale(domain=list(TIER_COLOURS), range=list(TIER_COLOURS.values())),
                    legend=None,
                ),
                tooltip=["tier", "requests", alt.Tooltip("cost_gbp:Q", format=".5f")],
            )
        )
        st.altair_chart(bars, use_container_width=True)
    with st.expander("Recent requests"):
        st.dataframe(
            df.sort_values("created_at", ascending=False).head(200),
            hide_index=True,
            use_container_width=True,
        )

st.subheader("Cost vs quality (eval)")
ev = load_eval(Path("reports/eval.json"))
if ev is None:
    st.info("Run `py -m gearbox.eval` to create reports/eval.json.")
else:
    st.caption(
        "Held-out test prompts; both axes relative to always using the large tier (100%). "
        "Cost is on a log scale; always-S (free) is left off the cost axis and listed below."
    )
    plot = ev[ev["cost_pct"] > 0]
    base = alt.Chart(plot).encode(
        x=alt.X("cost_pct:Q", scale=alt.Scale(type="log"), title="cost (% of always-L)"),
        y=alt.Y("quality_pct:Q", scale=alt.Scale(domain=[50, 102]), title="quality (%)"),
    )
    points = base.mark_circle(size=120, opacity=1, stroke="white", strokeWidth=2).encode(
        color=alt.Color(
            "kind:N",
            scale=alt.Scale(domain=list(KIND_COLOURS), range=list(KIND_COLOURS.values())),
            legend=alt.Legend(title=None, orient="top"),
        ),
        tooltip=["strategy", "quality_pct", "cost_pct", "pass_rate", "picks"],
    )
    # Labels in muted ink (readable on light and dark themes), never the series colour.
    # Fixed tiers labelled on the left, routers and the oracle on the right: always-M
    # and the v3 router sit almost on top of each other.
    fixed = alt.datum.kind == "fixed tier"
    left = (
        base.transform_filter(fixed)
        .mark_text(align="right", dx=-9, dy=-8, color=MUTED_INK)
        .encode(text="strategy:N")
    )
    right = (
        base.transform_filter(~fixed)
        .mark_text(align="left", dx=9, dy=-8, color=MUTED_INK)
        .encode(text="strategy:N")
    )
    st.altair_chart(points + left + right, use_container_width=True)
    table = ev.assign(pass_pct=(100 * ev["pass_rate"]).round(1))
    st.dataframe(
        table[
            [
                "strategy",
                "quality_pct",
                "pass_pct",
                "cost_pct",
                "saved_pct",
                "cost_per_1k_requests_gbp",
                "route_p50_ms",
            ]
        ],
        hide_index=True,
        use_container_width=True,
        column_config={
            "quality_pct": st.column_config.NumberColumn("quality %", format="%.1f"),
            "pass_pct": st.column_config.NumberColumn("pass %", format="%.1f"),
            "cost_pct": st.column_config.NumberColumn("cost % of L", format="%.1f"),
            "saved_pct": st.column_config.NumberColumn("saved %", format="%.1f"),
            "cost_per_1k_requests_gbp": st.column_config.NumberColumn(
                "GBP per 1k requests", format="%.3f"
            ),
            "route_p50_ms": st.column_config.NumberColumn("routing p50 ms", format="%.0f"),
        },
    )
