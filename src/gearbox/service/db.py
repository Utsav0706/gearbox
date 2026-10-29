"""The request log: one row per API request (sync SQLAlchemy 2.0).

What is logged is metadata only: which router chose which tier and why, tokens, cost,
latency. Prompt and answer text are never stored, so the log holds no user content.

Sync, not async: the service writes one small row per request, so a thread-pool call
(asyncio.to_thread) is enough; an async driver would add a dependency for no gain.
The schema is created by Alembic migrations (`py -m alembic upgrade head`), never by
the app at startup, so a schema change is always a reviewed, versioned step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    Engine,
    Float,
    Integer,
    String,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class RequestLog(Base):
    __tablename__ = "request_log"

    request_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16))  # ok | error
    router: Mapped[str] = mapped_column(String(64))  # router name, or "pinned"
    tier: Mapped[str | None] = mapped_column(String(1))
    model: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(500))  # why this tier (router's own words)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer)  # whole request
    route_ms: Mapped[int] = mapped_column(Integer)  # the routing decision alone
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)  # M7
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)  # M7
    error: Mapped[str | None] = mapped_column(String(500))


def make_engine(database_url: str) -> Engine:
    if database_url in ("sqlite://", "sqlite:///:memory:"):
        # Tests: one shared in-memory connection across threads.
        return create_engine(
            database_url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    if database_url.startswith("sqlite"):
        return create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 15})
    return create_engine(database_url, pool_pre_ping=True)  # drop dead Postgres connections


def make_sessions(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def db_is_up(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def write_log(sessions: sessionmaker[Session], row: RequestLog) -> None:
    with sessions.begin() as s:
        s.add(row)


def stats(sessions: sessionmaker[Session], hours: int = 24) -> dict[str, object]:
    """Cost and route split for the last `hours` (GET /stats)."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    window = RequestLog.created_at >= since
    with sessions() as s:
        total, cost, avg_ms, avg_route_ms, errors, cache_hits, fallbacks = s.execute(
            select(
                func.count(),
                func.coalesce(func.sum(RequestLog.cost_gbp), 0.0),
                func.avg(RequestLog.latency_ms),
                func.avg(RequestLog.route_ms),
                func.coalesce(func.sum(func.cast(RequestLog.status == "error", Integer)), 0),
                func.coalesce(func.sum(func.cast(RequestLog.cache_hit, Integer)), 0),
                func.coalesce(func.sum(func.cast(RequestLog.fallback_used, Integer)), 0),
            ).where(window)
        ).one()
        by_tier = dict(
            s.execute(
                select(RequestLog.tier, func.count())
                .where(window, RequestLog.tier.is_not(None))
                .group_by(RequestLog.tier)
            ).all()
        )
        by_router = dict(
            s.execute(
                select(RequestLog.router, func.count()).where(window).group_by(RequestLog.router)
            ).all()
        )
    return {
        "window_hours": hours,
        "requests": total,
        "errors": int(errors),
        "cost_gbp": round(float(cost), 6),
        "avg_latency_ms": round(float(avg_ms), 1) if avg_ms is not None else None,
        "avg_route_ms": round(float(avg_route_ms), 1) if avg_route_ms is not None else None,
        "by_tier": {t: by_tier.get(t, 0) for t in ("S", "M", "L")},
        "by_router": by_router,
        "cache_hits": int(cache_hits),
        "fallbacks": int(fallbacks),
    }
