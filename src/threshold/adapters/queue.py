"""A durable job queue on Postgres.

The claim is one statement:

    UPDATE job SET state='claimed', ... WHERE id IN (
        SELECT id FROM job
        WHERE state='pending' AND run_after <= now()
        ORDER BY run_after, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT n
    ) RETURNING *

``SKIP LOCKED`` is what makes this a queue rather than a bottleneck. Without it,
the second worker to arrive blocks on the row the first is locking and the pool
serialises. With it, each worker takes the next rows nobody else holds, and
throughput scales with workers until the database itself is the limit.

``FOR UPDATE`` without ``SKIP LOCKED`` is the classic mistake here, and it does
not show up in a single-worker test — which is why there is a concurrency test.

Crash recovery is the lease. A worker that dies stops renewing
``lease_expires_at``; the reaper finds rows whose lease has passed and returns
them to ``pending``. Nothing is lost because nothing was ever removed: a claim
is a state change, not a delete, so a dead worker's work is still sitting in the
table waiting to be noticed.

That is the whole crash story, and it is why the demo for this milestone is
killing a worker mid-run and watching the job resume.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol, cast, runtime_checkable

import structlog
from opentelemetry.propagate import inject
from sqlalchemy import CursorResult, Select, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.domain.jobs import (
    DEFAULT_LEASE,
    DEFAULT_MAX_ATTEMPTS,
    JobKind,
    JobState,
    backoff_for,
)
from threshold.domain.models import DeadLetter, Job

log = structlog.get_logger(__name__)

#: Where the trace rides across the queue.
#:
#: The payload rather than a column, which is the weaker choice on two axes --
#: a column would be queryable and would keep the payload purely business data
#: -- and the right one here. A column needs a migration, a `ClaimedJob` field,
#: an entry in claim()'s RETURNING list, an explicit copy in fail() and a
#: decision in replay(); this repository contained proof that such copy paths
#: get missed, because `DeadLetter.dedupe_key` was declared at M2 and nothing
#: wrote it until M8 -- five files, exactly as predicted here. The payload needs
#: none of that: it survives dead-lettering and replay because fail() copies it
#: verbatim, and a handler that has never heard of these keys ignores them.
#:
#: Underscore-prefixed because they are transport metadata, not business data.
TRACEPARENT_KEY: Final = "_traceparent"

#: Set by replay only. A replayed job is a human decision taken possibly days
#: later with a fresh attempt budget, so it starts its own trace and merely
#: points back at the one that died. Without this, a replay silently rejoins a
#: trace from last week and the new work appears to have taken six days.
LINKED_TRACEPARENT_KEY: Final = "_linked_traceparent"


def _with_trace_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy the payload with the current trace context attached.

    A copy, not a mutation: the caller's dict belongs to the caller, and
    ingest passes one it also logs.
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    traceparent = carrier.get("traceparent")
    if traceparent is None:
        return payload
    return {**payload, TRACEPARENT_KEY: traceparent}


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A job this worker holds a lease on."""

    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_expires_at: datetime
    #: What this job was deduplicated on, carried so that dying with it is not
    #: the same as losing it. `fail` copies it onto the dead letter and
    #: `replay_dead_letter` puts it back, which is the only reason a replayed
    #: job can be suppressed as a duplicate.
    dedupe_key: str | None
    #: How long this job sat claimable before anyone took it.
    #:
    #: Computed by the database inside the claim, as ``now() - run_after``, and
    #: not by subtracting two timestamps in Python. The worker's clock has no
    #: part in it, for the reason D-019 gives: the reaper and the claim
    #: predicate both compare against the database's ``now()``, so a worker
    #: whose clock has drifted after a host sleep would report a queue latency
    #: that says more about the VM than the queue.
    waited: timedelta

    @property
    def is_final_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


