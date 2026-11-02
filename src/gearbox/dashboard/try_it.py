"""Try it page: send one prompt to the running API and see how Gearbox handled it.

The page is just another client of the API (see client.py), so it shows the real
routing, cache, budget policy and fallback. Answers are rendered as Markdown with HTML
turned off, so an answer can't inject anything into the page. The last few tries are
kept for this browser session only; nothing is saved.
"""

from __future__ import annotations

import streamlit as st

from gearbox.config import get_secrets
from gearbox.dashboard import client

HISTORY = 10
MODELS = {"auto (router decides)": "auto", "S (local)": "S", "M (Haiku)": "M", "L (Sonnet)": "L"}
MAX_TIERS = {"none": None, "S": "S", "M": "M"}

st.title("Try Gearbox")
base_url = get_secrets().gearbox_url
st.caption(
    f"Sends your prompt to the Gearbox API at {base_url}, like any app would. Each try "
    "costs what the chosen model charges (usually a fraction of a penny); repeats of the "
    "same prompt come from the cache for free."
)

with st.form("try"):
    prompt = st.text_area("Prompt", height=120, placeholder="What is 17 * 23?")
    c1, c2, c3 = st.columns([2, 1, 1])
    model_label = c1.radio("Model", list(MODELS), horizontal=True)
    max_tier_label = c2.selectbox("Max tier", list(MAX_TIERS))
    max_tokens = c3.number_input("Max tokens", min_value=16, max_value=3000, value=1000)
    quality = st.checkbox(
        "Quality mode (cascade): check the answer; if it looks wrong, a bigger model answers",
        help="About 99% of the large model's quality at 42% of its cost on the test set "
        "(reports/cascade.md), but several times the cost of normal mode.",
    )
    sent = st.form_submit_button("Send", type="primary")

if sent:
    if not prompt.strip():
        st.warning("Type a prompt first.")
    else:
        with st.spinner("Waiting for Gearbox..."):
            result = client.ask(
                base_url,
                prompt,
                model=MODELS[model_label],
                max_tier=MAX_TIERS[max_tier_label],
                max_tokens=int(max_tokens),
                cascade=quality,
            )
        history = st.session_state.setdefault("tries", [])
        history.insert(0, (prompt, result))
        del history[HISTORY:]

tries = st.session_state.get("tries", [])
if tries:
    _, r = tries[0]
    if not r.ok:
        st.error(r.error)
    else:
        st.subheader("Answer")
        st.markdown(r.answer)  # Markdown only: HTML in an answer is shown as text
        if r.finish_reason == "length":
            st.caption("The answer was cut off at the token limit.")

        st.subheader("How Gearbox handled it")
        cols = st.columns(4)
        cols[0].metric("Tier", r.tier)
        cols[1].metric("Cost", f"£{r.cost_gbp:.6f}")
        cols[2].metric("Time", f"{r.seconds:.1f} s")
        cols[3].metric("Cache", "hit" if r.cache_hit else "miss")
        st.markdown(f"**Model:** `{r.model}`  \n**Router:** `{r.router}`")
        st.markdown("**Why this tier:**")
        st.code(r.reason or "(no reason given)", language=None, wrap_lines=True)
        st.caption(f"{r.tokens_in} tokens in, {r.tokens_out} tokens out")
        for label, value in (
            ("Quality mode", r.cascade),
            ("Policy", r.policy),
            ("Fallback path", r.fallback),
            ("Note", r.note),
        ):
            if value:
                st.info(f"{label}: {value}")

    if len(tries) > 1 or not tries[0][1].ok:
        st.subheader("Your recent tries")
        st.dataframe(
            [
                {
                    "prompt": p[:60] + ("..." if len(p) > 60 else ""),
                    "tier": t.tier if t.ok else "error",
                    "cost (GBP)": round(t.cost_gbp, 6),
                    "time (s)": round(t.seconds, 1),
                    "cache": "hit" if t.cache_hit else "",
                }
                for p, t in tries
            ],
            hide_index=True,
            use_container_width=True,
        )
