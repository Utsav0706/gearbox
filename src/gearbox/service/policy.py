"""Policy: limits applied after the router has chosen, before any model is called.

Two limits, the tighter one wins:
- x-gearbox-max-tier (request header): the caller's own ceiling, e.g. "M" for a feature
  that must never use the expensive model.
- The monthly budget (config.yaml policy.monthly_budget_gbp): once this calendar
  month's logged spend reaches it, every request is downgraded to tier S (free, local)
  until the month ends. The response says so, so the caller is never silently served
  a weaker model.

The cap also bounds the fallback: a request downgraded for budget never falls back to a
paid tier, which would spend past the budget the cap exists to protect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gearbox.config import TIERS, Tier


@dataclass
class Decision:
    tier: Tier
    cap: Tier  # the highest tier this request may use, fallback included
    notes: list[str] = field(default_factory=list)  # each limit that changed the route


def parse_max_tier(value: str | None) -> Tier | None:
    if value is None or not value.strip():
        return None
    v = value.strip().upper()
    if v not in TIERS:
        raise ValueError(f"x-gearbox-max-tier must be S, M or L, got {value!r}")
    return v  # type: ignore[return-value]


def apply(
    tier: Tier, max_tier: Tier | None, month_spend_gbp: float, monthly_budget_gbp: float | None
) -> Decision:
    cap: Tier = "L"
    notes: list[str] = []
    if max_tier is not None and TIERS.index(max_tier) < TIERS.index(cap):
        cap = max_tier
        if TIERS.index(tier) > TIERS.index(max_tier):
            notes.append(f"capped at {max_tier} by x-gearbox-max-tier")
    if monthly_budget_gbp is not None and month_spend_gbp >= monthly_budget_gbp:
        cap = "S"
        if tier != "S":
            notes.append(
                f"monthly budget reached (GBP {month_spend_gbp:.2f} of "
                f"{monthly_budget_gbp:.2f}): downgraded to S"
            )
    if TIERS.index(tier) > TIERS.index(cap):
        tier = cap
    return Decision(tier, cap, notes)
