"""Alembic environment.

Runs on the same asyncpg driver the application uses, rather than pulling in a
second synchronous driver just for migrations. One driver means the migration
path and the runtime path exercise the same connection behaviour — if asyncpg
rejects something, it is rejected at migration time rather than at 3am.

Migrations are their own unit of work, never an application-startup side effect,
so two API replicas can never race to migrate the same database.

The database URL goes straight to SQLAlchemy and never through Alembic's
``config``. That object is a ``ConfigParser``, which reads ``%`` as interpolation
syntax, and a password that Settings has percent-encoded — which is any
password RDS generates — made ``set_main_option`` raise before a connection was
attempted, with the URL in the traceback. Found by review of the AWS deploy
path before it ran; never seen locally, where the compose URL is a literal.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from threshold.config.settings import get_settings
from threshold.domain.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DATABASE_URL = str(get_settings().database_url)

target_metadata = Base.metadata


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    """Keep autogenerate from proposing drops for things it does not own."""
    return not (type_ == "table" and name in {"alembic_version"})


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        # One transaction for the whole upgrade: a migration that fails halfway
        # leaves the schema exactly as it was.
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
