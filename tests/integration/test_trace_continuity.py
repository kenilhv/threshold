"""One invoice, one trace, across two queue hops and two processes.

`ARCHITECTURE.md` has promised since M0 that every audit event carries "trace_id
— the same id as the distributed trace". This is the test that makes the
sentence checkable. Before M5 it was false twice over: the column held the
caller's request id, and every row the worker wrote held NULL.

The four rows are reached by joining through the entities they describe, which
is the only honest way to ask the question. Selecting rows `WHERE trace_id = ...`
and then asserting the trace ids match would be circular.

Requires: make up
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.unit.test_fixture_layouts import build_spec, render

pytestmark = pytest.mark.integration

API = "http://localhost:8000"
DATABASE_URL = "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold"

#: Every audit row an auto-approved invoice produces. Four, not five: the
#: automatic approval and a human decision are alternatives, never both.
EXPECTED_ACTIONS = [
    "document.received",
    "document.extracted",
    "match.decided",
    "invoice.auto_approved",
]

STORY = text("""
    with story as (
        select id from document where id = :document_id
        union all select id from extraction where document_id = :document_id
        union all select id from match_result where document_id = :document_id
        union all select id from approval_decision where document_id = :document_id
    )
    select ae.action, ae.actor_type, ae.trace_id
    from audit_event ae
    join story on story.id = ae.entity_id
    order by ae.occurred_at
""")


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The same async engine every other integration test uses.

    Deliberately not a second, synchronous driver: adding psycopg2 so one test
    can run a query is a dependency the service does not have, and
    `migrations/env.py` already refuses that trade for the same reason.
    """
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres is not reachable; `make up`")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30) as c:
        try:
            c.get(f"{API}/healthz")
        except httpx.HTTPError:
            pytest.skip("the stack is not running; `make up`")
        yield c


async def _story(
    sessions: async_sessionmaker[AsyncSession], document_id: str
) -> list[tuple[str, str, str | None]]:
    async with sessions() as session:
        result = await session.execute(STORY, {"document_id": document_id})
        return [(row[0], row[1], row[2]) for row in result]


async def _submit_and_wait(client: httpx.Client, sessions: async_sessionmaker[AsyncSession]) -> str:
    spec = dataclasses.replace(build_spec(), invoice_number=f"TRACE-{uuid.uuid4().hex[:8].upper()}")
    path = Path(f".trace-{spec.invoice_number}.pdf")
    try:
        render("continental_tabular", spec, path)
        response = client.post(
            f"{API}/v1/documents",
            files={"file": (path.name, path.read_bytes(), "application/pdf")},
        )
        response.raise_for_status()
        document_id: str = response.json()["document_id"]
    finally:
        path.unlink(missing_ok=True)

    for _ in range(60):
        if client.get(f"{API}/v1/documents/{document_id}/match").status_code == 200:
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("the worker never matched the invoice")

    for _ in range(20):
        if len(await _story(sessions, document_id)) >= len(EXPECTED_ACTIONS):
            break
        await asyncio.sleep(0.25)
    return document_id


class TestOneInvoiceOneTrace:
    async def test_every_audit_row_shares_one_trace_id(
        self, client: httpx.Client, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The sentence M5 exists to make true.

        A failure here means the trace stopped crossing the queue — the worker
        rows would carry their own ids, or NULL.
        """
        document_id = await _submit_and_wait(client, sessions)
        story = await _story(sessions, document_id)

        assert [action for action, _, _ in story] == EXPECTED_ACTIONS, story

        trace_ids = {trace_id for _, _, trace_id in story}
        assert None not in trace_ids, f"an audit row has no trace: {story}"
        assert len(trace_ids) == 1, f"one invoice produced {len(trace_ids)} traces: {story}"

        only = trace_ids.pop()
        assert only is not None
        assert len(only) == 32
        assert only == only.lower()
        assert int(only, 16) != 0, "a zero trace id is what an invalid span formats to"

    async def test_the_worker_rows_are_the_ones_that_used_to_be_null(
        self, client: httpx.Client, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Names the specific regression rather than only the property.

        document.extracted and match.decided are written by the worker, in a
        different process, after two queue hops.
        """
        document_id = await _submit_and_wait(client, sessions)
        by_action = {
            action: trace_id for action, _, trace_id in await _story(sessions, document_id)
        }

        assert by_action["document.extracted"] is not None
        assert by_action["match.decided"] is not None
        assert by_action["document.extracted"] == by_action["document.received"]

    async def test_two_invoices_do_not_share_a_trace(
        self, client: httpx.Client, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Guards against the way this test could pass while proving nothing.

        A constant trace id — or a bug that grouped everything under one — would
        satisfy every assertion above.
        """
        first = await _submit_and_wait(client, sessions)
        second = await _submit_and_wait(client, sessions)
        assert (await _story(sessions, first))[0][2] != (await _story(sessions, second))[0][2]
