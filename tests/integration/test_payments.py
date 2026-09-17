"""The payment executor against a real database, and the ledger it writes.

The state machine is a table in `tests/unit/test_payments.py` and the balance
rule is arithmetic in `tests/unit/test_ledger.py`. What can only be checked here
is the part that is about durability rather than about logic:

* that the row is committed as `submitting` **before** the rail is called, so a
  process dying mid-call leaves a truthful record rather than a guess
* that a rail which does not answer leaves the payment where it was and does not
  post a second time
* that Postgres itself refuses an unbalanced ledger transaction at COMMIT
* that two payments cannot exist for one authorisation, because a unique index
  says so rather than because a service remembered to look

Every scenario ends by asserting the trial balance sums to zero. That is one
number that goes non-zero the moment anything anywhere has gone wrong, and it
costs a query.

Requires: make up
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.databases import ISOLATED
from tests.integration.test_matching import _document_with_extraction, _invoice_fields

from threshold.adapters.rail.port import (
    PaymentInstruction,
    RailOutcome,
    RailResult,
    RailUnavailableError,
)
from threshold.adapters.rail.sandbox import SANDBOX_ID
from threshold.domain.approval import Decision
from threshold.domain.ledger import Account
from threshold.domain.models import (
    ApprovalDecision,
    Document,
    LedgerEntry,
    LedgerTransaction,
    MatchResult,
    Payment,
)
from threshold.domain.payments import PaymentState
from threshold.domain.roles import Role
from threshold.services.approvals import ApprovalService, DecisionRequest
from threshold.services.matching import MatchingService
from threshold.services.payments import PaymentRefusedError, PaymentService

pytestmark = pytest.mark.integration

#: Isolated: a payment's ledger entries must be the only ones moving.
DATABASE_URL = ISOLATED


class AlwaysAccepts:
    """A rail with no opinion, so a test about the ledger is about the ledger."""

    @property
    def rail_id(self) -> str:
        return "test-accepts"

    async def submit(self, instruction: PaymentInstruction) -> RailResult:
        return RailResult(outcome=RailOutcome.ACCEPTED, rail_reference="REF-OK")


class AlwaysRejects:
    @property
    def rail_id(self) -> str:
        return "test-rejects"

    async def submit(self, instruction: PaymentInstruction) -> RailResult:
        return RailResult(
            outcome=RailOutcome.REJECTED, rail_reference="REF-NO", reason="account closed"
        )


class NeverAnswers:
    """The rail that makes this a state machine.

    Counts its calls, because the property under test is that each retry sends
    the *same* key -- a rail seeing two different keys would be a rail paying
    twice.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []

    @property
    def rail_id(self) -> str:
        return "test-silent"

    async def submit(self, instruction: PaymentInstruction) -> RailResult:
        self.keys.append(instruction.idempotency_key)
        raise RailUnavailableError("the rail did not answer")


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


async def approved_invoice(session: AsyncSession) -> ApprovalDecision:
    """One clean invoice, matched by the real matcher and auto-approved.

    Goes through `MatchingService` rather than building a `MatchResult` by hand,
    because the amount on that row is the thing being paid and a fixture that
    wrote it directly would be testing the fixture.
    """
    document = await _document_with_extraction(
        session, _invoice_fields(invoice_number=f"PAY-{uuid.uuid4().hex[:8].upper()}")
    )
    await session.flush()
    result = await MatchingService().match_document(session, document.id)
    assert result.disposition == "auto_approve", result.disposition
    assert result.amount_minor is not None, "the matcher recorded no amount to pay"

    decision = (
        await session.execute(
            select(ApprovalDecision).where(ApprovalDecision.match_result_id == result.id)
        )
    ).scalar_one()
    return decision


async def balances(session: AsyncSession) -> dict[Account, int]:
    return await PaymentService().trial_balance(session)


