"""Postgres knows what a payment is, and refuses rows that disagree.

Migration 0011. Every test here talks to the table directly, because the
property is that the rules hold for a writer that never imported the service:
a hand-written UPDATE during an incident, a bug in a future code path, or an
application that has been taken over.

Three groups.

**The state machine, against the domain's own table.** `ALLOWED` in
`domain/payments.py` and the `IN (...)` list in `payment_moves_legally()` are
the same fact written twice, in two languages, and nothing but a test can keep
them the same. So every one of the twenty-five (from, to) pairs is attempted and
the database's answer is compared with the dictionary's.

**What a payment is for.** Each identity column, changed by the *owner*, is
refused -- so the immutability is a property of the table and not only of the
application's grants.

**What a payment may be created as.** Each way a row can disagree with the
authorisation it cites: a decision that does not approve, the wrong amount, a
different vendor's account, an account that was superseded.

`test_least_privilege.py` covers the other lock -- that `threshold_app` is
refused by privilege before any of these triggers is reached.

Requires: make up, make test-db
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.databases import ISOLATED
from tests.integration.test_matching import _document_with_extraction, _invoice_fields
from tests.integration.test_payments import (
    AlwaysAccepts,
    AlwaysRejects,
    NeverAnswers,
    approved_invoice,
    cleanup,
)

from threshold.domain.approval import PAYING, Decision
from threshold.domain.models import ApprovalDecision, MatchResult, Payment
from threshold.domain.payments import ALLOWED, PaymentState
from threshold.domain.roles import Role
from threshold.services.approvals import ApprovalService, DecisionRequest
from threshold.services.matching import MatchingService
from threshold.services.payments import PaymentService

pytestmark = pytest.mark.integration

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
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await cleanup(session)
    await engine.dispose()


#: How to reach each state the way the worker does: which rail, how many steps.
ROUTE: dict[PaymentState, tuple[type, int]] = {
    PaymentState.PENDING: (AlwaysAccepts, 1),
    PaymentState.SUBMITTING: (NeverAnswers, 3),  # created, moved, asked once and not answered
    PaymentState.SUBMITTED: (AlwaysAccepts, 3),
    PaymentState.SETTLED: (AlwaysAccepts, 4),
    PaymentState.FAILED: (AlwaysRejects, 3),
}


async def payment_in(session: AsyncSession, state: PaymentState) -> uuid.UUID:
    """A real payment, driven to `state` by the real service, committed."""
    decision = await approved_invoice(session)
    await session.commit()
    rail, steps = ROUTE[state]
    service = PaymentService(rail())
    payment: Payment | None = None
    for _ in range(steps):
        payment, _more = await service.advance(session, decision.id)
        await session.commit()
    assert payment is not None
    assert payment.state == state.value, f"fixture reached {payment.state}, wanted {state.value}"
    return payment.id


async def _attempt(session: AsyncSession, statement: str, **params: Any) -> str | None:
    """Run inside a savepoint; return the refusal message, or None if it was accepted.

    The savepoint is rolled back *either way*. The first version released it on
    success, so an accepted move persisted and the next attempt started from the
    wrong state -- which reported `submitting -> settled` as permitted, against a
    trigger that was refusing it correctly.
    """
    savepoint = await session.begin_nested()
    try:
        await session.execute(text(statement), params)
    except (IntegrityError, DBAPIError) as refusal:
        await savepoint.rollback()
        return str(refusal.orig)
    await savepoint.rollback()
    return None


class TestTheStateMachineIsTheDomainsOwn:
    @pytest.mark.parametrize("origin", list(PaymentState), ids=lambda s: s.value)
    async def test_every_move_gets_the_answer_the_domain_gives(
        self, sessions: async_sessionmaker[AsyncSession], origin: PaymentState
    ) -> None:
        async with sessions() as session:
            payment_id = await payment_in(session, origin)

            disagreements: list[str] = []
            for target in PaymentState:
                # `failed` must say why (ck_payment_failure_is_explained), and a
                # row leaving `failed` must stop saying it. Supplied here so the
                # only thing left to refuse the move is the transition itself.
                reason = "account closed" if target is PaymentState.FAILED else None
                refusal = await _attempt(
                    session,
                    "update payment set state = :to, failure_reason = :why where id = :id",
                    to=target.value,
                    why=reason,
                    id=payment_id,
                )
                permitted = refusal is None
                expected = target in ALLOWED[origin]
                if permitted != expected:
                    disagreements.append(
                        f"{origin.value} -> {target.value}: domain says "
                        f"{'yes' if expected else 'no'}, Postgres says "
                        f"{'yes' if permitted else refusal}"
                    )
            await session.rollback()
        assert not disagreements, "\n".join(disagreements)

    def test_the_comparison_had_something_to_compare(self) -> None:
        """The vacuity guard: a table that allows nothing, or everything, would
        make the loop above agree with a trigger that does the same."""
        moves = {(a, b) for a, targets in ALLOWED.items() for b in targets}
        assert 0 < len(moves) < len(PaymentState) ** 2
        assert (PaymentState.SETTLED, PaymentState.PENDING) not in moves
        assert set(ROUTE) == set(PaymentState), "a state with no route is a state never tested"


class TestWhatAPaymentIsForCannotChange:
    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("amount_minor", "amount_minor * 5"),
            ("currency", "'USD'"),
            ("currency_exponent", "0"),
            (
                "vendor_bank_account_id",
                "(select a.id from vendor_bank_account a where a.vendor_id <> payment.vendor_id "
                "limit 1)",
            ),
            ("vendor_id", "(select v.id from vendor v where v.id <> payment.vendor_id limit 1)"),
            ("idempotency_key", "'thr-pay-somebody-else'"),
            ("rail_id", "'a-different-rail'"),
            ("created_at", "now() - interval '1 year'"),
        ],
    )
    async def test_not_even_by_the_owner(
        self, sessions: async_sessionmaker[AsyncSession], column: str, value: str
    ) -> None:
        """The redirect, as it was performed in the review that found it: a
        pending payment, after authorisation and before submission."""
        async with sessions() as session:
            payment_id = await payment_in(session, PaymentState.PENDING)
            refusal = await _attempt(
                session, f"update payment set {column} = {value} where id = :id", id=payment_id
            )
            await session.rollback()
        assert refusal is not None, f"{column} was rewritten on a pending payment"
        assert "fixed when it is created" in refusal, refusal

    async def test_the_count_of_attempts_does_not_go_down(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            payment_id = await payment_in(session, PaymentState.SUBMITTING)
            refusal = await _attempt(
                session, "update payment set rail_attempts = 0 where id = :id", id=payment_id
            )
            await session.rollback()
        assert refusal is not None and "cannot decrease" in refusal, refusal

    @pytest.mark.parametrize("finished", [PaymentState.SETTLED, PaymentState.FAILED])
    async def test_a_finished_payment_is_not_touched_at_all(
        self, sessions: async_sessionmaker[AsyncSession], finished: PaymentState
    ) -> None:
        """Including by an UPDATE that changes nothing about the state -- which
        is how the service's own late `settled_at` write was caught."""
        async with sessions() as session:
            payment_id = await payment_in(session, finished)
            refusal = await _attempt(
                session,
                "update payment set rail_reference = 'REWRITTEN' where id = :id",
                id=payment_id,
            )
            await session.rollback()
        assert refusal is not None and "is not reopened" in refusal, refusal


