"""The worker loop.

Claim, execute, settle. The interesting parts are the failure paths.

**One transaction per job.** The handler runs inside it, and the settle — mark
succeeded, or schedule a retry, or dead-letter — commits with the handler's own
writes. A crash between "the work happened" and "the queue knows" is therefore
impossible, because they are the same commit.

**Graceful shutdown finishes what it started.** On SIGTERM the loop stops
claiming immediately but lets the job in flight run to completion. Killing it
mid-job would be safe — the lease expires and someone picks it up — but doing
the same work twice when waiting a few seconds avoids it is wasteful, and for
anything with a side effect it is worse than wasteful.

**The reaper runs under an advisory lock.** It is idempotent, so concurrent runs
would be harmless, but there is no reason for eight workers to do the same scan
every few seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from datetime import timedelta
from typing import Any, Final

import structlog
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract
from opentelemetry.trace import Link, SpanKind
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from threshold.adapters.db import session_scope
from threshold.adapters.queue import (
    LINKED_TRACEPARENT_KEY,
    TRACEPARENT_KEY,
    ClaimedJob,
    PostgresJobQueue,
    ReapableQueue,
    advisory_lock,
)
from threshold.domain.jobs import PermanentJobError, TransientJobError
from threshold.observability.metrics import (
    JobOutcome,
    publish_payment_snapshot,
    publish_queue_snapshot,
    record_job_execution,
    record_queue_wait,
)
from threshold.services.payments import PaymentService
from threshold.workers.handlers import HANDLERS

log = structlog.get_logger(__name__)

_tracer = trace.get_tracer(__name__)


def _job_context(payload: dict[str, Any]) -> tuple[Context | None, list[Link]]:
    """Where this job's span belongs in the trace graph.

    Continuing the producer's trace is a deliberate override of the usual
    messaging guidance, which prefers a link because a consumer may run
    arbitrarily later. The bounds here are known rather than assumed: three
    attempts including the first, five then ten seconds of backoff, a one-second
    poll and a two-minute lease with a thirty-second reaper. Measured over 317
    completed jobs, the wait from available to claimed has a median of 0.503s
    and a maximum of 1.084s -- the poll interval, arrived at uniformly -- rising
    to tens of seconds only when a job has burned backoff, and to a few minutes
    after a crash-and-reap cycle. Against that, linking would give one invoice
    four different trace ids across its four audit rows, and "the same id as the
    distributed trace" is the promise this milestone exists to make true.

    That trade is only valid while those bounds hold. A longer backoff, a bigger
    attempt budget, or jobs scheduled hours ahead invalidate it and it must be
    decided again rather than inherited. See D-021.

    A replayed job is the exception and gets a new trace with a link, because a
    human chose to run it again, possibly days later.
    """
    traceparent = payload.get(TRACEPARENT_KEY)
    if isinstance(traceparent, str):
        return extract({"traceparent": traceparent}), []

    linked = payload.get(LINKED_TRACEPARENT_KEY)
    if isinstance(linked, str):
        context = trace.get_current_span(extract({"traceparent": linked})).get_span_context()
        if context.is_valid:
            return None, [Link(context)]

    # A job enqueued before this existed, or one produced with no active trace.
    return None, []


#: SQLSTATE for insufficient_privilege.
_INSUFFICIENT_PRIVILEGE = "42501"


def _refused_by_privilege(exc: BaseException) -> bool:
    """Whether Postgres refused this for lack of a grant, however it was wrapped.

    SQLAlchemy raises `ProgrammingError`, whose `orig` is the driver's adapter,
    whose cause is asyncpg's `InsufficientPrivilegeError`. Which layer carries
    the code has changed between releases, so every link is asked.
    """
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        for attribute in ("sqlstate", "pgcode"):
            if getattr(link, attribute, None) == _INSUFFICIENT_PRIVILEGE:
                return True
        link = getattr(link, "orig", None) or link.__cause__ or link.__context__
    return False


def _outcome_of(dead_lettered: bool) -> JobOutcome:
    """A failure that will be retried and one that has given up are different.

    They are the same log line today and were the same silence before that, but
    an alert on "jobs are failing" that cannot separate the two fires on a
    transient blip and on a poison message identically.
    """
    return JobOutcome.DEAD_LETTERED if dead_lettered else JobOutcome.RETRYING


#: How long to wait when the queue is empty. Short enough that work starts
#: promptly, long enough that an idle pool is not hammering Postgres. At eight
#: workers this is one cheap indexed query per worker per second.
POLL_INTERVAL: Final = timedelta(seconds=1)

REAP_INTERVAL: Final = timedelta(seconds=30)

#: Cap on the backoff after a failed claim. A claim that raises has claimed
#: nothing, so retrying is always safe and the only question is how hard to
#: hammer a database that is not answering. Kept low because the pool is blind
#: to new work for the whole delay — thirty seconds of lag after an outage is
#: cheap; a pool that gave up is not.
CLAIM_RETRY_MAX: Final = timedelta(seconds=30)

#: Arbitrary but fixed, and namespaced high enough not to collide with anything
#: else that might take an advisory lock in this database. Any worker holding it
#: is the one running the reaper.
REAPER_LOCK_KEY: Final = 0x7485_5170


def worker_identity() -> str:
    """Identifies which process holds a claim.

    Host plus pid plus a random suffix: the host and pid make it possible to go
    and look at the thing holding a stuck lease, and the suffix keeps two
    restarts of the same pid distinguishable.
    """
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:6]}"


class WorkerRuntime:
    def __init__(
        self,
        *,
        queue: ReapableQueue | None = None,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        batch_size: int = 1,
        poll_interval: timedelta = POLL_INTERVAL,
    ) -> None:
        self.identity = worker_identity()
        self._queue: ReapableQueue = queue or PostgresJobQueue()
        self._sessions = sessions
        self._batch_size = batch_size
        self._poll = poll_interval.total_seconds()
        self._stopping = asyncio.Event()
        self._reaper_holds_lock = False

    def request_stop(self) -> None:
        self._stopping.set()

    @property
    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    async def run_forever(self) -> None:
        log.info("worker.started", worker=self.identity, batch_size=self._batch_size)
        reaper = asyncio.create_task(self._reap_loop())
        # Consecutive claim failures, reset by any successful claim. Only the
        # claim path needs this: a failure inside `_execute` is already settled
        # against the job, and the reaper guards itself.
        failures = 0
        try:
            while not self._stopping.is_set():
                try:
                    claimed = await self._claim_batch()
                except Exception:
                    # Postgres restarting, a failover, a migration running
                    # against a live pool, a network blip. None of these are
                    # this worker's fault and none of them are permanent, so
                    # exiting would turn a ten-second outage into a pool that
                    # never comes back and needs a human to notice.
                    failures += 1
                    delay = min(self._poll * 2**failures, CLAIM_RETRY_MAX.total_seconds())
                    log.exception(
                        "worker.claim_failed", consecutive=failures, retry_in_seconds=delay
                    )
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    continue
                failures = 0

                if not claimed:
                    # Nothing to do. Wait on the stop event rather than sleeping
                    # so SIGTERM is honoured immediately.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), timeout=self._poll)
                    continue
                for job in claimed:
                    await self._execute(job)
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            log.info("worker.stopped", worker=self.identity)

    async def _claim_batch(self) -> list[ClaimedJob]:
        async with session_scope(self._sessions) as session:
            return await self._queue.claim(session, worker_id=self.identity, limit=self._batch_size)

    async def _execute(self, job: ClaimedJob) -> None:
        # Recorded before anything can go wrong, and for every kind including
        # one this build has no handler for: the wait is a fact about the queue
        # that has already happened by the time we are here, and a queue that is
        # falling behind on jobs nobody can run is still falling behind.
        record_queue_wait(kind=job.kind, seconds=job.waited.total_seconds())
        started = time.perf_counter()

        handler = HANDLERS.get(job.kind)
        if handler is None:
            # An unknown kind means this worker is running older code than
            # whatever enqueued the job. Permanent from this process's point of
            # view: no retry here will grow it a handler.
            async with session_scope(self._sessions) as session:
                await self._queue.fail(
                    session,
                    job,
                    worker_id=self.identity,
                    error=f"no handler registered for kind {job.kind!r}",
                    permanent=True,
                )
            # This path used to return before any telemetry started, so the one
            # failure that means "the deployment is inconsistent" was the one
            # failure that produced nothing to see.
            record_job_execution(
                kind=job.kind,
                outcome=JobOutcome.NO_HANDLER,
                seconds=time.perf_counter() - started,
            )
            return

        bound = log.bind(job_id=str(job.id), kind=job.kind, attempt=job.attempts)
        context, links = _job_context(job.payload)
        try:
            # One transaction: the handler's writes and the success marker
            # commit together, so the queue can never disagree with the work.
            #
            # The span wraps the transaction rather than sitting inside the
            # handler, so every audit row the handler writes inherits the trace
            # -- which is the whole point of carrying it across the queue.
            with _tracer.start_as_current_span(
                f"process {job.kind}",
                context=context,
                links=links,
                kind=SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "postgresql",
                    "messaging.destination.name": "job",
                    "messaging.message.id": str(job.id),
                    "messaging.operation.name": "process",
                    "threshold.job.kind": job.kind,
                    "threshold.job.attempt": job.attempts,
                },
            ):
                async with session_scope(self._sessions) as session:
                    await handler(session, job.payload)
                    await self._queue.succeed(session, job.id, worker_id=self.identity)
            self._record(job, JobOutcome.SUCCEEDED, started)
            bound.info("worker.job_succeeded")

        except PermanentJobError as exc:
            dead = await self._settle_failure(job, str(exc), permanent=True)
            self._record(job, _outcome_of(dead), started)
            bound.warning("worker.job_failed_permanently", error=str(exc))

        except TransientJobError as exc:
            dead = await self._settle_failure(job, str(exc), permanent=False)
            self._record(job, _outcome_of(dead), started)
            bound.info("worker.job_failed_transiently", error=str(exc))

        except Exception as exc:
            if _refused_by_privilege(exc):
                # SQLSTATE 42501 could not happen while the application was a
                # superuser, so nothing classified it and it fell through to
                # "transient" below: every job of the affected kind waited out
                # its whole backoff, three times, to be refused identically.
                # A missing grant does not heal between attempts. It is fixed by
                # a migration, after which `make replay` brings these back.
                message = (
                    f"refused by Postgres (42501): {exc}. Either the role this worker "
                    "connects as was not granted something its handler needs, or a row-level "
                    "policy refused the row it tried to write. Both are fixed in a migration "
                    "or in the handler, and neither by retrying."
                )
                dead = await self._settle_failure(job, message, permanent=True)
                self._record(job, _outcome_of(dead), started)
                bound.critical("worker.job_refused_by_database", error=str(exc))
                return

            # Unclassified. Treated as transient because an unexpected exception
            # is more often a bug in the handler than a poison payload, and the
            # attempt budget bounds the cost of guessing wrong.
            dead = await self._settle_failure(job, f"{type(exc).__name__}: {exc}", permanent=False)
            self._record(job, _outcome_of(dead), started)
            bound.exception("worker.job_failed_unexpectedly")

    def _record(self, job: ClaimedJob, outcome: JobOutcome, started: float) -> None:
        record_job_execution(kind=job.kind, outcome=outcome, seconds=time.perf_counter() - started)

    async def _settle_failure(self, job: ClaimedJob, error: str, *, permanent: bool) -> bool:
        """Record the failure in its own transaction. True if dead-lettered.

        Separate from the handler's transaction on purpose: that one has already
        rolled back, and the failure must be recorded regardless of why.
        """
        async with session_scope(self._sessions) as session:
            return await self._queue.fail(
                session, job, worker_id=self.identity, error=error, permanent=permanent
            )

    async def _observe_queue(self, session: AsyncSession) -> None:
        """Publish the queue's depth, from the one worker elected to say it.

        Called only by the lock holder. Queue depth is a property of the queue,
        so if every worker published it a backend would receive N identical
        series for one number and `sum()` would report N times the truth. The
        reaper's advisory lock is already an election held for the same reason,
        and reusing it means there is one answer to "who speaks for the queue"
        rather than two mechanisms that can disagree.

        The reading is taken here, on the worker's event loop, because the
        observable gauges that publish it run their callbacks on the exporter's
        thread, where there is no loop to await a database on.
        """
        stats = await self._queue.stats(session)
        publish_queue_snapshot(
            depth_by_state=stats,
            oldest_pending_age_seconds=await self._queue.oldest_claimable_age(session),
            dead_letters_unreplayed=stats.get("dead_letters_unreplayed", 0),
        )

        # The money, read by the same elected worker on the same cadence. One
        # election rather than two, for the reason the queue's docstring gives:
        # two mechanisms for "who speaks for the system" can disagree, and then
        # `sum()` reports N times the truth.
        counts, oldest_submitting = await PaymentService().in_flight(session)
        publish_payment_snapshot(
            count_by_state=counts,
            oldest_submitting_age_seconds=oldest_submitting,
        )

    async def _reap_loop(self) -> None:
        """Return work abandoned by workers that died holding a claim.

        Also the system's own observer: the elected worker reads the queue here
        on the same cadence it reaps, which is the only place a periodic read of
        the whole queue belongs.
        """
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=REAP_INTERVAL.total_seconds())
            if self._stopping.is_set():
                return
            try:
                async with session_scope(self._sessions) as session:
                    if not self._reaper_holds_lock:
                        self._reaper_holds_lock = await advisory_lock(session, REAPER_LOCK_KEY)
                        if not self._reaper_holds_lock:
                            continue
                    await self._queue.reap_expired_leases(session)
                    await self._observe_queue(session)
            except Exception:
                # The reaper failing must never take the worker down with it.
                log.exception("worker.reaper_failed")