@runtime_checkable
class Queue(Protocol):
    """The only queue surface the application depends on.

    Narrower than ``PostgresJobQueue`` on purpose. Producers need ``enqueue``
    and workers need the claim/settle cycle; the operational verbs — reap,
    replay, purge, stats — belong to ``queuectl`` and an operator, not to
    business code, so binding a service to this protocol makes it impossible
    for a request handler to quietly start purging the queue.

    It also states the cost of the design decision in D-008. Every method takes
    the caller's ``AsyncSession``, because enqueueing has to commit in the same
    transaction as the state change that caused it. A broker-backed
    implementation cannot honour that, and the signature says so rather than
    leaving a reader to discover it. An SQS adapter would satisfy this shape
    only by accepting at-least-once delivery with a real duplicate window, which
    is exactly the trade to make deliberately rather than by accident.
    """

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        kind: JobKind,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        run_after: datetime | None = None,
    ) -> uuid.UUID | None: ...

    async def claim(
        self, session: AsyncSession, *, worker_id: str, limit: int = 1
    ) -> list[ClaimedJob]: ...

    async def heartbeat(
        self, session: AsyncSession, job_id: uuid.UUID, *, worker_id: str
    ) -> bool: ...

    async def succeed(
        self, session: AsyncSession, job_id: uuid.UUID, *, worker_id: str
    ) -> None: ...

    async def fail(
        self,
        session: AsyncSession,
        job: ClaimedJob,
        *,
        worker_id: str,
        error: str,
        permanent: bool,
    ) -> bool: ...


@runtime_checkable
class ReapableQueue(Queue, Protocol):
    """What a worker needs, which is strictly more than a producer needs.

    Lease recovery is on this and not on ``Queue`` because it is the one
    operational verb a worker legitimately owns: it is the mechanism by which a
    pool heals itself after one of its own members dies. A request handler has
    no business calling it.

    Observation is on this for the same reason. ``Queue``'s docstring reserves
    the operational verbs for ``queuectl`` and an operator, and reading the
    depth is not an exception to that so much as the other half of it: a pool
    that heals itself is also the thing that should be able to say how far
    behind it is. Publishing that reading is still restricted to one worker —
    see ``_observe_queue`` — because queue depth is a property of the queue and
    eight workers reporting it would make ``sum()`` report eight times the
    truth.
    """

    async def reap_expired_leases(self, session: AsyncSession) -> int: ...

    async def stats(self, session: AsyncSession) -> dict[str, int]: ...

    async def oldest_claimable_age(self, session: AsyncSession) -> float | None: ...


