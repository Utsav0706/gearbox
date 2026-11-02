"""Gearbox dashboard: two pages in the sidebar.

    py -m streamlit run src/gearbox/dashboard/app.py --server.port 8502
    (or scripts/dev_dashboard.py, which starts it from the repo root)

- Overview: spend, requests per tier and recent requests from the request log, plus
  the eval's cost-vs-quality chart.
- Try it: type a prompt and see which model Gearbox picked, why, and what it cost.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent

st.set_page_config(page_title="Gearbox", layout="wide")
page = st.navigation(
    [
        st.Page(HERE / "overview.py", title="Overview", default=True),
        st.Page(HERE / "try_it.py", title="Try it"),
    ]
)
page.run()