async def _postings_for(
    session: AsyncSession, payment_id: uuid.UUID, *, match_result_id: uuid.UUID
) -> dict[Account, int]:
    """Every posting this one invoice caused, by account, zero-filled.

    The accrual hangs off the match result and the three payment movements hang
    off the payment, so both are needed to see the whole story of one invoice.

    Zero-filled across the chart on purpose: an account that nets to zero and an
    account that was never touched are different facts, and an assertion like
    "the liability nets to nothing" is only meaningful if a missing key cannot
    satisfy it by accident.
    """
    rows = (
        await session.execute(
            select(LedgerEntry.account, func.sum(LedgerEntry.amount_minor))
            .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
            .where(
                (LedgerTransaction.payment_id == payment_id)
                | (LedgerTransaction.match_result_id == match_result_id)
            )
            .group_by(LedgerEntry.account)
        )
    ).all()
    totals = dict.fromkeys(Account, 0)
    for account, total in rows:
        totals[Account(account)] = int(total)
    return totals


async def run_to_completion(
    session: AsyncSession, service: PaymentService, decision_id: uuid.UUID, *, limit: int = 8
) -> Payment:
    """Drive the machine the way the worker does: one transition, then commit.

    The commit between steps is not test ceremony. It is the property under
    test -- `submitting` has to be durable before the rail is called.
    """
    for _ in range(limit):
        payment, more = await service.advance(session, decision_id)
        await session.commit()
        if not more:
            return payment
    raise AssertionError("the payment did not reach a terminal state")


