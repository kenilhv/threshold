"""The queue, against a real Postgres.

These cannot be unit tests. Every property that matters here — `SKIP LOCKED`
handing two concurrent claimers different rows, a partial unique index
suppressing a duplicate enqueue, a lease expiring and the work coming back — is
a property of the database, not of the Python around it. An in-memory double
would assert that the code I wrote does what I think it does, which is not the
question.

Requires `make up`.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.metrics_reader import Recording

from threshold.adapters.queue import ClaimedJob, PostgresJobQueue
from threshold.domain.jobs import DEFAULT_MAX_ATTEMPTS, JobKind, JobState, backoff_for
from threshold.domain.models import DeadLetter, Job
from threshold.workers.runtime import WorkerRuntime

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

ADMIN_URL = "postgresql+asyncpg://threshold:threshold@localhost:5432/postgres"

#: These tests get their own database, and that is not fastidiousness.
#:
#: `make up` leaves a worker container polling the application database once a
#: second. It is a real consumer with the same claim query, so it will happily
#: take a job a test just enqueued — and then, because the payload names a
#: document that does not exist, fail it. Every assertion here about who claimed
#: what, how many attempts a job has, and what reached the dead-letter table is
#: only meaningful if nothing else is draining the queue.
#:
#: The suite passed for a while regardless, which is the uncomfortable part: it
#: was passing because the worker had crashed and nobody had noticed. A test
#: that depends on another process being broken is not a passing test.
QUEUE_TEST_DATABASE = "threshold_queue_tests"
DATABASE_URL = f"postgresql+asyncpg://threshold:threshold@localhost:5432/{QUEUE_TEST_DATABASE}"


async def _provision() -> None:
    """Drop, recreate and migrate the queue test database."""
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{QUEUE_TEST_DATABASE}' AND pid <> pg_backend_pid()"
                )
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{QUEUE_TEST_DATABASE}"'))
            await conn.execute(text(f'CREATE DATABASE "{QUEUE_TEST_DATABASE}"'))
    finally:
        await admin.dispose()


def _migrate() -> None:
    """Alembic, not `create_all`.

    The metadata does not carry the partial indexes — `uq_job_dedupe_key` is
    hand-written in `0003_job_queue` because SQLAlchemy cannot express the
    predicate the deduplication depends on. A schema built from the models
    would silently drop the exact constraint several of these tests exist to
    verify.
    """
    from alembic import command
    from alembic.config import Config

    from threshold.config.settings import get_settings

    # `migrations/env.py` builds the full Settings object, so every required
    # field has to be present even though a migration touches none of them but
    # the database. Placeholders, not credentials — nothing here connects to
    # S3.
    overrides = {
        "THRESHOLD_DATABASE_URL": DATABASE_URL,
        "THRESHOLD_S3_ACCESS_KEY_ID": "unused-by-migrations",
        "THRESHOLD_S3_SECRET_ACCESS_KEY": "unused-by-migrations",
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    finally:
        for key, was in previous.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def queue_database() -> str:
    """Provisioned once per run.

    Deliberately a synchronous fixture: it is outside any event loop, so
    `asyncio.run` is safe here and Alembic's own `asyncio.run` does not collide
    with a loop pytest-asyncio is already driving.
    """
    try:
        asyncio.run(_provision())
    except Exception as exc:  # pragma: no cover - environment, not logic
        pytest.skip(f"Postgres is not reachable ({exc.__class__.__name__}); run `make up`")
    _migrate()
    return DATABASE_URL


@pytest_asyncio.fixture
async def sessionmaker_(queue_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(queue_database)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_() as s:
        yield s
        await s.rollback()


@pytest_asyncio.fixture(autouse=True)
async def clean_queue(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Each test starts from an empty queue.

    The suite runs against a persistent stack, so leftovers from a previous run
    would make claim-ordering assertions meaningless.
    """
    async with sessionmaker_() as s:
        await s.execute(delete(DeadLetter))
        await s.execute(delete(Job))
        await s.commit()
    yield


@pytest.fixture
def queue() -> PostgresJobQueue:
    return PostgresJobQueue(lease=timedelta(seconds=30))


