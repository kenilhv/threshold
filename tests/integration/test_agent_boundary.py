"""The agent's perimeter, enforced by Postgres rather than by this repository.

The unit suite checks that no tool is *named* like a write and that the
escalation vocabulary is closed. Both are checks on intention, and intention is
what changes when somebody adds a helper in a hurry.

This file checks the mechanism. Every tool receives a session opened with
`SET TRANSACTION READ ONLY`, so a write through it fails at the database
regardless of what the calling code believed it was doing. That is a property
nothing in `src/` can quietly revoke, and the only way to know it holds is to
try it against a real Postgres.

Requires: make up
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from threshold.agent import tools
from threshold.domain.models import (
    Document,
    MatchException,
    MatchResult,
    PurchaseOrder,
    Vendor,
    VendorBankAccount,
)
from threshold.domain.projection import restricted_columns

pytestmark = pytest.mark.integration

DATABASE_URL = "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold"


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres is not reachable; `make up`")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestTheDatabaseRefusesTheAgentsWrites:
    """Not "the agent does not write" — "the agent cannot"."""

    async def test_an_insert_is_refused(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        async with tools.read_only(sessions) as session:
            with pytest.raises(DBAPIError, match="read-only"):
                await session.execute(
                    text("insert into audit_event (id, action) values (:id, 'agent.wrote')"),
                    {"id": str(uuid.uuid4())},
                )

    async def test_an_update_is_refused(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """The one that matters most: an escalation is a request, and an UPDATE
        to `approval_decision` would make it a decision."""
        async with tools.read_only(sessions) as session:
            with pytest.raises(DBAPIError, match="read-only"):
                await session.execute(text("update approval_decision set decision = 'approved'"))

    async def test_a_delete_is_refused(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        async with tools.read_only(sessions) as session:
            with pytest.raises(DBAPIError, match="read-only"):
                await session.execute(text("delete from match_exception"))

    async def test_reads_still_work(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """The vacuity guard. A session that refused everything would satisfy
        all three tests above and make the agent useless rather than safe."""
        async with tools.read_only(sessions) as session:
            assert (await session.execute(text("select 1"))).scalar_one() == 1
            await session.execute(select(MatchResult).limit(1))

    async def test_the_refusal_is_the_database_and_not_the_orm(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Raw SQL, straight past SQLAlchemy's session machinery.

        If the guarantee lived in the ORM it would end at the first piece of
        code that reached for `text()`. It does not: the transaction itself is
        read-only, so this fails in the server.
        """
        async with tools.read_only(sessions) as session:
            with pytest.raises(DBAPIError) as caught:
                await session.execute(text("create table agent_was_here (id uuid primary key)"))
            assert "read-only" in str(caught.value).lower()


class TestTheToolsCannotReachRestrictedData:
    """The agent cannot exfiltrate what it never held.

    The mechanism turns out to be stronger than the one ARCHITECTURE.md
    describes. It says restricted columns are dropped from the projection, which
    is true — but the agent never gets that far, because every table holding
    restricted data is one it has no tool to query. Tax identifiers live on
    `vendor` and payment details on `vendor_bank_account`, and the agent reads
    neither.

    Both checks are kept. The projection is the mechanism that would matter if a
    column on one of the four readable tables were ever classified restricted,
    and asserting it now is what makes that future change safe rather than
    silent.
    """

    @staticmethod
    async def _first_match(session: AsyncSession) -> MatchResult | None:
        """A match result that will still be there in a moment.

        Deliberately not the newest. The worker writes match results constantly,
        so `order by decided_at desc` hands back a row that may be mid-pipeline
        -- and worse, a different row on each call inside the same test, which
        is how this failed under load while passing in isolation.

        The oldest is stable: nothing rewrites it, and the projection is a pure
        function of the row, so which one is used does not affect what is being
        proved.
        """
        return (
            (
                await session.execute(
                    select(MatchResult)
                    .where(MatchResult.purchase_order_id.is_not(None))
                    .order_by(MatchResult.decided_at.asc())
                )
            )
            .scalars()
            .first()
        )

    def test_restricted_data_exists_somewhere(self) -> None:
        """The vacuity guard for everything below.

        If nothing in the schema were classified restricted, every assertion in
        this class would pass against a system with no privacy controls at all.
        """
        assert restricted_columns(Vendor)
        assert restricted_columns(VendorBankAccount)

    def test_the_agent_has_no_tool_for_the_tables_that_hold_it(self) -> None:
        """The actual reason the agent never sees a bank number.

        Stated as a property of the registry rather than of the projection,
        because that is where it is enforced: there is no `get_vendor`.
        """
        reachable = set(tools.TOOLS)
        assert not {name for name in reachable if "vendor" in name or "bank" in name}, reachable

    def test_the_readable_tables_declare_nothing_restricted(self) -> None:
        """Which is why the projections below come back whole.

        If this ever fails it is not a bug: it means somebody classified a
        column on a table the agent reads, and the next test is the one that
        then starts doing work.
        """
        for model in (MatchResult, MatchException, Document, PurchaseOrder):
            assert not restricted_columns(model), (
                f"{model.__name__} now declares restricted columns; the projection must "
                "drop them and the agent's output should be re-checked"
            )

    async def test_every_tool_output_survives_the_projection(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The mechanism, exercised even while it has nothing to remove.

        A projection that silently stopped running would pass a
        column-name check today and fail the day a column is classified. This
        asserts it ran and returned something.
        """
        async with tools.read_only(sessions) as session:
            match = await self._first_match(session)
            if match is None:
                pytest.skip("no match results yet; run `make smoke`")
            projected = await tools.get_match(session, match.id)
            exceptions = await tools.get_exceptions(session, match.id)

        assert projected is not None
        assert "disposition" in projected, "the projection returned nothing useful"
        for row in exceptions:
            assert not (set(row) & restricted_columns(MatchException))

    async def test_no_tool_returns_a_bank_number_or_a_tax_id(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The claim ARCHITECTURE.md makes in words: the agent sees
        `bank_changed: true` and never a bank number.

        Checked by value rather than by column name, because the way this
        breaks is a restricted value arriving under an innocent key.
        """
        async with tools.read_only(sessions) as session:
            match = await self._first_match(session)
            if match is None:
                pytest.skip("no match results yet; run `make smoke`")

            document = await tools.get_document(session, match.document_id)
            order = (
                None
                if match.purchase_order_id is None
                else await tools.get_purchase_order(session, match.purchase_order_id)
            )
            payloads: list[Any] = [
                await tools.get_match(session, match.id),
                await tools.get_exceptions(session, match.id),
                document,
                order,
            ]

            secrets = (
                (
                    await session.execute(
                        text(
                            "select tax_id from vendor where tax_id is not null "
                            "union all select account_number_last4 from vendor_bank_account "
                            "union all select routing_number_last4 from vendor_bank_account"
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert secrets, "no restricted values in the database; this checks nothing"
        rendered = repr(payloads)
        leaked = [value for value in secrets if value and str(value) in rendered]
        assert not leaked, f"a restricted value reached the agent: {leaked}"


class TestTheClassificationFailsClosed:
    async def test_an_undeclared_column_is_treated_as_restricted(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Forgetting to classify a column must not open the agent's window.

        Asserted here as well as in the projection tests because this is the
        rule the agent's safety rests on, and a rule with one test is a rule
        with one place to break.
        """
        from threshold.domain.classification import Sensitivity, sensitivity_of

        class _Unclassified:
            info: ClassVar[dict[str, Any]] = {}

        assert sensitivity_of(_Unclassified()) is Sensitivity.RESTRICTED  # type: ignore[arg-type]
