"""A matching job that runs twice must not pay the invoice twice.

This file is the reproduction from docs/THREAT-MODEL.md §5, kept as a test. The
steps are the ones the defect took in production shape: a handler outlives its
lease, the reaper hands the job to a second worker, and both run the matcher
against one document. That makes match result v1 and v2, two `auto_approved`
decisions, and two payment jobs. Before migration 0014 both jobs settled, with
two different rail keys, and the books showed twice the invoice paid.

The worker no longer commits a run whose lease it lost (see `test_queue.py`),
which closes the path that was reproduced. What is tested here is the backstop
for every other path to a second authorisation: `uq_payment_live_per_document`,
and `PaymentService` turning its refusal into a dead letter instead of an
IntegrityError.

The two runs are called directly rather than through two workers because the
property under test is what the payment executor does once a second
authorisation exists. How the second authorisation came to exist is the part
that has more than one answer.

Requires: make up
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.databases import ISOLATED
from tests.integration.test_matching import _document_with_extraction, _invoice_fields
from tests.integration.test_payments import (
    AlwaysAccepts,
    AlwaysRejects,
    balances,
    cleanup,
    run_to_completion,
)

from threshold.domain.approval import Decision
from threshold.domain.models import (
    ApprovalDecision,
    Document,
    LedgerTransaction,
    MatchResult,
    Payment,
)
from threshold.domain.payments import PaymentState
from threshold.services.matching import MatchingService
from threshold.services.payments import PaymentRefusedError, PaymentService

pytestmark = pytest.mark.integration

#: Isolated: nothing else may settle, fail or clean up these payments mid-test.
DATABASE_URL = ISOLATED


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


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """The same narrow cleanup `test_payments.py` uses, for the same reason.

    Afterwards only. A payment left behind by a failed run here is exactly the
    kind of row migration 0014 refuses to build its index over, so the cleanup
    matters more in this file than in most.
    """
    yield
    async with sessions() as session:
        await cleanup(session)


async def _clean_document(session: AsyncSession) -> Document:
    """One invoice that the matcher will auto-approve, with nothing to object to."""
    document = await _document_with_extraction(
        session, _invoice_fields(invoice_number=f"TWICE-{uuid.uuid4().hex[:8].upper()}")
    )
    await session.commit()
    return document


async def _match_twice(session: AsyncSession, document_id: uuid.UUID) -> list[uuid.UUID]:
    """What two workers holding one job do: match, commit, match, commit.

    Returns the two approval decisions, oldest match first, after asserting the
    runs produced the shape the reproduction did. If the matcher ever stopped
    auto-approving the second run, the refusal below would be untested rather
    than failing, so the shape is checked rather than assumed.
    """
    first = await MatchingService().match_document(session, document_id)
    await session.commit()
    second = await MatchingService().match_document(session, document_id)
    await session.commit()

    assert (first.version, second.version) == (1, 2)
    assert first.disposition == second.disposition == "auto_approve", (
        first.disposition,
        second.disposition,
    )

    rows = (
        await session.execute(
            select(ApprovalDecision.id, ApprovalDecision.decision)
            .join(MatchResult, MatchResult.id == ApprovalDecision.match_result_id)
            .where(MatchResult.document_id == document_id)
            .order_by(MatchResult.version)
        )
    ).all()
    assert [decision for _, decision in rows] == [Decision.AUTO_APPROVED.value] * 2, rows
    return [decision_id for decision_id, _ in rows]


async def _payments_for(session: AsyncSession, document_id: uuid.UUID) -> list[Payment]:
    return list(
        (await session.execute(select(Payment).where(Payment.document_id == document_id)))
        .scalars()
        .all()
    )


async def _accruals_for(session: AsyncSession, document_id: uuid.UUID) -> int:
    """How many times the ledger recorded that we owe this invoice.

    An accrual cites the match result, not the payment, so it is counted across
    every match result the document has. Two would mean the liability was
    booked twice even if the cash was not.
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(LedgerTransaction)
            .join(MatchResult, MatchResult.id == LedgerTransaction.match_result_id)
            .where(MatchResult.document_id == document_id)
            .where(LedgerTransaction.kind == "accrual")
        )
    ).scalar_one()


