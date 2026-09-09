"""Worker entrypoint.

In M0 this proves the process starts, reaches both datastores, and shuts down
cleanly on SIGTERM. The polling loop it wraps is M2 — a Postgres-backed queue
using ``SELECT ... FOR UPDATE SKIP LOCKED`` with leases, backoff and a dead
letter queue.

The graceful-shutdown handling is here from the start rather than added later
because it is the part that is awkward to retrofit: once work is in flight, the
question "what happens to a job when the container is told to stop" has to be
answered by the loop's structure, not by a signal handler bolted on top.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from types import FrameType

import redis.asyncio as aioredis
import structlog
from sqlalchemy import text

from threshold.adapters.db import dispose_engine, init_engine, session_scope
from threshold.config.settings import get_settings
from threshold.observability.logging import configure_logging

log = structlog.get_logger(__name__)

IDLE_INTERVAL_SECONDS = 5.0


class Worker:
    def __init__(self) -> None:
        self._stopping = asyncio.Event()

    def request_stop(self, signum: int, _frame: FrameType | None = None) -> None:
        log.info("worker.stop_requested", signal=signal.Signals(signum).name)
        self._stopping.set()

    async def run(self) -> None:
        settings = get_settings()
        configure_logging(settings)
        init_engine(settings)

        redis_client = aioredis.from_url(str(settings.redis_url))

        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
            await redis_client.ping()
            log.info("worker.ready", queue="postgres", note="polling loop lands in M2")

            while not self._stopping.is_set():
                # M2 replaces this with a claim-and-execute cycle. Waiting on the
                # stop event rather than sleeping means SIGTERM is honoured
                # immediately instead of after the interval elapses.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=IDLE_INTERVAL_SECONDS)
        finally:
            await redis_client.aclose()
            await dispose_engine()
            log.info("worker.stopped")


async def _main() -> None:
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, worker.request_stop, sig, None)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