async def _decided(session: AsyncSession, decision: Decision) -> ApprovalDecision:
    """One decision of each kind, each taken by the real service."""
    if decision is Decision.AUTO_APPROVED:
        return await approved_invoice(session)

    document = await _document_with_extraction(
        session,
        _invoice_fields(
            invoice_number=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            unit_price="21.0000",  # PO-44718 agrees 18.40: blocked, and the buyer's to decide
        ),
    )
    await session.flush()
    result = await MatchingService().match_document(session, document.id)
    assert result.disposition == "blocked", result.disposition
    return await ApprovalService().decide(
        session,
        DecisionRequest(
            match_result_id=result.id,
            decision=decision,
            seen_version=result.version,
            reason="decided for a test of what may be paid",
        ),
        actor_id="buyer-sub",
        actor_label="A Buyer",
        roles=frozenset({Role.BUYER}),
    )


async def _honest_row(session: AsyncSession, decision: ApprovalDecision) -> dict[str, Any]:
    """The payment `PaymentService._create` would write for this decision."""
    result = (
        await session.execute(select(MatchResult).where(MatchResult.id == decision.match_result_id))
    ).scalar_one()
    payee = (
        await session.execute(
            text(
                "select a.id, a.vendor_id from vendor_bank_account a "
                "join purchase_order p on p.vendor_id = a.vendor_id "
                "where p.id = :po and a.effective_to is null"
            ),
            {"po": result.purchase_order_id},
        )
    ).one()
    return {
        "approval_decision_id": decision.id,
        "match_result_id": result.id,
        "document_id": result.document_id,
        "vendor_id": payee.vendor_id,
        "vendor_bank_account_id": payee.id,
        "amount_minor": result.amount_minor,
        "currency": result.currency,
        "currency_exponent": result.currency_exponent,
        "state": "pending",
        "idempotency_key": f"thr-pay-{decision.id}",
        "rail_id": "test-direct",
    }


