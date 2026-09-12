"""Database engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from threshold.config.settings import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(
            str(settings.database_url),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,  # a recycled connection through a NAT is a 3am page
            echo=settings.db_echo,
        )
        # Instrumented here, on the engine, and deliberately not globally from
        # `configure_tracing`. The global form works by replacing
        # `sqlalchemy.ext.asyncio.create_async_engine` on the module, so it only
        # reaches engines built from a reference resolved after it ran -- and
        # this module binds `create_async_engine` at import, which is long
        # before `create_app()` configures anything. The result was silent and
        # total: from M5 until this was measured, the API emitted a `connect`
        # span and no `select` spans at all, and the connection pool metric was
        # declared and never produced. Instrumenting the object rather than the
        # factory cannot fail that way, whatever the import order is.
        SQLAlchemyInstrumentor().instrument(engine=_engine.sync_engine, enable_commenter=False)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("init_engine() must be called during startup")
    return _sessionmaker


@asynccontextmanager
async def session_scope(
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work.

    Commits on success, rolls back on any exception. Audit events are written
    inside this scope by their callers so the trail commits with the change it
    describes, never after it.

    ``sessions`` overrides the process-global engine. Production never passes it;
    it exists so a worker can be run against a specific database without
    mutating global state, which is what makes the outage test in
    ``tests/integration/test_queue.py`` possible at all.
    """
    async with (sessions or get_sessionmaker())() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session
