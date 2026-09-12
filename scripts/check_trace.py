"""Take a trace id out of the audit table and find it in Tempo.

Prints the number of distinct services the trace touched, which `make smoke`
compares against 2. That number is the whole M5 claim in one integer: an audit
row in Postgres, a trace in Tempo, and the same work seen from two processes.

Deliberately not a check that "tracing is configured". Configuration was
correct twice in M5 while the result was wrong -- the column held a request id,
and the SQL spans were never emitted because an instrumentor patched a function
name that had already been imported. Both looked fine from the inside.
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold"
TEMPO = "http://localhost:3200"

#: The newest decision the MATCHER made, not simply the newest audit row.
#:
#: That distinction is the test. Most rows are written by one process and their
#: traces are correctly single-service: a person approving an invoice through
#: the console never touches the worker, so its trace naming only the API is
#: right rather than broken. `match.decided` is written by the worker, about
#: work the API started, so its trace has to span both -- and if it does not,
#: the queue hop lost the context.
NEWEST_TRACE = text(
    "select trace_id from audit_event "
    "where action = 'match.decided' and trace_id is not null "
    "order by occurred_at desc limit 1"
)


async def newest_trace_id() -> str | None:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(NEWEST_TRACE)).first()
            return None if row is None else str(row[0])
    finally:
        await engine.dispose()


def services_in(trace_id: str) -> set[str]:
    response = httpx.get(f"{TEMPO}/api/traces/{trace_id}", timeout=15)
    response.raise_for_status()
    return {
        attribute["value"]["stringValue"]
        for batch in response.json().get("batches", [])
        for attribute in batch["resource"]["attributes"]
        if attribute["key"] == "service.name"
    }


def both_services(trace_id: str, *, budget: int = 25) -> set[str]:
    """Poll until the trace is whole, or the budget runs out.

    A trace arrives in pieces. The batch span processor holds spans for five
    seconds before exporting, the API and the worker run on their own timers,
    and the audit row this id came from is written *inside* the work rather
    than after it -- so the row is in Postgres before the spans describing it
    reach Tempo. Asking once and reporting "one service" would be measuring
    that gap rather than the property.
    """
    seen: set[str] = set()
    for _ in range(budget):
        try:
            seen = services_in(trace_id)
        except httpx.HTTPError:
            seen = set()
        if len(seen) >= 2:
            return seen
        time.sleep(1)
    return seen


def main() -> int:
    trace_id = asyncio.run(newest_trace_id())
    if trace_id is None:
        print("0")
        return 1

    services = both_services(trace_id)
    if not services:
        print(f"tempo has nothing for trace {trace_id}", file=sys.stderr)
        print("0")
        return 1

    print(len(services))
    print(f"trace {trace_id}: {sorted(services)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
