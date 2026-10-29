"""Alembic environment: points migrations at Gearbox's models and database."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from gearbox.service.db import Base, make_engine

target_metadata = Base.metadata


def _url() -> str:
    # An explicit URL (tests pass one) wins; otherwise the app's own setting.
    url = context.config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from gearbox.config import get_secrets

    return get_secrets().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite can't ALTER much
    )


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