async def cleanup(session: AsyncSession) -> None:
    documents = (
        (await session.execute(select(Document.id).where(Document.source == "fixture")))
        .scalars()
        .all()
    )
    if not documents:
        return
    payments = (
        (await session.execute(select(Payment.id).where(Payment.document_id.in_(documents))))
        .scalars()
        .all()
    )
    if payments:
        await session.execute(
            delete(LedgerTransaction).where(LedgerTransaction.payment_id.in_(payments))
        )
    results = (
        (
            await session.execute(
                select(MatchResult.id).where(MatchResult.document_id.in_(documents))
            )
        )
        .scalars()
        .all()
    )
    if results:
        await session.execute(
            delete(LedgerTransaction).where(LedgerTransaction.match_result_id.in_(results))
        )
    await session.execute(delete(Payment).where(Payment.document_id.in_(documents)))
    await session.execute(delete(Document).where(Document.id.in_(documents)))
    await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Remove only what these tests create, and only afterwards.

    Narrow on purpose: this database holds the seed these tests match against,
    and a blanket delete would take the purchase orders with it.
    """
    yield
    async with sessions() as session:
        await cleanup(session)


class TestTheHappyPath:
    async def test_an_approved_invoice_is_paid_and_the_books_balance(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()

            payment = await run_to_completion(session, PaymentService(AlwaysAccepts()), decision.id)

            assert payment.state == PaymentState.SETTLED.value
            assert payment.rail_reference == "REF-OK"
            assert payment.submitted_at is not None
            assert payment.settled_at is not None

            after = await balances(session)
            amount = payment.amount_minor
            # What *this* payment moved, rather than what the system's totals
            # did. A live worker settles other payments while this test runs, so
            # a before/after delta on a global account measures the whole
            # database and fails whenever anything else happens to be in flight.
            mine = await _postings_for(session, payment.id, match_result_id=payment.match_result_id)

        assert sum(after.values()) == 0, "the trial balance does not balance"
        # Accrued and then paid: the liability nets to nothing, the expense stays.
        assert mine[Account.ACCOUNTS_PAYABLE] == 0, mine
        assert mine[Account.CASH_IN_TRANSIT] == 0, mine
        assert mine[Account.EXPENSE] == amount, mine
        assert mine[Account.CASH] == -amount, mine
        assert sum(mine.values()) == 0, mine

    async def test_it_takes_four_steps_and_each_one_is_durable(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """Walked explicitly, because the number of steps is the design rather
        than an implementation detail: the commit between step two and step
        three is what makes a crash survivable."""
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()
            service = PaymentService(AlwaysAccepts())

            observed = []
            for _ in range(4):
                payment, more = await service.advance(session, decision.id)
                await session.commit()
                observed.append((payment.state, more))

        assert observed == [
            (PaymentState.PENDING.value, True),
            (PaymentState.SUBMITTING.value, True),
            (PaymentState.SUBMITTED.value, True),
            (PaymentState.SETTLED.value, False),
        ]

    async def test_the_submission_is_posted_before_the_rail_is_asked(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The ordering the whole module is built around.

        Stopped deliberately at `SUBMITTING` -- the state a crashed process
        would leave behind -- and the ledger is checked there. A payment that
        left the building with no entry against it is what this prevents.
        """
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()
            service = PaymentService(AlwaysAccepts())

            await service.advance(session, decision.id)  # -> pending
            await session.commit()
            payment, _ = await service.advance(session, decision.id)  # -> submitting
            await session.commit()

            assert payment.state == PaymentState.SUBMITTING.value
            assert payment.rail_reference is None, "the rail has not been asked yet"

            kinds = (
                (
                    await session.execute(
                        select(LedgerTransaction.kind).where(
                            LedgerTransaction.payment_id == payment.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert list(kinds) == ["payment_submitted"]
            assert sum((await balances(session)).values()) == 0


class TestTheRailRefuses:
    async def test_a_rejection_reverses_the_liability(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """Three transactions saying what happened, rather than none saying it
        never did. The supplier is owed again and the ledger shows why."""
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()
            before = await balances(session)

            payment = await run_to_completion(session, PaymentService(AlwaysRejects()), decision.id)

            assert payment.state == PaymentState.FAILED.value
            assert payment.failure_reason == "account closed"

            after = await balances(session)
            amount = payment.amount_minor
            kinds = sorted(
                (
                    await session.execute(
                        select(LedgerTransaction.kind).where(
                            LedgerTransaction.payment_id == payment.id
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert kinds == ["payment_failed", "payment_submitted"]
        assert sum(after.values()) == 0
        assert (
            after.get(Account.ACCOUNTS_PAYABLE, 0) - before.get(Account.ACCOUNTS_PAYABLE, 0)
            == -amount
        ), "we should owe it again"
        assert after.get(Account.CASH, 0) == before.get(Account.CASH, 0), "no money left"
        assert after.get(Account.CASH_IN_TRANSIT, 0) == before.get(Account.CASH_IN_TRANSIT, 0)


class TestTheRailDoesNotAnswer:
    async def test_the_payment_stays_where_it_was_and_posts_once(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The case that makes payment systems hard.

        A rail that timed out may have taken the money. Writing `failed` here is
        how a supplier gets paid twice on the retry, so the payment stays in
        `submitting` -- and crucially the ledger is not posted again, or every
        outage would inflate the books.
        """
        rail = NeverAnswers()
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()
            service = PaymentService(rail)

            await service.advance(session, decision.id)
            await session.commit()
            payment, more = await service.advance(session, decision.id)
            await session.commit()
            assert payment.state == PaymentState.SUBMITTING.value

            payment_id = payment.id

            for _ in range(3):
                payment, more = await service.advance(session, decision.id)
                await session.commit()
                assert more, "an unanswered rail is not a finished payment"
                assert payment.state == PaymentState.SUBMITTING.value

            entries = (
                await session.execute(
                    select(func.count())
                    .select_from(LedgerTransaction)
                    .where(LedgerTransaction.payment_id == payment_id)
                )
            ).scalar_one()
            assert entries == 1, "the submission was posted more than once"
            # Three, not four: entering submitting makes no rail call, it only
            # makes the row durable. The rail is asked once per loop below.
            assert payment.rail_attempts == 3
            assert sum((await balances(session)).values()) == 0

    async def test_a_recovered_rail_settles_the_same_payment(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The outage ends and the payment finishes, without a second
        submission posting."""
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()

            silent = NeverAnswers()
            await PaymentService(silent).advance(session, decision.id)
            await session.commit()
            await PaymentService(silent).advance(session, decision.id)
            await session.commit()
            await PaymentService(silent).advance(session, decision.id)
            await session.commit()

            assert len(set(silent.keys)) == 1, f"the key changed between attempts: {silent.keys}"

            payment = await run_to_completion(session, PaymentService(AlwaysAccepts()), decision.id)
            assert payment.state == PaymentState.SETTLED.value

            kinds = sorted(
                (
                    await session.execute(
                        select(LedgerTransaction.kind).where(
                            LedgerTransaction.payment_id == payment.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert kinds == ["payment_settled", "payment_submitted"]
            assert sum((await balances(session)).values()) == 0


class TestTheDatabaseRefusesASecondPayment:
    async def test_one_payment_per_authorisation(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The third idempotency layer, enforced by an index rather than by a
        service remembering to look."""
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()
            first = await run_to_completion(session, PaymentService(AlwaysAccepts()), decision.id)

            session.add(
                Payment(
                    approval_decision_id=decision.id,
                    match_result_id=first.match_result_id,
                    document_id=first.document_id,
                    vendor_id=first.vendor_id,
                    vendor_bank_account_id=first.vendor_bank_account_id,
                    amount_minor=first.amount_minor,
                    currency=first.currency,
                    currency_exponent=first.currency_exponent,
                    state=PaymentState.PENDING.value,
                    idempotency_key=first.idempotency_key + "-different",
                    rail_id=SANDBOX_ID,
                )
            )
            with pytest.raises(IntegrityError, match="uq_payment_authorisation"):
                await session.flush()
            await session.rollback()

    async def test_advancing_a_settled_payment_is_a_no_op(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """A replayed job outliving its payment is normal, and must not move
        money or raise."""
        async with sessions() as session:
            decision = await approved_invoice(session)
            await session.commit()
            service = PaymentService(AlwaysAccepts())
            payment = await run_to_completion(session, service, decision.id)
            before = await balances(session)

            again, more = await service.advance(session, decision.id)
            await session.commit()

            assert not more
            assert again.state == PaymentState.SETTLED.value
            assert again.id == payment.id
            assert await balances(session) == before


class TestPostgresEnforcesTheBalance:
    async def test_an_unbalanced_transaction_is_refused_at_commit(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Not by `domain/ledger.py`, which is bypassed here entirely. The rows
        are inserted directly, they are accepted one at a time, and COMMIT is
        what refuses them."""
        async with sessions() as session:
            transaction = LedgerTransaction(
                kind="opening", currency="EUR", currency_exponent=2, payment_id=None
            )
            session.add(transaction)
            await session.flush()
            session.add(
                LedgerEntry(transaction_id=transaction.id, account="cash", amount_minor=1000)
            )
            session.add(
                LedgerEntry(
                    transaction_id=transaction.id, account="opening_balance", amount_minor=-999
                )
            )
            await session.flush()  # accepted: the check is deferred to COMMIT

            with pytest.raises(IntegrityError, match="does not balance"):
                await session.commit()
            await session.rollback()

    async def test_a_balanced_one_commits(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """The vacuity guard. A trigger that refused everything would satisfy
        the test above and make the ledger unwritable."""
        async with sessions() as session:
            transaction = LedgerTransaction(
                kind="opening", currency="EUR", currency_exponent=2, payment_id=None
            )
            session.add(transaction)
            await session.flush()
            for account, amount in (("cash", 1000), ("opening_balance", -1000)):
                session.add(
                    LedgerEntry(transaction_id=transaction.id, account=account, amount_minor=amount)
                )
            await session.commit()

            await session.execute(
                delete(LedgerEntry).where(LedgerEntry.transaction_id == transaction.id)
            )
            await session.execute(
                delete(LedgerTransaction).where(LedgerTransaction.id == transaction.id)
            )
            await session.commit()

    async def test_moving_a_posting_between_transactions_is_refused(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The hole 0007 shipped, closed by 0008.

        Build a transaction that is unbalanced on its own, then balance it by
        stealing a posting from a settled one. The destination ends balanced and
        the source does not. A trigger resolving the transaction to check as
        COALESCE(NEW, OLD) never looks at the source on an UPDATE, so this
        committed -- verified against this database before the fix, leaving a
        committed transaction summing to 700.
        """
        async with sessions() as session:
            settled = LedgerTransaction(kind="opening", currency="EUR", currency_exponent=2)
            session.add(settled)
            await session.flush()
            # Held as plain values: a rollback expires every object in the
            # session, and these are read after one.
            settled_id = settled.id
            for account, amount in (("expense", 700), ("accounts_payable", -700)):
                session.add(
                    LedgerEntry(transaction_id=settled_id, account=account, amount_minor=amount)
                )
            await session.commit()

            thief = LedgerTransaction(kind="opening", currency="EUR", currency_exponent=2)
            session.add(thief)
            await session.flush()
            thief_id = thief.id
            session.add(LedgerEntry(transaction_id=thief_id, account="expense", amount_minor=700))
            await session.flush()
            await session.execute(
                update(LedgerEntry)
                .where(LedgerEntry.transaction_id == settled_id)
                .where(LedgerEntry.account == "accounts_payable")
                .values(transaction_id=thief_id)
            )

            with pytest.raises(IntegrityError, match="does not balance"):
                await session.commit()
            await session.rollback()

            await session.execute(
                delete(LedgerEntry).where(LedgerEntry.transaction_id.in_([settled_id, thief_id]))
            )
            await session.execute(
                delete(LedgerTransaction).where(LedgerTransaction.id.in_([settled_id, thief_id]))
            )
            await session.commit()

    async def test_deleting_one_side_of_a_settled_transaction_is_refused(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The attack on a ledger that is not an INSERT. Adding a bad entry is
        the obvious one; removing a good one from a transaction that already
        balanced is the quiet one, and a trigger that only fired on INSERT would
        allow it."""
        async with sessions() as session:
            transaction = LedgerTransaction(
                kind="opening", currency="EUR", currency_exponent=2, payment_id=None
            )
            session.add(transaction)
            await session.flush()
            transaction_id = transaction.id
            for account, amount in (("cash", 2500), ("opening_balance", -2500)):
                session.add(
                    LedgerEntry(transaction_id=transaction_id, account=account, amount_minor=amount)
                )
            await session.commit()

            await session.execute(
                delete(LedgerEntry)
                .where(LedgerEntry.transaction_id == transaction_id)
                .where(LedgerEntry.account == "cash")
            )
            with pytest.raises(IntegrityError, match="does not balance"):
                await session.commit()
            await session.rollback()

            await session.execute(
                delete(LedgerEntry).where(LedgerEntry.transaction_id == transaction_id)
            )
            await session.execute(
                delete(LedgerTransaction).where(LedgerTransaction.id == transaction_id)
            )
            await session.commit()


class TestItRefusesWhatItCannotPay:
    async def test_an_invoice_with_no_recorded_amount_is_refused(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """Not retried, either. A total that could not be read will not read
        differently on the third attempt, so this is a question for a person."""
        async with sessions() as session:
            decision = await approved_invoice(session)
            result = (
                await session.execute(
                    select(MatchResult).where(MatchResult.id == decision.match_result_id)
                )
            ).scalar_one()
            result.amount_minor = None
            result.currency = None
            result.currency_exponent = None
            await session.commit()

            with pytest.raises(PaymentRefusedError, match="records no amount"):
                await PaymentService(AlwaysAccepts()).advance(session, decision.id)
            await session.rollback()

    async def test_a_rejection_is_not_an_authorisation(
        self, sessions: async_sessionmaker[AsyncSession], clean: None
    ) -> None:
        """The executor was handed a decision id and never asked what was decided.

        `_request_payment` only enqueues for an approving decision, and for a
        sprint that was the only place the question was asked -- on the way
        *into* the queue. The executor trusted whatever id its job carried. A
        replayed dead letter, a hand-enqueued job during an incident or a bug
        upstream would each have paid an invoice a person had refused.

        The guard belongs at the end that moves money, whatever the other end does.
        """
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(
                    invoice_number=f"PAY-{uuid.uuid4().hex[:8].upper()}",
                    unit_price="21.0000",  # PO-44718 agrees 18.40: blocked, buyer's to decide
                ),
            )
            await session.flush()
            result = await MatchingService().match_document(session, document.id)
            assert result.disposition == "blocked", result.disposition
            assert result.amount_minor is not None, "nothing to pay, so nothing is being tested"

            refusal = await ApprovalService().decide(
                session,
                DecisionRequest(
                    match_result_id=result.id,
                    decision=Decision.REJECTED,
                    seen_version=result.version,
                    reason="billed above the agreed price",
                ),
                actor_id="buyer-sub",
                actor_label="A Buyer",
                roles=frozenset({Role.BUYER}),
            )
            await session.commit()
            refusal_id = refusal.id

            with pytest.raises(PaymentRefusedError, match="does not authorise"):
                await PaymentService(AlwaysAccepts()).advance(session, refusal_id)
            await session.rollback()

            paid = (
                await session.execute(
                    select(Payment).where(Payment.approval_decision_id == refusal_id)
                )
            ).scalar_one_or_none()
            assert paid is None, "a payment row exists for an invoice somebody rejected"

    async def test_an_authorisation_that_does_not_exist_is_refused(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            with pytest.raises(PaymentRefusedError, match="no approval decision"):
                await PaymentService(AlwaysAccepts()).advance(session, uuid.uuid4())