class PostgresJobQueue:
    """The default queue. See ``docs/ARCHITECTURE.md`` for why not a broker."""

    def __init__(self, lease: timedelta = DEFAULT_LEASE) -> None:
        self._lease = lease

    # ------------------------------------------------------------- producing

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
        """Add work. Returns None when a live job already covers this key.

        The caller is expected to run this inside the same transaction as the
        state change that motivated it. That is the entire reason this is a
        table and not a broker: enqueue and state change commit together, so
        there is no window in which one happened and the other did not.

        The current trace context is attached to the payload here rather than at
        each call site, so a new producer cannot forget it.
        """
        values: dict[str, Any] = {
            "kind": kind.value,
            "payload": _with_trace_context(payload),
            "dedupe_key": dedupe_key,
            "max_attempts": max_attempts,
        }
        if run_after is not None:
            values["run_after"] = run_after

        base = insert(Job).values(**values)
        if dedupe_key is not None:
            # Two things are load-bearing here.
            #
            # First, this is applied to the Insert before .returning(): the
            # postgresql dialect exposes on_conflict_* on Insert, not on
            # ReturningInsert, so the other order neither type-checks nor runs.
            #
            # Second, index_where must REPEAT the partial index's predicate
            # exactly. Postgres infers the arbiter index from the conflict
            # target, and a bare ON CONFLICT (dedupe_key) cannot match a partial
            # unique index — it fails with "no unique or exclusion constraint
            # matching the ON CONFLICT specification". Every deduped enqueue
            # would raise. An integration test caught this; nothing in a
            # single-statement unit test would have.
            #
            # The predicate is narrow on purpose: a key collides only with
            # pending or claimed rows, so the same logical work can be enqueued
            # again once an earlier run finished.
            base = base.on_conflict_do_nothing(
                index_elements=[Job.dedupe_key],
                index_where=text("dedupe_key IS NOT NULL AND state IN ('pending', 'claimed')"),
            )

        job_id = (await session.execute(base.returning(Job.id))).scalar_one_or_none()
        if job_id is None:
            log.info("queue.duplicate_suppressed", kind=kind.value, dedupe_key=dedupe_key)
        return job_id

    # ------------------------------------------------------------- consuming

    def _claimable(self) -> Select[tuple[uuid.UUID]]:
        return (
            select(Job.id)
            .where(Job.state == JobState.PENDING.value, Job.run_after <= func.now())
            .order_by(Job.run_after, Job.created_at)
            .with_for_update(skip_locked=True)
        )

    async def claim(
        self, session: AsyncSession, *, worker_id: str, limit: int = 1
    ) -> list[ClaimedJob]:
        """Take up to ``limit`` jobs nobody else holds, oldest first.

        The ``ORDER BY`` inside the subquery decides *which* rows are claimed.
        It does not decide the order they come back in: ``UPDATE … RETURNING``
        makes no promise about row order, and in practice returns them in heap
        order, which drifts from insertion order as soon as the table has seen
        deletes and starts reusing free space.

        So the rows are re-sorted here. A queue that selects oldest-first and
        then hands a worker its batch in arbitrary order is FIFO in the part
        nobody observes and not FIFO in the part they do.
        """
        # The database's clock, never this process's. The reaper decides a lease
        # has expired by comparing it with the database's now(), so a lease
        # computed from the worker's clock is only as good as the agreement
        # between two clocks -- and a worker VM that has drifted a few minutes
        # behind after a host sleep writes leases that are already expired, so
        # the reaper hands live work to a second worker. models.py has always
        # claimed timestamps come from the database; the queue did not.
        stmt = (
            update(Job)
            .where(Job.id.in_(self._claimable().limit(limit).scalar_subquery()))
            .values(
                state=JobState.CLAIMED.value,
                claimed_by=worker_id,
                claimed_at=func.now(),
                lease_expires_at=func.now() + self._lease,
                attempts=Job.attempts + 1,
            )
            .returning(
                Job.id,
                Job.kind,
                Job.payload,
                Job.attempts,
                Job.max_attempts,
                Job.lease_expires_at,
                Job.dedupe_key,
                Job.run_after,
                Job.created_at,
                # The wait, measured by the one clock that governs the claim.
                # `now()` here is the same transaction timestamp written into
                # `claimed_at` on the line above, so this is exactly
                # `claimed_at - run_after` without a second column or a second
                # opinion about what time it is.
                (func.now() - Job.run_after).label("waited"),
            )
        )
        rows = sorted(
            (await session.execute(stmt)).all(), key=lambda r: (r.run_after, r.created_at)
        )
        return [
            ClaimedJob(
                id=r.id,
                kind=r.kind,
                payload=r.payload,
                attempts=r.attempts,
                max_attempts=r.max_attempts,
                lease_expires_at=r.lease_expires_at,
                dedupe_key=r.dedupe_key,
                waited=r.waited,
            )
            for r in rows
        ]

    async def heartbeat(self, session: AsyncSession, job_id: uuid.UUID, *, worker_id: str) -> bool:
        """Extend the lease on work still in progress.

        Returns False when the lease was already lost — the reaper decided this
        worker was dead and gave the job away. The worker must then STOP, because
        someone else is now doing this work and finishing would double-process
        it. Long jobs must therefore check the return value, not fire and forget.
        """
        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.claimed_by == worker_id,
                Job.state == JobState.CLAIMED.value,
            )
            # Database clock, for the reason given in claim().
            .values(lease_expires_at=func.now() + self._lease)
            .returning(Job.id)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def succeed(self, session: AsyncSession, job_id: uuid.UUID, *, worker_id: str) -> None:
        await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.claimed_by == worker_id)
            .values(
                state=JobState.SUCCEEDED.value,
                lease_expires_at=None,
                last_error=None,
            )
        )

    async def fail(
        self,
        session: AsyncSession,
        job: ClaimedJob,
        *,
        worker_id: str,
        error: str,
        permanent: bool,
    ) -> bool:
        """Record a failure. Returns True when the job was dead-lettered.

        A permanent failure skips the remaining attempts entirely. Retrying a
        corrupt document three times wastes the budget and delays it reaching a
        human by the length of the backoff, which helps nobody.
        """
        exhausted = permanent or job.is_final_attempt

        if not exhausted:
            # Database clock: the claim predicate compares run_after with now().
            retry_at = (
                await session.execute(
                    update(Job)
                    .where(Job.id == job.id, Job.claimed_by == worker_id)
                    .values(
                        state=JobState.PENDING.value,
                        claimed_by=None,
                        claimed_at=None,
                        lease_expires_at=None,
                        run_after=func.now() + backoff_for(job.attempts),
                        last_error=error[:4000],
                    )
                    # The value the database actually stored, so the log line
                    # reports the retry time that will govern the claim rather
                    # than this process's opinion of it.
                    .returning(Job.run_after)
                )
            ).scalar_one_or_none()
            log.info(
                "queue.retry_scheduled",
                job_id=str(job.id),
                attempt=job.attempts,
                retry_at=retry_at.isoformat() if retry_at else None,
            )
            return False

        session.add(
            DeadLetter(
                original_job_id=job.id,
                kind=job.kind,
                payload=job.payload,
                attempts=job.attempts,
                dedupe_key=job.dedupe_key,
                final_error=error[:4000],
                failure_kind="permanent" if permanent else "transient",
            )
        )
        await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.claimed_by == worker_id)
            .values(
                state=JobState.DEAD.value,
                claimed_by=None,
                claimed_at=None,
                lease_expires_at=None,
                last_error=error[:4000],
            )
        )
        log.warning(
            "queue.dead_lettered",
            job_id=str(job.id),
            attempts=job.attempts,
            permanent=permanent,
        )
        return True

    # -------------------------------------------------------------- recovery

    async def reap_expired_leases(self, session: AsyncSession) -> int:
        """Return jobs whose worker stopped renewing their lease.

        This is crash recovery in five lines. A worker killed mid-run leaves a
        claimed row with a lease that stops advancing; once it passes, the job
        goes back to pending and the next poll picks it up.

        Attempts are NOT incremented here. The claim already counted this try,
        and counting it twice would halve the effective retry budget of every
        job whose worker happened to die.
        """
        stmt = (
            update(Job)
            .where(
                Job.state == JobState.CLAIMED.value,
                Job.lease_expires_at < func.now(),
            )
            .values(
                state=JobState.PENDING.value,
                claimed_by=None,
                claimed_at=None,
                lease_expires_at=None,
                last_error="lease expired; worker presumed dead",
            )
            .returning(Job.id)
        )
        reaped = (await session.execute(stmt)).all()
        if reaped:
            log.warning("queue.leases_reaped", count=len(reaped))
        return len(reaped)

    async def replay_dead_letter(
        self, session: AsyncSession, dead_letter_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Put a dead job back on the queue with a fresh attempt budget.

        The dead-letter row is marked rather than deleted, so the history of
        what failed and when survives the fix.
        """
        letter = (
            await session.execute(
                select(DeadLetter).where(
                    DeadLetter.id == dead_letter_id, DeadLetter.replayed_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if letter is None:
            return None

        # A replay is a new trace, linked to the dead one rather than joined
        # to it. The original context is days old by the time a human decides to
        # replay; inheriting it would make the retry look like a six-day
        # operation and would bury today's work inside last week's trace.
        payload = dict(letter.payload)
        died_in = payload.pop(TRACEPARENT_KEY, None)
        payload.pop(LINKED_TRACEPARENT_KEY, None)
        if died_in is not None:
            payload[LINKED_TRACEPARENT_KEY] = died_in

        # The key goes back on, which is the whole reason the column exists.
        #
        # It used to pass None, reasoning that "a replay is an explicit human
        # decision to run this again". That is true of one letter and false of
        # `queuectl replay --all`, which is already shipped: two letters can
        # share a key -- job A dies with `extract:{digest}`, the key frees
        # because the partial index only covers pending and claimed, a producer
        # enqueues B with the same key, B dies too -- and replaying both then
        # runs the same work twice. On the extract path that is a second model
        # call and a spurious extraction version; nothing downstream refuses it.
        job_id = await self.enqueue(
            session,
            kind=JobKind(letter.kind),
            payload=payload,
            dedupe_key=letter.dedupe_key,
        )

        # Only a replay that produced a job counts as replayed.
        #
        # This guard is unreachable until the line above passes a real key, and
        # is written with it rather than after it: `enqueue` returns None when
        # the partial index suppresses the insert, and marking the letter anyway
        # would drop it out of `ix_dead_letter_unreplayed`, out of the
        # `dead_letters_unreplayed` gauge and out of the DeadLettersUnreplayed
        # alert -- an operator's to-do item deleted by the act of failing to
        # act on it.
        if job_id is None:
            log.info(
                "queue.replay_suppressed",
                dead_letter_id=str(dead_letter_id),
                dedupe_key=letter.dedupe_key,
            )
            return None

        letter.replayed_at = func.now()  # database clock, like everything else here
        log.info("queue.replayed", dead_letter_id=str(dead_letter_id), job_id=str(job_id))
        return job_id

    # ---------------------------------------------------------------- upkeep

    async def purge_finished(self, session: AsyncSession, *, older_than: timedelta) -> int:
        """Delete succeeded jobs past their useful life.

        The partial claim index keeps the hot path fast regardless of table
        size, but the table itself still grows, and nothing here needs a
        permanent record of every successful extraction — the audit log has
        that. Dead jobs are never purged; they are the ones somebody still has
        to look at.
        """
        cutoff = func.now() - older_than
        result = await session.execute(
            delete(Job).where(Job.state == JobState.SUCCEEDED.value, Job.updated_at < cutoff)
        )
        # execute() is typed as Result; a DELETE always yields a CursorResult,
        # which is where rowcount lives.
        return cast("CursorResult[Any]", result).rowcount or 0

    async def oldest_claimable_age(self, session: AsyncSession) -> float | None:
        """Seconds the longest-waiting claimable job has been waiting, or None.

        ``None`` when nothing is claimable, which is deliberately different from
        zero: zero is what an empty queue and a job that arrived this instant
        would both report, and the only question this answers is "has something
        been waiting too long".

        Subtracted inside the database for the reason the claim is:
        `run_after` was written by the database and must be compared with the
        database's own clock.
        """
        waited = (
            await session.execute(
                select(func.extract("epoch", func.now() - func.min(Job.run_after))).where(
                    Job.state == JobState.PENDING.value, Job.run_after <= func.now()
                )
            )
        ).scalar_one()
        # min() over no rows is NULL, so an empty queue answers None without a
        # second round trip to ask whether it was empty.
        return None if waited is None else float(waited)

    async def stats(self, session: AsyncSession) -> dict[str, int]:
        """Queue depth by state, for the dashboard and the alert rules in M5."""
        rows = (await session.execute(select(Job.state, func.count()).group_by(Job.state))).all()
        # str()/int() rather than dict(rows): the Row tuples are typed loosely
        # enough that a bare dict() fails mypy, and the conversion is honest
        # about what these columns actually are.
        counts: dict[str, int] = {str(state): int(count) for state, count in rows}
        pending_overdue = (
            await session.execute(
                select(func.count())
                .select_from(Job)
                .where(Job.state == JobState.PENDING.value, Job.run_after <= func.now())
            )
        ).scalar_one()
        counts["claimable_now"] = pending_overdue
        counts["dead_letters_unreplayed"] = (
            await session.execute(
                select(func.count()).select_from(DeadLetter).where(DeadLetter.replayed_at.is_(None))
            )
        ).scalar_one()
        return counts


async def advisory_lock(session: AsyncSession, key: int) -> bool:
    """Try to take a transaction-scoped advisory lock, without blocking.

    Used so exactly one worker runs the reaper at a time. The reaper is
    idempotent and would be harmless run concurrently, but there is no reason
    for eight workers to do the same scan every few seconds.

    `pg_try_advisory_xact_lock`, not `pg_try_advisory_lock`. The session-level
    version was the M9 threat model's own weakness table entry, and the failure
    is subtler than "the lock leaks": the caller cached the result in a Python
    flag and never asked again, so when the pooled connection holding the lock
    was recycled Postgres released it while the worker went on believing it was
    the elected reaper. A second worker could then take the same lock, and both
    would reap and publish queue observations. Invisible at one worker, which is
    the deployment this has always run.

    A transaction-scoped lock releases at COMMIT, so it has to be re-taken every
    cycle. That is the point: the election is re-run each time rather than
    remembered, and there is no cached state to go stale.
    """
    return bool(
        (await session.execute(select(text(f"pg_try_advisory_xact_lock({key})")))).scalar_one()
    )
