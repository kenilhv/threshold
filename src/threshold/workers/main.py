"""Worker entrypoint.

Starts the claim loop and translates SIGTERM into a graceful stop: the loop
stops taking new work immediately and lets the job in flight finish. Killing it
mid-job would be safe — the lease expires and another worker picks it up — but
repeating work that was seconds from done is waste, and for anything with a side
effect it is worse than waste.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from threshold.adapters.db import dispose_engine, init_engine
from threshold.config.settings import get_settings
from threshold.observability.logging import configure_logging
from threshold.observability.tracing import configure_tracing
from threshold.workers.runtime import WorkerRuntime

log = structlog.get_logger(__name__)


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings)
    configure_tracing(settings)
    init_engine(settings)

    runtime = WorkerRuntime()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, runtime.request_stop)

    try:
        await runtime.run_forever()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
