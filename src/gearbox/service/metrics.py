"""Prometheus metrics (GET /metrics), counted from the request log itself.

Every request, on both endpoints and on every failure path, already writes one
RequestLog row. The metrics are counted when those rows are saved (a listener on this
app's session factory), so they can never disagree with the log, and no endpoint can
forget to report.

- gearbox_requests_total{router, tier, status, cache_hit, fallback, escalated}:
  traffic, tier mix, errors; by router, so a canary split (e.g. Pitwall's v4-finetuned
  vs v4-candidate) shows up as two series.
- gearbox_cost_gbp_total{tier, model}: money spent. Cost per 1k requests =
  rate(cost) / rate(requests) * 1000.
- gearbox_tokens_total{tier, direction}: tokens in and out.
- gearbox_request_latency_seconds{tier}, gearbox_route_latency_seconds{router}: the
  whole request, and the routing decision alone.
- gearbox_month_spend_gbp, gearbox_monthly_budget_gbp: this month's spend (read from the
  log at scrape time) against the budget cap.

Each app has its own registry, so tests can build many apps in one process.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from gearbox.service.db import RequestLog, month_spend

REQUEST_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)
ROUTE_BUCKETS = (0.001, 0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)


def _flag(value: bool | None) -> str:
    return "true" if value else "false"


class GearboxMetrics:
    def __init__(self, sessions: sessionmaker[Session], monthly_budget_gbp: float | None) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gearbox_requests",
            "API requests, from the request log",
            ["router", "tier", "status", "cache_hit", "fallback", "escalated"],
            registry=self.registry,
        )
        self.cost = Counter(
            "gearbox_cost_gbp",
            "GBP spent on model calls",
            ["tier", "model"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "gearbox_tokens", "Tokens", ["tier", "direction"], registry=self.registry
        )
        self.latency = Histogram(
            "gearbox_request_latency_seconds",
            "Whole request",
            ["tier"],
            buckets=REQUEST_BUCKETS,
            registry=self.registry,
        )
        self.route_latency = Histogram(
            "gearbox_route_latency_seconds",
            "The routing decision alone",
            ["router"],
            buckets=ROUTE_BUCKETS,
            registry=self.registry,
        )
        self.registry.register(_BudgetCollector(sessions, monthly_budget_gbp))
        event.listen(sessions, "after_flush", self._after_flush)

    def _after_flush(self, session: Session, _context: Any) -> None:
        for obj in session.new:
            if isinstance(obj, RequestLog):
                self.observe(obj)

    def observe(self, row: RequestLog) -> None:
        tier = row.tier or "none"
        self.requests.labels(
            row.router,
            tier,
            row.status,
            _flag(row.cache_hit),
            _flag(row.fallback_used),
            _flag(row.escalated),
        ).inc()
        if row.model:  # a model answered: count its cost even when it is £0 (tier S), so
            # cost per 1k requests reads £0 for free traffic instead of "no data"
            self.cost.labels(tier, row.model).inc(row.cost_gbp or 0)
        self.tokens.labels(tier, "in").inc(row.tokens_in or 0)
        self.tokens.labels(tier, "out").inc(row.tokens_out or 0)
        self.latency.labels(tier).observe((row.latency_ms or 0) / 1000)
        self.route_latency.labels(row.router).observe((row.route_ms or 0) / 1000)


class _BudgetCollector:
    """This month's spend against the cap, read from the log when Prometheus scrapes."""

    def __init__(self, sessions: sessionmaker[Session], budget: float | None) -> None:
        self._sessions = sessions
        self._budget = budget

    def collect(self) -> Iterator[GaugeMetricFamily]:
        yield GaugeMetricFamily(
            "gearbox_month_spend_gbp",
            "GBP spent this calendar month (UTC)",
            value=month_spend(self._sessions),
        )
        if self._budget is not None:
            yield GaugeMetricFamily(
                "gearbox_monthly_budget_gbp", "Monthly budget cap", value=self._budget
            )