INSERT = (
    "insert into payment (approval_decision_id, match_result_id, document_id, vendor_id, "
    "vendor_bank_account_id, amount_minor, currency, currency_exponent, state, "
    "idempotency_key, rail_id) values (:approval_decision_id, :match_result_id, :document_id, "
    ":vendor_id, :vendor_bank_account_id, :amount_minor, :currency, :currency_exponent, :state, "
    ":idempotency_key, :rail_id)"
)


class TestAPaymentAgreesWithItsAuthorisation:
    @pytest.mark.parametrize("decision", list(Decision), ids=lambda d: d.value)
    async def test_only_an_approving_decision_can_be_paid(
        self, sessions: async_sessionmaker[AsyncSession], decision: Decision
    ) -> None:
        """`PAYING` in the domain and `IN ('approved', 'auto_approved')` in the
        trigger, held together the same way as the state machine."""
        async with sessions() as session:
            decided = await _decided(session, decision)
            await session.commit()
            refusal = await _attempt(session, INSERT, **await _honest_row(session, decided))
            await session.rollback()

        if decision in PAYING:
            assert refusal is None, (
                f"an honest payment on '{decision.value}' was refused: {refusal}"
            )
        else:
            assert refusal is not None, f"a payment was created on a '{decision.value}' decision"
            assert "does not authorise a payment" in refusal, refusal

    @pytest.mark.parametrize(
        ("tamper", "expected"),
        [
            ({"amount_minor": 99_999_999}, "the authorised invoice was for"),
            ({"currency": "USD"}, "the authorised invoice was for"),
            ({"state": "settled"}, "created pending"),
            ({"state": "submitted"}, "created pending"),
        ],
        ids=["inflated", "other-currency", "born-settled", "born-submitted"],
    )
    async def test_a_row_that_disagrees_is_refused(
        self,
        sessions: async_sessionmaker[AsyncSession],
        tamper: dict[str, Any],
        expected: str,
    ) -> None:
        async with sessions() as session:
            decided = await approved_invoice(session)
            await session.commit()
            row = {**await _honest_row(session, decided), **tamper}
            refusal = await _attempt(session, INSERT, **row)
            await session.rollback()
        assert refusal is not None and expected in refusal, refusal

    async def test_another_vendors_account_is_refused(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The redirect, at insert time. There is a foreign key on this column
        and it is satisfied: the account exists. It is somebody else's."""
        async with sessions() as session:
            decided = await approved_invoice(session)
            await session.commit()
            row = await _honest_row(session, decided)
            row["vendor_bank_account_id"] = (
                await session.execute(
                    text(
                        "select id from vendor_bank_account "
                        "where vendor_id <> :v and effective_to is null limit 1"
                    ),
                    {"v": row["vendor_id"]},
                )
            ).scalar_one()
            refusal = await _attempt(session, INSERT, **row)
            await session.rollback()
        assert refusal is not None and "does not belong to vendor" in refusal, refusal

    async def test_a_superseded_account_is_refused(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The right vendor, and the details they used to have. Paying the old
        account after a verified change is its own failure, and paying it after
        an *attacker's* change was reverted is the more likely one."""
        async with sessions() as session:
            decided = await approved_invoice(session)
            await session.commit()
            row = await _honest_row(session, decided)
            row["vendor_bank_account_id"] = (
                await session.execute(
                    text(
                        "select id from vendor_bank_account "
                        "where vendor_id = :v and effective_to is not null limit 1"
                    ),
                    {"v": row["vendor_id"]},
                )
            ).scalar_one()
            refusal = await _attempt(session, INSERT, **row)
            await session.rollback()
        assert refusal is not None and "was superseded" in refusal, refusal

    async def test_a_payment_for_a_different_document_is_refused(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            decided = await approved_invoice(session)
            other = await approved_invoice(session)
            await session.commit()
            row = await _honest_row(session, decided)
            row["document_id"] = (await _honest_row(session, other))["document_id"]
            refusal = await _attempt(session, INSERT, **row)
            await session.rollback()
        assert refusal is not None and "is about" in refusal, refusal
