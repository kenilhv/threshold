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
import uuid
from datetime import timedelta
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from threshold.adapters.db import session_scope
from threshold.adapters.queue import (
    ClaimedJob,
    PostgresJobQueue,
    ReapableQueue,
    advisory_lock,
)
from threshold.domain.jobs import PermanentJobError, TransientJobError
from threshold.workers.handlers import HANDLERS

log = structlog.get_logger(__name__)

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
            return

        bound = log.bind(job_id=str(job.id), kind=job.kind, attempt=job.attempts)
        try:
            # One transaction: the handler's writes and the success marker
            # commit together, so the queue can never disagree with the work.
            async with session_scope(self._sessions) as session:
                await handler(session, job.payload)
                await self._queue.succeed(session, job.id, worker_id=self.identity)
            bound.info("worker.job_succeeded")

        except PermanentJobError as exc:
            await self._settle_failure(job, str(exc), permanent=True)
            bound.warning("worker.job_failed_permanently", error=str(exc))

        except TransientJobError as exc:
            await self._settle_failure(job, str(exc), permanent=False)
            bound.info("worker.job_failed_transiently", error=str(exc))

        except Exception as exc:
            # Unclassified. Treated as transient because an unexpected exception
            # is more often a bug in the handler than a poison payload, and the
            # attempt budget bounds the cost of guessing wrong.
            await self._settle_failure(job, f"{type(exc).__name__}: {exc}", permanent=False)
            bound.exception("worker.job_failed_unexpectedly")

    async def _settle_failure(self, job: ClaimedJob, error: str, *, permanent: bool) -> None:
        """Record the failure in its own transaction.

        Separate from the handler's transaction on purpose: that one has already
        rolled back, and the failure must be recorded regardless of why.
        """
        async with session_scope(self._sessions) as session:
            await self._queue.fail(
                session, job, worker_id=self.identity, error=error, permanent=permanent
            )

    async def _reap_loop(self) -> None:
        """Return work abandoned by workers that died holding a claim."""
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
            except Exception:
                # The reaper failing must never take the worker down with it.
                log.exception("worker.reaper_failed")
