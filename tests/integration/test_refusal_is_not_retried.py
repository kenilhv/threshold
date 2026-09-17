"""A job refused for lack of privilege is dead-lettered once, as permanent.

SQLSTATE 42501 could not happen while the application connected as a superuser,
so nothing classified it. It fell through the worker's catch-all as transient:
every job of the affected kind waited out its whole backoff and its whole
attempt budget to be refused identically each time, then reached the dead-letter
queue labelled `transient` -- which tells the operator the wrong thing about
whether replaying it would help.

This runs the real `WorkerRuntime` as the real `threshold_app` role, with a
handler that does something the role may not. The exception it has to recognise
is therefore the genuine one -- SQLAlchemy's `ProgrammingError` around the
driver's adapter around asyncpg's `InsufficientPrivilegeError` -- rather than a
hand-built stand-in that carries the code wherever the test author expected it.

Requires: make up, make test-db
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.databases import ISOLATED, ISOLATED_AS_APP

from threshold.workers import runtime as runtime_module
from threshold.workers.runtime import WorkerRuntime, _refused_by_privilege

pytestmark = pytest.mark.integration

KIND = "privilege_probe"


@pytest_asyncio.fixture
async def as_app() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(ISOLATED_AS_APP.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        async with engine.connect() as connection:
            who = (await connection.execute(text("select current_user"))).scalar_one()
    except Exception as unreachable:
        await engine.dispose()
        pytest.skip(
            f"cannot connect as threshold_app; `make up` and `make test-db` ({unreachable})"
        )
    assert who == "threshold_app", who
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def as_owner() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(ISOLATED)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield sessions
    async with sessions() as session:
        await session.execute(text("delete from dead_letter where kind = :k"), {"k": KIND})
        await session.execute(text("delete from job where kind = :k"), {"k": KIND})
        await session.commit()
    await engine.dispose()


async def _rewrites_the_audit_log(session: AsyncSession, _payload: dict[str, Any]) -> None:
    await session.execute(text("update audit_event set action = 'nothing.happened'"))


async def _divides_by_zero(session: AsyncSession, _payload: dict[str, Any]) -> None:
    await session.execute(text("select 1 / 0"))


async def _run_one(
    sessions: async_sessionmaker[AsyncSession], owner: async_sessionmaker[AsyncSession]
) -> tuple[str, int, str | None, str | None]:
    """Enqueue one job with a three-attempt budget, run it once, report what happened."""
    job_id = uuid.uuid4()
    async with owner() as session:
        await session.execute(
            text(
                # First in line by a century: the isolated database has no worker,
                # so other tests' jobs sit pending in it for ever, and the claim is
                # oldest-first. The first version of this asked for a batch of fifty
                # and filtered -- which claimed forty-nine jobs that were not its own.
                "insert into job (id, kind, payload, max_attempts, run_after) "
                "values (:id, :kind, '{}'::jsonb, 3, now() - interval '100 years')"
            ),
            {"id": job_id, "kind": KIND},
        )
        await session.commit()

    worker = WorkerRuntime(sessions=sessions, batch_size=1)
    claimed = await worker._claim_batch()
    assert [job.id for job in claimed] == [job_id], "claimed something other than the probe"
    await worker._execute(claimed[0])

    async with owner() as session:
        state, attempts = (
            await session.execute(
                text("select state, attempts from job where id = :id"), {"id": job_id}
            )
        ).one()
        letter = (
            await session.execute(
                text(
                    "select failure_kind, final_error from dead_letter where original_job_id = :id"
                ),
                {"id": job_id},
            )
        ).one_or_none()
    return state, attempts, (letter[0] if letter else None), (letter[1] if letter else None)


class TestARefusalIsNotRetried:
    async def test_it_is_dead_lettered_on_the_first_attempt_as_permanent(
        self,
        as_app: async_sessionmaker[AsyncSession],
        as_owner: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(runtime_module.HANDLERS, KIND, _rewrites_the_audit_log)
        state, attempts, failure_kind, error = await _run_one(as_app, as_owner)

        assert state == "dead", f"still {state} after a refusal that retrying cannot fix"
        assert attempts == 1, "the attempt budget was spent on a missing grant"
        assert failure_kind == "permanent"
        assert error is not None and "42501" in error and "audit_event" in error, error
        assert "migration" in error, "the letter should say where the fix lives"

    async def test_an_ordinary_database_error_is_still_retried(
        self,
        as_app: async_sessionmaker[AsyncSession],
        as_owner: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The vacuity guard. A classifier that called every database error a
        refusal would pass the test above and dead-letter a deadlock."""
        monkeypatch.setitem(runtime_module.HANDLERS, KIND, _divides_by_zero)
        state, attempts, failure_kind, _ = await _run_one(as_app, as_owner)

        assert state == "pending", state
        assert attempts == 1
        assert failure_kind is None, "one failed attempt out of three is not a dead letter"


class TestTheClassifierReadsTheRealException:
    async def test_the_genuine_refusal_is_recognised(
        self, as_app: async_sessionmaker[AsyncSession]
    ) -> None:
        async with as_app() as session:
            with pytest.raises(DBAPIError) as refused:
                await session.execute(text("delete from ledger_entry"))
        assert _refused_by_privilege(refused.value)

    async def test_other_errors_are_not(self, as_app: async_sessionmaker[AsyncSession]) -> None:
        async with as_app() as session:
            with pytest.raises(DBAPIError) as failed:
                await session.execute(text("select * from a_table_that_does_not_exist"))
        assert not _refused_by_privilege(failed.value)
        assert not _refused_by_privilege(RuntimeError("permission denied"))

    def test_a_cycle_of_causes_terminates(self) -> None:
        first, second = RuntimeError("a"), RuntimeError("b")
        first.__cause__, second.__cause__ = second, first
        assert not _refused_by_privilege(first)