class TestAJobThatRanTwicePaysOnce:
    async def test_the_second_authorisation_is_refused_and_the_invoice_is_paid_once(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The reproduction, run to the end. It used to finish with two settled
        payments and twice the invoice gone; the second drain now stops at its
        first step, naming the payment that already went."""
        async with sessions() as session:
            # Held as plain values: the rollback below expires every object in
            # the session, and an expired attribute cannot be loaded lazily here.
            document_id = (await _clean_document(session)).id
            first, second = await _match_twice(session, document_id)
            service = PaymentService(AlwaysAccepts())

            paid = await run_to_completion(session, service, first)
            assert paid.state == PaymentState.SETTLED.value
            paid_id = paid.id

            with pytest.raises(PaymentRefusedError, match=str(paid_id)):
                await service.advance(session, second)
            # Counted before the rollback, inside the refused attempt's own
            # transaction. Afterwards the rollback would hide an accrual posted
            # ahead of the insert, and this would pass whatever the order was.
            assert await _accruals_for(session, document_id) == 1, (
                "the refused payment still booked a second liability for one invoice"
            )
            await session.rollback()

            payments = await _payments_for(session, document_id)
            assert [(p.id, p.state) for p in payments] == [(paid_id, PaymentState.SETTLED.value)]
            assert await _accruals_for(session, document_id) == 1
            assert sum((await balances(session)).values()) == 0

    async def test_the_refusal_holds_while_the_first_payment_is_still_in_flight(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """Two payment jobs interleave in production; they do not queue politely.

        `pending` is the first live state, before anything has been posted
        against the payment, and the most likely one for the other job to find.
        An index that only counted settled payments would pass the test above
        and miss this.
        """
        async with sessions() as session:
            document_id = (await _clean_document(session)).id
            first, second = await _match_twice(session, document_id)
            service = PaymentService(AlwaysAccepts())

            pending, more = await service.advance(session, first)
            await session.commit()
            assert (pending.state, more) == (PaymentState.PENDING.value, True)
            pending_id = pending.id

            with pytest.raises(PaymentRefusedError, match="already being paid"):
                await service.advance(session, second)
            assert await _accruals_for(session, document_id) == 1  # before the rollback, as above
            await session.rollback()

            assert [p.id for p in await _payments_for(session, document_id)] == [pending_id]

    async def test_two_documents_are_each_paid(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The vacuity guard. An index keyed on something coarser than the
        document -- the vendor, the purchase order, nothing at all -- would pass
        both tests above by refusing every second payment in the database."""
        async with sessions() as session:
            one = await _clean_document(session)
            other = await _clean_document(session)
            service = PaymentService(AlwaysAccepts())

            for document in (one, other):
                result = await MatchingService().match_document(session, document.id)
                await session.commit()
                assert result.disposition == "auto_approve", result.disposition
                decision = (
                    await session.execute(
                        select(ApprovalDecision.id).where(
                            ApprovalDecision.match_result_id == result.id
                        )
                    )
                ).scalar_one()
                paid = await run_to_completion(session, service, decision)
                assert paid.state == PaymentState.SETTLED.value

            for document in (one, other):
                states = [p.state for p in await _payments_for(session, document.id)]
                assert states == [PaymentState.SETTLED.value], (document.id, states)

    async def test_a_failed_payment_does_not_block_the_next(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The index is partial, and this is why.

        A payment the rail rejected has been posted back, and the supplier is
        owed again. Paying them through a later authorisation is the designed
        recovery, so a full unique index on the document -- simpler, and the
        obvious first draft -- would turn a closed account into a debt this
        system could never settle.
        """
        async with sessions() as session:
            document = await _clean_document(session)
            first, second = await _match_twice(session, document.id)

            failed = await run_to_completion(session, PaymentService(AlwaysRejects()), first)
            assert failed.state == PaymentState.FAILED.value

            paid = await run_to_completion(session, PaymentService(AlwaysAccepts()), second)
            assert paid.state == PaymentState.SETTLED.value

            states = sorted(p.state for p in await _payments_for(session, document.id))
            assert states == [PaymentState.FAILED.value, PaymentState.SETTLED.value]
            assert sum((await balances(session)).values()) == 0
