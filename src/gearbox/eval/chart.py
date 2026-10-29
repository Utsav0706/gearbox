"""The cost-vs-quality chart (reports/cost_quality.svg), drawn as plain SVG.

Plain SVG rather than a plotting library: the chart is one scatter of a few points, it
must render on GitHub (an image, so no hover), and it follows the viewer's light or dark
theme through a CSS media query inside the file. One less dependency to install.

Encoding: x = cost as % of always-L on a log scale (costs span 2.5% to 100%; always-S
costs nothing and sits on its own "free" tick at the left), y = quality as % of
always-L. Colour says what kind of strategy a point is (router / fixed baseline /
oracle); every point is labelled directly, so colour is never the only cue.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any

W, H = 760, 460
LEFT, RIGHT, TOP, BOTTOM = 84, 150, 92, 64
X_MIN, X_MAX = 1.0, 100.0  # % of always-L cost, log scale
FREE_X = LEFT + 6  # zero-cost points get their own column, left of the log axis
X0 = LEFT + 50  # where the log axis (the 1% tick) starts
Y_MIN, Y_MAX = 50.0, 102.0
CHAR_W, LINE_H, DOT_R = 7.0, 14.0, 6.0

STYLE = """
.surface { fill: #fcfcfb; } .ink { fill: #0b0b0b; } .ink2 { fill: #52514e; }
.muted { fill: #898781; } .grid { stroke: #e1e0d9; } .axis { stroke: #c3c2b7; }
.ring { stroke: #fcfcfb; }
.router { fill: #2a78d6; } .baseline { fill: #eb6834; } .oracle { fill: #1baf7a; }
@media (prefers-color-scheme: dark) {
  .surface { fill: #1a1a19; } .ink { fill: #f0efec; } .ink2 { fill: #c3c2b7; }
  .grid { stroke: #2c2c2a; } .axis { stroke: #383835; } .ring { stroke: #1a1a19; }
  .router { fill: #3987e5; } .baseline { fill: #d95926; } .oracle { fill: #199e70; }
}
text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.tick { font-size: 12px; font-variant-numeric: tabular-nums; }
"""


def kind(name: str) -> str:
    if name.startswith("always-"):
        return "baseline"
    return "oracle" if name == "oracle" else "router"


def x_of(cost_pct: float) -> float:
    if cost_pct <= 0:
        return FREE_X
    c = min(max(cost_pct, X_MIN), X_MAX)
    span = math.log10(X_MAX) - math.log10(X_MIN)
    return X0 + (math.log10(c) - math.log10(X_MIN)) / span * (W - RIGHT - X0)


def y_of(quality_pct: float) -> float:
    q = min(max(quality_pct, Y_MIN), Y_MAX)
    return TOP + (Y_MAX - q) / (Y_MAX - Y_MIN) * (H - TOP - BOTTOM)


Box = tuple[float, float, float, float]  # x0, y0, x1, y1


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def place_labels(
    points: list[tuple[str, float, float]],
) -> dict[str, tuple[float, float, str]]:
    """(x, y, text-anchor) per label. Tries right, left, above, below each point and
    keeps the first spot that covers no other point and no label already placed."""
    obstacles: list[Box] = [(x - DOT_R, y - DOT_R, x + DOT_R, y + DOT_R) for _, x, y in points]
    out = {}
    for name, px, py in sorted(points, key=lambda p: (p[1], p[2])):
        w = CHAR_W * len(name)
        candidates = [
            (px + 10, py + 4, "start", (px + 9, py - 9, px + 10 + w, py + 5)),
            (px - 10, py + 4, "end", (px - 10 - w, py - 9, px - 9, py + 5)),
            (px, py - 12, "middle", (px - w / 2, py - 24, px + w / 2, py - 9)),
            (px, py + 22, "middle", (px - w / 2, py + 10, px + w / 2, py + 25)),
        ]
        own = (px - DOT_R, py - DOT_R, px + DOT_R, py + DOT_R)
        free = [
            c
            for c in candidates
            if c[3][0] >= 0
            and c[3][2] <= W
            and not any(_overlaps(c[3], o) for o in obstacles if o != own)
        ]
        x, y, anchor, box = (free or candidates)[0]  # crowded everywhere: to the right
        obstacles.append(box)
        out[name] = (x, y, anchor)
    return out


def cost_quality_svg(results: dict[str, Any], n_prompts: int) -> str:
    plot_right = W - RIGHT
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" aria-labelledby="t d">',
        f"<style>{STYLE}</style>",
        '<title id="t">Cost versus quality of each routing strategy</title>',
        f'<desc id="d">Quality and cost as a percentage of always using the large tier, on '
        f"{n_prompts} held-out prompts. Full numbers are in reports/eval.md.</desc>",
        f'<rect class="surface" width="{W}" height="{H}" rx="8"/>',
        '<text class="ink" x="20" y="30" font-size="16" font-weight="600">'
        "Cost vs quality, per routing strategy</text>",
        f'<text class="ink2" x="20" y="50" font-size="12">{n_prompts} held-out prompts. Both '
        "axes relative to always using the large tier (Claude Sonnet 5.5) = 100%.</text>",
    ]
    # legend: a swatch dot + secondary ink text per kind
    lx = 20
    for k, label in (
        ("router", "Gearbox router"),
        ("baseline", "fixed tier"),
        ("oracle", "oracle (perfect labels)"),
    ):
        parts.append(f'<circle class="{k}" cx="{lx + 5}" cy="70" r="5"/>')
        parts.append(f'<text class="ink2" x="{lx + 15}" y="74" font-size="12">{label}</text>')
        lx += 30 + 7 * len(label)

    # gridlines + ticks
    for q in range(50, 101, 10):
        y = y_of(q)
        parts.append(
            f'<line class="grid" x1="{LEFT}" x2="{plot_right}" y1="{y:.1f}" '
            f'y2="{y:.1f}" stroke-width="1"/>'
        )
        parts.append(
            f'<text class="muted tick" x="{LEFT - 8}" y="{y + 4:.1f}" text-anchor="end">{q}%</text>'
        )
    for c in (1, 3, 10, 30, 100):
        x = x_of(c)
        parts.append(
            f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{TOP}" '
            f'y2="{H - BOTTOM}" stroke-width="1"/>'
        )
        parts.append(
            f'<text class="muted tick" x="{x:.1f}" y="{H - BOTTOM + 18}" '
            f'text-anchor="middle">{c}%</text>'
        )
    parts.append(
        f'<text class="muted tick" x="{FREE_X:.1f}" y="{H - BOTTOM + 18}" '
        f'text-anchor="middle">free</text>'
    )
    parts.append(
        f'<line class="axis" x1="{LEFT}" x2="{plot_right}" y1="{H - BOTTOM}" '
        f'y2="{H - BOTTOM}" stroke-width="1"/>'
    )
    parts.append(
        f'<text class="ink2" x="{(LEFT + plot_right) / 2:.0f}" y="{H - 18}" '
        'font-size="12" text-anchor="middle">cost (log scale)</text>'
    )
    parts.append(
        f'<text class="ink2" font-size="12" text-anchor="middle" '
        f'transform="translate(26 {(TOP + H - BOTTOM) / 2:.0f}) rotate(-90)">'
        "quality (mean judge score)</text>"
    )

    points = [(name, x_of(r["cost_pct"]), y_of(r["quality_pct"])) for name, r in results.items()]
    labels = place_labels(points)
    for name, px, py in points:
        parts.append(
            f'<circle class="{kind(name)} ring" cx="{px:.1f}" cy="{py:.1f}" r="6" '
            'stroke-width="2"/>'
        )
    for name, _, _ in points:
        x, y, anchor = labels[name]
        parts.append(
            f'<text class="ink" x="{x:.1f}" y="{y:.1f}" font-size="12" '
            f'text-anchor="{anchor}">{escape(name)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