async def enqueue_n(
    queue: PostgresJobQueue, sm: async_sessionmaker[AsyncSession], n: int
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with sm() as s:
        for i in range(n):
            job_id = await queue.enqueue(
                s, kind=JobKind.EXTRACT_DOCUMENT, payload={"document_id": str(uuid.uuid4()), "i": i}
            )
            assert job_id is not None
            ids.append(job_id)
        await s.commit()
    return ids


class TestClaiming:
    async def test_a_claimed_job_is_leased_and_counted(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            claimed = await queue.claim(s, worker_id="w1", limit=5)
            await s.commit()

        assert len(claimed) == 1
        assert claimed[0].attempts == 1, "the claim itself counts as the first attempt"
        assert claimed[0].lease_expires_at > datetime.now(UTC)

    async def test_claiming_takes_the_oldest_runnable_first(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """Ordering holds across transactions, which is the case that matters.

        Note what this does NOT claim. ``run_after`` and ``created_at`` both
        default to ``now()``, and inside one transaction Postgres returns the
        *transaction start* time for every call — so jobs enqueued together
        share a timestamp and their relative order is genuinely undefined.

        That is fine for this system: documents arrive in separate requests, so
        each gets its own transaction and its own timestamp. If strict FIFO
        within a batch were ever needed, ordering would have to move to a
        sequence rather than a clock.
        """
        ids: list[uuid.UUID] = []
        for _ in range(3):
            ids.extend(await enqueue_n(queue, sessionmaker_, 1))

        async with sessionmaker_() as s:
            claimed = await queue.claim(s, worker_id="w1", limit=3)
            await s.commit()
        assert [c.id for c in claimed] == ids

    async def test_a_job_scheduled_for_later_is_not_claimable_yet(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessionmaker_() as s:
            await queue.enqueue(
                s,
                kind=JobKind.EXTRACT_DOCUMENT,
                payload={"document_id": str(uuid.uuid4())},
                run_after=datetime.now(UTC) + timedelta(hours=1),
            )
            await s.commit()

        async with sessionmaker_() as s:
            assert await queue.claim(s, worker_id="w1", limit=10) == []


class TestConcurrency:
    async def test_two_workers_never_receive_the_same_job(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """The property `SKIP LOCKED` exists for.

        This is the test that distinguishes a queue from a race. `FOR UPDATE`
        without `SKIP LOCKED` passes every single-worker test and then either
        serialises the pool or hands one row to two workers, depending on the
        isolation level — and in this domain that means paying an invoice twice.
        """
        await enqueue_n(queue, sessionmaker_, 40)

        async def worker(name: str) -> list[uuid.UUID]:
            taken: list[uuid.UUID] = []
            for _ in range(10):
                async with sessionmaker_() as s:
                    claimed = await queue.claim(s, worker_id=name, limit=4)
                    await s.commit()
                taken.extend(c.id for c in claimed)
            return taken

        results = await asyncio.gather(*(worker(f"w{i}") for i in range(4)))

        everything = [job_id for r in results for job_id in r]
        assert len(everything) == 40, "every job should be claimed exactly once"
        assert len(set(everything)) == 40, "the same job was handed to two workers"

    async def test_concurrent_enqueue_with_one_dedupe_key_yields_one_job(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """At-least-once delivery must not become at-least-once work.

        ``run_after`` is pushed into the future deliberately. The dedupe index
        is partial — it only covers ``pending`` and ``claimed`` rows — so a key
        stops blocking as soon as its job finishes. That is intended: the same
        logical work should be enqueueable again later. But it means a running
        worker can complete the first job mid-test and free the key for a
        second insert, which made this assertion flake against a live stack.

        Keeping the job unclaimable isolates what is under test — the unique
        index under concurrent inserts — from what is not.
        """
        key = f"dedupe-{uuid.uuid4()}"
        unclaimable = datetime.now(UTC) + timedelta(hours=1)

        async def submit() -> uuid.UUID | None:
            async with sessionmaker_() as s:
                job_id = await queue.enqueue(
                    s,
                    kind=JobKind.EXTRACT_DOCUMENT,
                    payload={"document_id": str(uuid.uuid4())},
                    dedupe_key=key,
                    run_after=unclaimable,
                )
                await s.commit()
                return job_id

        results = await asyncio.gather(*(submit() for _ in range(8)), return_exceptions=True)
        created = [r for r in results if isinstance(r, uuid.UUID)]
        assert len(created) == 1, f"expected exactly one job, got {len(created)}"

        async with sessionmaker_() as s:
            live = (await s.execute(select(Job).where(Job.dedupe_key == key))).scalars().all()
        assert len(live) == 1


class TestLeaseRecovery:
    async def test_an_expired_lease_returns_the_job_to_the_queue(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """Crash recovery. A worker that dies stops renewing its lease."""
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            claimed = await queue.claim(s, worker_id="doomed", limit=1)
            await s.commit()
        job_id = claimed[0].id

        # Simulate the worker dying: the lease is never renewed and passes.
        async with sessionmaker_() as s:
            await s.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
            await s.commit()

        async with sessionmaker_() as s:
            reaped = await queue.reap_expired_leases(s)
            await s.commit()
        assert reaped == 1

        async with sessionmaker_() as s:
            reclaimed = await queue.claim(s, worker_id="survivor", limit=1)
            await s.commit()
        assert len(reclaimed) == 1
        assert reclaimed[0].id == job_id
        assert reclaimed[0].attempts == 2, (
            "the reaper must not re-count the attempt the dead worker already used"
        )

    async def test_a_live_lease_is_not_reaped(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            await queue.claim(s, worker_id="healthy", limit=1)
            await s.commit()
        async with sessionmaker_() as s:
            assert await queue.reap_expired_leases(s) == 0

    async def test_heartbeat_fails_once_the_job_was_given_away(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """A worker whose lease was reaped must find out and stop.

        Otherwise it finishes work another worker is already doing.
        """
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            claimed = await queue.claim(s, worker_id="slow", limit=1)
            await s.commit()

        async with sessionmaker_() as s:
            assert await queue.heartbeat(s, claimed[0].id, worker_id="slow") is True
            await s.commit()

        async with sessionmaker_() as s:
            await s.execute(
                update(Job)
                .where(Job.id == claimed[0].id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
            await queue.reap_expired_leases(s)
            await s.commit()

        async with sessionmaker_() as s:
            assert await queue.heartbeat(s, claimed[0].id, worker_id="slow") is False


async def _stolen(
    queue: PostgresJobQueue, sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID, *, thief: str
) -> ClaimedJob:
    """Take a claimed job from whoever holds it, by the only route production has.

    Not by writing `claimed_by` directly: the lease is expired by the database
    owner, which is the one step that stands in for time passing, and then the
    real reaper and the real claim do the rest. A test that forged the handover
    would be testing the forgery.
    """
    async with sm() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
        assert await queue.reap_expired_leases(s) == 1
        [taken] = await queue.claim(s, worker_id=thief, limit=1)
        await s.commit()
    assert taken.id == job_id, "the thief claimed some other job"
    return taken


class TestALostLeaseSettlesNothing:
    """`succeed()` and `fail()` report whether this worker still held the job.

    Both were already scoped to the owner, so a stolen lease updated no rows.
    Neither said so. `succeed()` returned None, and the runtime committed the
    handler's work on the strength of a statement that matched nothing -- which
    is how a matching job that outlived its lease paid an invoice twice.
    `fail()` was worse: it wrote the dead letter before its UPDATE and returned
    True regardless, filing a job another worker was running as dead.
    """

    async def _handed_over(
        self, queue: PostgresJobQueue, sm: async_sessionmaker[AsyncSession]
    ) -> tuple[ClaimedJob, ClaimedJob]:
        await enqueue_n(queue, sm, 1)
        async with sm() as s:
            [loser] = await queue.claim(s, worker_id="A", limit=1)
            await s.commit()
        winner = await _stolen(queue, sm, loser.id, thief="B")
        return loser, winner

    async def test_the_worker_that_lost_the_lease_cannot_succeed_and_the_holder_can(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        loser, winner = await self._handed_over(queue, sessionmaker_)

        async with sessionmaker_() as s:
            assert await queue.succeed(s, loser.id, worker_id="A") is False
            await s.commit()
        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == loser.id))).scalar_one()
            assert (row.state, row.claimed_by) == (JobState.CLAIMED.value, "B"), (
                "the losing worker's success reached the row anyway"
            )

        # The vacuity guard: a succeed() that always answered False would pass
        # everything above.
        async with sessionmaker_() as s:
            assert await queue.succeed(s, winner.id, worker_id="B") is True
            await s.commit()
        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == winner.id))).scalar_one()
            assert row.state == JobState.SUCCEEDED.value

    async def test_the_worker_that_lost_the_lease_cannot_dead_letter_the_job(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """The one that filed a live job as dead. An operator replaying that
        letter would have run the work a third time."""
        loser, _ = await self._handed_over(queue, sessionmaker_)

        async with sessionmaker_() as s:
            assert (
                await queue.fail(s, loser, worker_id="A", error="gave up", permanent=True) is False
            )
            await s.commit()

        async with sessionmaker_() as s:
            letters = (await s.execute(select(func.count()).select_from(DeadLetter))).scalar_one()
            row = (await s.execute(select(Job).where(Job.id == loser.id))).scalar_one()
        assert letters == 0, "a dead letter was filed for a job another worker holds"
        assert (row.state, row.claimed_by) == (JobState.CLAIMED.value, "B")
        assert row.last_error != "gave up"

    async def test_the_worker_that_lost_the_lease_cannot_reschedule_the_job(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """The retry branch. It was already scoped and already returned False,
        so the return value was never wrong here -- but it logged a retry that
        nobody had scheduled, and now it says what happened instead."""
        loser, _ = await self._handed_over(queue, sessionmaker_)
        assert not loser.is_final_attempt, "this would take the dead-letter branch"

        async with sessionmaker_() as s:
            assert await queue.fail(s, loser, worker_id="A", error="blip", permanent=False) is False
            await s.commit()

        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == loser.id))).scalar_one()
        assert (row.state, row.claimed_by) == (JobState.CLAIMED.value, "B")
        assert row.last_error != "blip"

    async def test_the_worker_holding_the_lease_can_still_dead_letter_it(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """The vacuity guard for the two above. `fail()` now checks before it
        writes, and a check that never passed would make both of them green
        while dead-lettering nothing ever again."""
        _, winner = await self._handed_over(queue, sessionmaker_)

        async with sessionmaker_() as s:
            assert await queue.fail(s, winner, worker_id="B", error="bad", permanent=True) is True
            await s.commit()

        async with sessionmaker_() as s:
            letter = (
                await s.execute(select(DeadLetter).where(DeadLetter.original_job_id == winner.id))
            ).scalar_one()
        assert (letter.final_error, letter.attempts) == ("bad", 2)


class TestALosingWorkerCommitsNothing:
    """The runtime half. A handler that outlives its lease leaves no trace.

    The handler here does real work -- it writes an audit row, which is the one
    write every real handler makes -- and then the job is taken from it while it
    runs, through the real reaper and the real claim. When it returns, the
    runtime's `succeed()` finds the job held by someone else, and everything the
    handler wrote must roll back. Before this, it committed.

    The audit row is flushed and read back inside the handler, so "no trace"
    means a row that reached Postgres was rolled back, not a pending object that
    was never sent.
    """

    PROBE = "test.lease_probe"

    async def _run(
        self,
        queue: PostgresJobQueue,
        sm: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        *,
        steal: bool,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Run one job through a real WorkerRuntime. Returns (job id, probe id)."""
        from threshold.services.audit import Actor, record
        from threshold.workers.handlers import HANDLERS

        probe = uuid.uuid4()
        runtime = WorkerRuntime(queue=queue, sessions=sm, poll_interval=timedelta(milliseconds=20))

        async def handler(session: AsyncSession, payload: dict[str, Any]) -> None:
            await record(
                session,
                actor=Actor.system(),
                action=self.PROBE,
                entity_type="job",
                entity_id=probe,
            )
            await session.flush()
            written = await session.execute(
                text("select count(*) from audit_event where entity_id = :probe"),
                {"probe": probe},
            )
            assert written.scalar_one() == 1, "the write never reached the database"

            if steal:
                held = (
                    await session.execute(select(Job.id).where(Job.claimed_by == runtime.identity))
                ).scalar_one()
                await _stolen(queue, sm, held, thief="the-other-worker")

            # Stop after this job, so run_forever returns once it is settled.
            runtime.request_stop()

        monkeypatch.setitem(HANDLERS, JobKind.EXTRACT_DOCUMENT.value, handler)
        [job_id] = await enqueue_n(queue, sm, 1)

        task = asyncio.create_task(runtime.run_forever())
        await asyncio.wait_for(task, timeout=10.0)
        return job_id, probe

    async def _probes(self, sm: async_sessionmaker[AsyncSession], probe: uuid.UUID) -> int:
        async with sm() as s:
            return (
                await s.execute(
                    text("select count(*) from audit_event where entity_id = :probe"),
                    {"probe": probe},
                )
            ).scalar_one()

    async def test_the_handler_s_writes_roll_back_and_the_job_stays_with_its_new_owner(
        self,
        queue: PostgresJobQueue,
        sessionmaker_: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        recording: Recording,
    ) -> None:
        from threshold.observability import metrics as m

        job_id, probe = await self._run(queue, sessionmaker_, monkeypatch, steal=True)

        assert await self._probes(sessionmaker_, probe) == 0, (
            "a worker that had lost its lease committed its handler's work"
        )
        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
            letters = (await s.execute(select(func.count()).select_from(DeadLetter))).scalar_one()
        assert (row.state, row.claimed_by) == (JobState.CLAIMED.value, "the-other-worker")
        assert row.attempts == 2, "the new owner's claim is the only other thing that happened"
        assert letters == 0, "the loser settled a failure for a job it no longer held"
        assert (
            recording.increase(
                m.JOB_DURATION, kind=JobKind.EXTRACT_DOCUMENT.value, outcome="lease_lost"
            )
            == 1
        )

    async def test_a_handler_that_keeps_its_lease_commits(
        self,
        queue: PostgresJobQueue,
        sessionmaker_: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The vacuity guard. If the probe never committed at all -- a handler
        that was not registered, a write that was never flushed -- the test above
        would pass without the runtime having done anything."""
        job_id, probe = await self._run(queue, sessionmaker_, monkeypatch, steal=False)

        assert await self._probes(sessionmaker_, probe) == 1
        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert row.state == JobState.SUCCEEDED.value


class TestFailureHandling:
    async def test_a_transient_failure_is_retried_with_backoff(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            job = (await queue.claim(s, worker_id="w1", limit=1))[0]
            dead_lettered = await queue.fail(
                s, job, worker_id="w1", error="provider timed out", permanent=False
            )
            await s.commit()

        assert dead_lettered is False
        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == job.id))).scalar_one()
            assert row.state == JobState.PENDING.value
            assert row.run_after > datetime.now(UTC), "the retry must be scheduled, not immediate"
            assert row.claimed_by is None

    async def test_a_permanent_failure_skips_straight_to_the_dead_letter_queue(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        """Retrying a corrupt document three times helps nobody."""
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            job = (await queue.claim(s, worker_id="w1", limit=1))[0]
            assert job.attempts == 1, "still has attempts left"
            dead_lettered = await queue.fail(
                s, job, worker_id="w1", error="document is not a PDF", permanent=True
            )
            await s.commit()

        assert dead_lettered is True
        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == job.id))).scalar_one()
            assert row.state == JobState.DEAD.value
            letter = (
                await s.execute(select(DeadLetter).where(DeadLetter.original_job_id == job.id))
            ).scalar_one()
            assert letter.failure_kind == "permanent"
            assert letter.attempts == 1

    async def test_attempts_are_exhausted_then_dead_lettered(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        await enqueue_n(queue, sessionmaker_, 1)
        job_id: uuid.UUID | None = None

        for attempt in range(1, 4):
            async with sessionmaker_() as s:
                # Clear any backoff so the next claim is immediate.
                # The database's clock. This used the host's, and the claim that
                # follows compares against now() in the same transaction -- so
                # it passed or failed on sub-millisecond skew between the two.
                await s.execute(update(Job).values(run_after=func.now()))
                jobs = await queue.claim(s, worker_id="w1", limit=1)
                await s.commit()
            assert len(jobs) == 1, f"expected a claimable job on attempt {attempt}"
            job_id = jobs[0].id
            async with sessionmaker_() as s:
                await queue.fail(
                    s, jobs[0], worker_id="w1", error="flaky dependency", permanent=False
                )
                await s.commit()

        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
            assert row.state == JobState.DEAD.value
            letter = (
                await s.execute(select(DeadLetter).where(DeadLetter.original_job_id == job_id))
            ).scalar_one()
            assert letter.failure_kind == "transient"
            assert letter.attempts == 3


class TestReplay:
    async def test_a_dead_letter_can_be_replayed_and_is_marked(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            job = (await queue.claim(s, worker_id="w1", limit=1))[0]
            await queue.fail(s, job, worker_id="w1", error="bad", permanent=True)
            await s.commit()

        async with sessionmaker_() as s:
            letter = (
                await s.execute(select(DeadLetter).where(DeadLetter.original_job_id == job.id))
            ).scalar_one()
            new_job_id = await queue.replay_dead_letter(s, letter.id)
            await s.commit()

        assert new_job_id is not None
        async with sessionmaker_() as s:
            replayed = (await s.execute(select(Job).where(Job.id == new_job_id))).scalar_one()
            assert replayed.state == JobState.PENDING.value
            assert replayed.attempts == 0, "a replay starts with a fresh budget"
            letter = (
                await s.execute(select(DeadLetter).where(DeadLetter.id == letter.id))
            ).scalar_one()
            assert letter.replayed_at is not None, "the history of the failure survives the fix"

    async def test_replaying_twice_is_refused(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            job = (await queue.claim(s, worker_id="w1", limit=1))[0]
            await queue.fail(s, job, worker_id="w1", error="bad", permanent=True)
            await s.commit()
        async with sessionmaker_() as s:
            letter_id = (
                await s.execute(select(DeadLetter.id).where(DeadLetter.original_job_id == job.id))
            ).scalar_one()
            assert await queue.replay_dead_letter(s, letter_id) is not None
            await s.commit()
        async with sessionmaker_() as s:
            assert await queue.replay_dead_letter(s, letter_id) is None


class TestTheDeadLetterRemembersItsKey:
    """`DeadLetter.dedupe_key` was declared at M2 and written by nothing until M8.

    `queue.py`'s own module docstring cited it as proof that hand-copied columns
    get missed -- five places to change, and one of them was forgotten for six
    milestones. These are the tests that make the copy path load-bearing rather
    than hopeful.
    """

    async def test_a_dying_job_takes_its_key_with_it(
        self, session: AsyncSession, queue: PostgresJobQueue
    ) -> None:
        await queue.enqueue(
            session,
            kind=JobKind.EXTRACT_DOCUMENT,
            payload={"document_id": str(uuid.uuid4())},
            dedupe_key="extract:abc123",
        )
        await session.commit()

        [job] = await queue.claim(session, worker_id="w", limit=1)
        assert job.dedupe_key == "extract:abc123", "the claim did not return the key"

        await queue.fail(session, job, worker_id="w", error="unreadable", permanent=True)
        await session.commit()

        letter = (
            await session.execute(select(DeadLetter).where(DeadLetter.original_job_id == job.id))
        ).scalar_one()
        assert letter.dedupe_key == "extract:abc123"

    async def test_replaying_puts_the_key_back(
        self, session: AsyncSession, queue: PostgresJobQueue
    ) -> None:
        """Without this a replayed job is undeduplicated, which is how the same
        work runs twice."""
        await queue.enqueue(
            session,
            kind=JobKind.EXTRACT_DOCUMENT,
            payload={"document_id": str(uuid.uuid4())},
            dedupe_key="extract:replayed",
        )
        await session.commit()
        [job] = await queue.claim(session, worker_id="w", limit=1)
        await queue.fail(session, job, worker_id="w", error="nope", permanent=True)
        await session.commit()

        letter = (
            await session.execute(select(DeadLetter).where(DeadLetter.original_job_id == job.id))
        ).scalar_one()
        replayed = await queue.replay_dead_letter(session, letter.id)
        await session.commit()

        assert replayed is not None
        revived = (await session.execute(select(Job).where(Job.id == replayed))).scalar_one()
        assert revived.dedupe_key == "extract:replayed"

    async def test_two_letters_sharing_a_key_replay_to_one_job(
        self, session: AsyncSession, queue: PostgresJobQueue
    ) -> None:
        """The case `queuectl replay --all` can actually produce.

        A key is unique only among `pending` and `claimed` jobs (D-009), so once
        job A dies its key is free and a producer can enqueue B with the same
        one. If B dies too there are two letters for one piece of work, and
        replaying both used to run it twice -- each replay passed
        `dedupe_key=None`, so the partial index had nothing to refuse.
        """
        letters = []
        for _ in range(2):
            await queue.enqueue(
                session,
                kind=JobKind.EXTRACT_DOCUMENT,
                payload={"document_id": str(uuid.uuid4())},
                dedupe_key="extract:shared",
            )
            await session.commit()
            [job] = await queue.claim(session, worker_id="w", limit=1)
            await queue.fail(session, job, worker_id="w", error="nope", permanent=True)
            await session.commit()
            letters.append(
                (
                    await session.execute(
                        select(DeadLetter).where(DeadLetter.original_job_id == job.id)
                    )
                ).scalar_one()
            )

        first = await queue.replay_dead_letter(session, letters[0].id)
        second = await queue.replay_dead_letter(session, letters[1].id)
        await session.commit()

        assert first is not None, "the first replay should have produced a job"
        assert second is None, "the second replay ran the same work again"

        live = (
            await session.execute(
                select(func.count())
                .select_from(Job)
                .where(Job.dedupe_key == "extract:shared")
                .where(Job.state.in_(["pending", "claimed"]))
            )
        ).scalar_one()
        assert live == 1

    async def test_a_suppressed_replay_stays_on_the_operator_s_list(
        self, session: AsyncSession, queue: PostgresJobQueue
    ) -> None:
        """The landmine that populating the key would otherwise have armed.

        `replay_dead_letter` used to set `replayed_at` unconditionally, which was
        unreachable while it passed `dedupe_key=None` -- the insert always
        returned an id. The moment a real key is passed, a suppressed replay
        would mark the letter done having created no job, dropping it out of
        `ix_dead_letter_unreplayed`, out of the `dead_letters_unreplayed` gauge
        and out of the DeadLettersUnreplayed alert. An operator's to-do item
        deleted by the act of failing to act on it.
        """
        letters = []
        for _ in range(2):
            await queue.enqueue(
                session,
                kind=JobKind.EXTRACT_DOCUMENT,
                payload={"document_id": str(uuid.uuid4())},
                dedupe_key="extract:suppressed",
            )
            await session.commit()
            [job] = await queue.claim(session, worker_id="w", limit=1)
            await queue.fail(session, job, worker_id="w", error="nope", permanent=True)
            await session.commit()
            letters.append(
                (
                    await session.execute(
                        select(DeadLetter).where(DeadLetter.original_job_id == job.id)
                    )
                ).scalar_one()
            )

        await queue.replay_dead_letter(session, letters[0].id)
        assert await queue.replay_dead_letter(session, letters[1].id) is None
        await session.commit()

        await session.refresh(letters[1])
        assert letters[1].replayed_at is None, (
            "a replay that produced no job marked the letter done, so nothing "
            "will ever show it to an operator again"
        )

    async def test_a_job_with_no_key_still_dies_and_replays(
        self, session: AsyncSession, queue: PostgresJobQueue
    ) -> None:
        """The vacuity guard. Most jobs carry no key at all, and copying a null
        must stay a no-op rather than becoming a crash."""
        await queue.enqueue(
            session,
            kind=JobKind.EXTRACT_DOCUMENT,
            payload={"document_id": str(uuid.uuid4())},
            dedupe_key=None,
        )
        await session.commit()
        [job] = await queue.claim(session, worker_id="w", limit=1)
        assert job.dedupe_key is None
        await queue.fail(session, job, worker_id="w", error="nope", permanent=True)
        await session.commit()

        letter = (
            await session.execute(select(DeadLetter).where(DeadLetter.original_job_id == job.id))
        ).scalar_one()
        assert letter.dedupe_key is None
        assert await queue.replay_dead_letter(session, letter.id) is not None


class TestBackoffSchedule:
    def test_it_grows_and_then_stops_growing(self) -> None:
        delays = [backoff_for(n) for n in range(1, 12)]
        assert delays[0] == timedelta(seconds=5)
        assert delays[1] == timedelta(seconds=10)
        assert delays[2] == timedelta(seconds=20)
        assert all(b >= a for a, b in itertools.pairwise(delays))
        assert delays[-1] == timedelta(minutes=10), "must cap, or a retry sleeps for a week"

    def test_attempt_numbering_is_one_based(self) -> None:
        with pytest.raises(ValueError, match="1-based"):
            backoff_for(0)


class TestPoolSurvivesADatabaseOutage:
    """The pool must outlive a database that goes away and comes back.

    This is a regression test for a real incident in this repository. Running
    `make migrate-roundtrip` against a live stack dropped the `job` table
    underneath a polling worker; the claim raised `UndefinedTableError`, the
    exception escaped `run_forever`, the process exited, and because Compose
    leaves `restart` at its default the container stayed dead. Work stopped and
    nothing said so — the API kept accepting documents and queueing jobs that
    nobody would ever run.

    A failed claim has claimed nothing. There is no half-finished work to
    reconcile and no lease to lose, so retrying is unconditionally safe, which
    makes exiting the strictly worse choice.
    """

    async def test_a_failing_claim_backs_off_instead_of_killing_the_worker(
        self, queue: PostgresJobQueue, sessionmaker_: async_sessionmaker[AsyncSession]
    ) -> None:
        outage = _FailsFirstNClaims(queue, failures=3)
        runtime = WorkerRuntime(
            queue=outage, sessions=sessionmaker_, poll_interval=timedelta(milliseconds=20)
        )

        await enqueue_n(queue, sessionmaker_, 1)

        task = asyncio.create_task(runtime.run_forever())
        try:
            # Long enough to cover 40ms + 80ms + 160ms of backoff and then work.
            await asyncio.wait_for(outage.recovered.wait(), timeout=10.0)
            await asyncio.sleep(0.5)
        finally:
            runtime.request_stop()
            await asyncio.wait_for(task, timeout=10.0)

        assert outage.attempts > 3, "the worker gave up instead of retrying"
        assert task.exception() is None if task.done() else True

        async with sessionmaker_() as s:
            states = (await s.execute(select(Job.state))).scalars().all()
        assert JobState.PENDING.value not in states, (
            "the worker never got back to claiming after the outage ended"
        )


class _FailsFirstNClaims:
    """The real queue with a database outage in front of the claim path.

    Wraps rather than reimplements, so every method except `claim` is the
    genuine one and the job really is claimed, executed and settled once the
    outage lifts.
    """

    def __init__(self, inner: PostgresJobQueue, *, failures: int) -> None:
        self._inner = inner
        self._failures = failures
        self.attempts = 0
        self.recovered = asyncio.Event()

    async def claim(
        self, session: AsyncSession, *, worker_id: str, limit: int = 1
    ) -> list[ClaimedJob]:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise ProgrammingError('relation "job" does not exist', None, Exception())
        self.recovered.set()
        return await self._inner.claim(session, worker_id=worker_id, limit=limit)

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        kind: JobKind,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        run_after: datetime | None = None,
    ) -> uuid.UUID | None:
        return await self._inner.enqueue(
            session,
            kind=kind,
            payload=payload,
            dedupe_key=dedupe_key,
            max_attempts=max_attempts,
            run_after=run_after,
        )

    async def heartbeat(self, session: AsyncSession, job_id: uuid.UUID, *, worker_id: str) -> bool:
        return await self._inner.heartbeat(session, job_id, worker_id=worker_id)

    async def succeed(self, session: AsyncSession, job_id: uuid.UUID, *, worker_id: str) -> bool:
        return await self._inner.succeed(session, job_id, worker_id=worker_id)

    async def fail(
        self,
        session: AsyncSession,
        job: ClaimedJob,
        *,
        worker_id: str,
        error: str,
        permanent: bool,
    ) -> bool:
        return await self._inner.fail(
            session, job, worker_id=worker_id, error=error, permanent=permanent
        )

    async def reap_expired_leases(self, session: AsyncSession) -> int:
        return await self._inner.reap_expired_leases(session)


class TestClockSkew:
    """A worker's clock must not decide who owns a job.

    The reaper judges a lease against the database's now(). Before this, the
    queue computed the lease from the worker's own clock, so the two had to
    agree -- and a worker VM that drifts behind after the host sleeps, which
    WSL2 does, would write leases already expired from the database's point of
    view. The reaper would then return live work to pending and a second
    worker would run it. Every timestamp the queue compares is now written by
    the database.
    """

    async def test_a_worker_minutes_behind_still_holds_a_valid_lease(
        self,
        queue: PostgresJobQueue,
        sessionmaker_: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import threshold.adapters.queue as queue_module

        real = queue_module.datetime

        class TenMinutesSlow(real):  # type: ignore[misc, valid-type]
            @classmethod
            def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
                return real.now(tz) - timedelta(minutes=10)

        # A worker whose clock has drifted ten minutes behind the database's.
        monkeypatch.setattr(queue_module, "datetime", TenMinutesSlow)

        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            claimed = await queue.claim(s, worker_id="slow-clock", limit=1)
            await s.commit()
        assert len(claimed) == 1

        async with sessionmaker_() as s:
            reaped = await queue.reap_expired_leases(s)
            await s.commit()
        assert reaped == 0, "the reaper took a live job from a worker whose clock is slow"

        async with sessionmaker_() as s:
            row = (await s.execute(select(Job).where(Job.id == claimed[0].id))).scalar_one()
        assert row.state == JobState.CLAIMED.value
        assert row.claimed_by == "slow-clock"

    async def test_a_retry_is_scheduled_on_the_database_clock(
        self,
        queue: PostgresJobQueue,
        sessionmaker_: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker ten minutes behind must not schedule a retry in the past."""
        import threshold.adapters.queue as queue_module

        real = queue_module.datetime

        class TenMinutesSlow(real):  # type: ignore[misc, valid-type]
            @classmethod
            def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
                return real.now(tz) - timedelta(minutes=10)

        monkeypatch.setattr(queue_module, "datetime", TenMinutesSlow)

        await enqueue_n(queue, sessionmaker_, 1)
        async with sessionmaker_() as s:
            job = (await queue.claim(s, worker_id="slow-clock", limit=1))[0]
            await queue.fail(s, job, worker_id="slow-clock", error="transient", permanent=False)
            await s.commit()

        async with sessionmaker_() as s:
            # Immediately claimable would mean the backoff was computed from a
            # clock ten minutes in the past, and the retry skipped its delay.
            assert await queue.claim(s, worker_id="other", limit=1) == []
