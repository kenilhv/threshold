"""The payment executor: one state transition per job run, and why that is the design.

A payment has to be committed as `submitting` *before* the rail is called, or a
process that dies mid-call leaves a row that cannot say whether money moved. A
handler runs inside one transaction that the runtime commits at the end, so
there is no honest way to do both in one run.

So `advance` performs exactly one transition and returns. The handler re-enqueues
and the next run picks up where it left off. Four runs carry a payment from
authorised to settled:

1. nothing -> `PENDING`, and the accrual is posted: we owe it.
2. `PENDING` -> `SUBMITTING`, and the submission is posted. **This commit is the
   whole point**: after it, the row says money may have left.
3. `SUBMITTING` -> `SUBMITTED` or `FAILED`, by asking the rail. This is the only
   run that touches the network, and it starts from a state that was durable
   before it began.
4. `SUBMITTED` -> `SETTLED`, and the cash is posted out.

That is more round trips than a single function would need, and it buys the one
property that matters: at every instant, the durable state is a truthful account
of what may have happened. The queue is the thing making it durable, which is
what the queue has been for since M3 -- this is not new machinery, it is the
existing machinery used for the case it was built for.

## Retrying is safe because the key does not change

`SUBMITTING -> SUBMITTING` is a legal transition. A rail that did not answer
leaves the payment exactly where it was and the next run asks again with the
same idempotency key, which is derived from the approval decision and therefore
identical across every attempt. A rail that already took the money returns its
original answer.

Nothing is posted on that retry. `POSTS` has no entry for it, which is how the
ledger avoids doubling on every timeout.

## The accrual is posted here rather than in the approval service

Strictly it belongs at approval: the obligation exists from the moment somebody
accepts it, whether or not a payment is ever attempted. In this system the
payment is created by the approval, so the two moments coincide, and putting the
posting here keeps every ledger write in one module. If approvals ever stop
triggering payments, this moves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.rail.port import (
    PaymentInstruction,
    PaymentRail,
    RailOutcome,
    RailUnavailableError,
)
from threshold.adapters.rail.sandbox import SandboxRail
from threshold.domain import ledger
from threshold.domain.approval import PAYING, Decision
from threshold.domain.ledger import Account, Transaction, TransactionKind
from threshold.domain.models import (
    ApprovalDecision,
    LedgerEntry,
    LedgerTransaction,
    MatchResult,
    Payment,
    VendorBankAccount,
)
from threshold.domain.payments import (
    PaymentError,
    PaymentRequest,
    PaymentState,
    posting_for,
    transition,
)
from threshold.services.audit import Actor, record

log = structlog.get_logger(__name__)

#: The index that refuses a second live payment for one document (migration
#: 0014). Named here because the refusal is recognised by it: an IntegrityError
#: from any other constraint is a bug, and reporting it as "already being paid"
#: would hide it behind a reasonable-sounding sentence.
LIVE_PER_DOCUMENT: Final = "uq_payment_live_per_document"


def _violates(exc: BaseException, constraint: str) -> bool:
    """Whether Postgres refused this for violating `constraint`, however wrapped.

    SQLAlchemy raises `IntegrityError`, whose `orig` is the driver's adapter,
    whose cause is asyncpg's `UniqueViolationError` -- and that last one is what
    carries `constraint_name`. Walked the same way `_refused_by_privilege` walks
    for a SQLSTATE in `workers/runtime.py`, and for the same reason: which layer
    holds the attribute is a detail of the driver, not a contract.

    By attribute rather than by searching the message, because the message is
    prose and a constraint whose name contains this one's would match it.
    """
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if getattr(link, "constraint_name", None) == constraint:
            return True
        link = getattr(link, "orig", None) or link.__cause__ or link.__context__
    return False


class PaymentRefusedError(PaymentError):
    """This invoice cannot be paid, and not because of a transient failure.

    Separate from `PaymentError` so a handler can send it to the dead-letter
    queue rather than retrying. Almost every reason here is a missing fact -- no
    amount was read, no payment details are on file -- and re-reading the same
    rows produces the same absence. The exception is a fact that is present: the
    document already has a live payment. That does not heal on retry either, and
    a second authorisation for an invoice already being paid is a question for a
    person rather than for the rail.
    """


class PaymentService:
    def __init__(self, rail: PaymentRail | None = None) -> None:
        self._rail = rail or SandboxRail()

    async def advance(
        self, session: AsyncSession, approval_decision_id: uuid.UUID
    ) -> tuple[Payment, bool]:
        """Move this payment one step. Returns the payment and whether more remains.

        One step per call, deliberately. See the module docstring.
        """
        payment = (
            await session.execute(
                select(Payment).where(Payment.approval_decision_id == approval_decision_id)
            )
        ).scalar_one_or_none()

        if payment is None:
            payment = await self._create(session, approval_decision_id)
            return payment, True

        state = PaymentState(payment.state)
        if state is PaymentState.PENDING:
            await self._move(session, payment, PaymentState.SUBMITTING)
            return payment, True

        if state is PaymentState.SUBMITTING:
            return payment, await self._ask_the_rail(session, payment)

        if state is PaymentState.SUBMITTED:
            await self._move(session, payment, PaymentState.SETTLED)
            return payment, False

        # Terminal. Reaching here means a job outlived its payment, which is
        # normal after a replay and is not worth an error.
        log.info("payment.already_finished", payment_id=str(payment.id), state=payment.state)
        return payment, False

    async def _create(self, session: AsyncSession, approval_decision_id: uuid.UUID) -> Payment:
        """Turn an authorisation into a payable row, or refuse with a reason.

        Every refusal here is a fact rather than a policy judgment, and each is
        stated separately so the dead-letter queue says which one. All but the
        last are missing facts. The last is the database saying this document is
        already being paid, which is the backstop for a matching job that ran
        twice and minted a second authorisation (D-037).
        """
        decision = (
            await session.execute(
                select(ApprovalDecision).where(ApprovalDecision.id == approval_decision_id)
            )
        ).scalar_one_or_none()
        if decision is None:
            raise PaymentRefusedError(f"no approval decision {approval_decision_id}")
        # Asked here as well as where the job is enqueued, because this is the
        # end that moves money and a job is only a message. Until M9 it was
        # asked nowhere on this side: the executor paid whatever decision its
        # payload named, including a rejection.
        if Decision(decision.decision) not in PAYING:
            raise PaymentRefusedError(
                f"approval decision {approval_decision_id} is '{decision.decision}', which "
                "does not authorise a payment"
            )

        result = (
            await session.execute(
                select(MatchResult).where(MatchResult.id == decision.match_result_id)
            )
        ).scalar_one_or_none()
        if result is None:
            raise PaymentRefusedError(f"approval {approval_decision_id} cites no match result")
        if result.amount_minor is None or result.currency is None:
            raise PaymentRefusedError(
                f"match result {result.id} records no amount; the invoice total was not read "
                "well enough to pay against, and guessing it is not an option"
            )
        if result.purchase_order_id is None:
            raise PaymentRefusedError(
                f"match result {result.id} resolved no purchase order, so there is no vendor "
                "to pay -- that is an exception for a person, not a payment"
            )

        account = await self._payee(session, result)
        vendor_id = account.vendor_id

        request = PaymentRequest(
            approval_decision_id=approval_decision_id,
            match_result_id=result.id,
            document_id=result.document_id,
            vendor_id=vendor_id,
            vendor_bank_account_id=account.id,
            amount_minor=result.amount_minor,
            currency=result.currency,
            currency_exponent=result.currency_exponent or ledger.exponent_for(result.currency),
        )

        payment = Payment(
            approval_decision_id=request.approval_decision_id,
            match_result_id=request.match_result_id,
            document_id=request.document_id,
            vendor_id=request.vendor_id,
            vendor_bank_account_id=request.vendor_bank_account_id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            currency_exponent=request.currency_exponent,
            state=PaymentState.PENDING.value,
            idempotency_key=request.idempotency_key(),
            rail_id=self._rail.rail_id,
        )
        # In a savepoint, so that a refusal by `uq_payment_live_per_document`
        # leaves the transaction usable: the refusal names the payment that is
        # already live, and finding it takes a query.
        #
        # The accrual comes after this on purpose. A refused insert posts
        # nothing, so there is no second liability for one invoice to unwind.
        try:
            async with session.begin_nested():
                session.add(payment)
                await session.flush()
        except IntegrityError as exc:
            if not _violates(exc, LIVE_PER_DOCUMENT):
                raise
            raise PaymentRefusedError(
                await self._already_paying(session, request.document_id, approval_decision_id)
            ) from exc

        await self._post(
            session,
            ledger.accrue(amount_minor=request.amount_minor, currency=request.currency),
            match_result_id=result.id,
        )
        await record(
            session,
            actor=Actor.system(),
            action="payment.created",
            entity_type="payment",
            entity_id=payment.id,
            after={
                "amount_minor": request.amount_minor,
                "currency": request.currency,
                "state": PaymentState.PENDING.value,
            },
        )
        return payment

    async def _already_paying(
        self, session: AsyncSession, document_id: uuid.UUID, approval_decision_id: uuid.UUID
    ) -> str:
        """The refusal for a document that already has a live payment, naming it.

        Named, because the dead letter this becomes is read by a person who has
        to decide which authorisation was the real one, and "a payment exists"
        sends them looking for it. Read after the savepoint rolled back, so the
        row found is the committed one the index compared against.
        """
        existing = (
            (
                await session.execute(
                    select(Payment)
                    .where(Payment.document_id == document_id)
                    .where(Payment.state != PaymentState.FAILED.value)
                    .order_by(Payment.created_at)
                )
            )
            .scalars()
            .first()
        )
        which = (
            f"payment {existing.id} ({existing.state}, {existing.amount_minor} "
            f"{existing.currency}, authorised by approval decision "
            f"{existing.approval_decision_id})"
            if existing is not None
            else "a payment this transaction cannot see"
        )
        return (
            f"document {document_id} is already being paid by {which}; approval decision "
            f"{approval_decision_id} would pay the same invoice a second time. A second "
            "authorisation for one document is a question for a person, not for the rail"
        )

    async def _payee(self, session: AsyncSession, result: MatchResult) -> VendorBankAccount:
        """The account currently on file for this invoice's vendor.

        `effective_to IS NULL` is what "current" means here, and there is exactly
        one such row per vendor by construction. A vendor with none is a refusal:
        an invoice from somebody we have no payment details for is a question for
        treasury, and inventing a destination is the failure mode this whole
        system exists to prevent.
        """
        from threshold.domain.models import PurchaseOrder

        account = (
            (
                await session.execute(
                    select(VendorBankAccount)
                    .join(PurchaseOrder, PurchaseOrder.vendor_id == VendorBankAccount.vendor_id)
                    .where(PurchaseOrder.id == result.purchase_order_id)
                    .where(VendorBankAccount.effective_to.is_(None))
                )
            )
            .scalars()
            .first()
        )
        if account is None:
            raise PaymentRefusedError(
                f"no current payment details on file for the vendor on match result {result.id}"
            )
        return account

    async def _ask_the_rail(self, session: AsyncSession, payment: Payment) -> bool:
        """The only network call in this module, made from a durable state.

        A rail that does not answer leaves the payment in `SUBMITTING` and
        returns "more to do". That is not a swallowed error: the row already says
        money may have moved, and the next run asks again with the same key.
        """
        payment.rail_attempts += 1
        instruction = PaymentInstruction(
            idempotency_key=payment.idempotency_key,
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            payee_account_id=payment.vendor_bank_account_id,
            reference=str(payment.document_id),
        )

        try:
            answer = await self._rail.submit(instruction)
        except RailUnavailableError as outage:
            # Deliberately not a failure. A rail that timed out may have taken
            # the money, and writing `failed` here is how a supplier gets paid
            # twice on the retry.
            log.warning(
                "payment.rail_unavailable",
                payment_id=str(payment.id),
                attempts=payment.rail_attempts,
                error=str(outage),
            )
            return True

        payment.rail_reference = answer.rail_reference
        if answer.outcome is RailOutcome.ACCEPTED:
            await self._move(session, payment, PaymentState.SUBMITTED)
            return True

        payment.failure_reason = answer.reason
        await self._move(session, payment, PaymentState.FAILED)
        return False

    async def _move(self, session: AsyncSession, payment: Payment, target: PaymentState) -> None:
        """Transition, post whatever that transition posts, and say so once."""
        current = PaymentState(payment.state)
        payment.state = transition(current, target).value
        # Stamped here, before anything flushes, so the timestamp travels in the
        # same UPDATE as the state. They used to be set by the caller afterwards,
        # which made a second UPDATE against a row that was already `settled` --
        # and since 0011 Postgres refuses to touch a settled payment at all.
        if target is PaymentState.SUBMITTING:
            payment.submitted_at = datetime.now(UTC)
        elif target is PaymentState.SETTLED:
            payment.settled_at = datetime.now(UTC)

        kind = posting_for(current, target)
        if kind is not None:
            await self._post(session, _movement(kind, payment), payment_id=payment.id)

        await record(
            session,
            actor=Actor.system(),
            action=f"payment.{target.value}",
            entity_type="payment",
            entity_id=payment.id,
            before={"state": current.value},
            after={"state": target.value, "rail_reference": payment.rail_reference},
        )
        log.info(
            "payment.moved",
            payment_id=str(payment.id),
            **{"from": current.value, "to": target.value},
        )

    async def _post(
        self,
        session: AsyncSession,
        movement: Transaction,
        *,
        payment_id: uuid.UUID | None = None,
        match_result_id: uuid.UUID | None = None,
    ) -> LedgerTransaction:
        """Write a balanced movement. The database checks the balance again at COMMIT.

        `domain.ledger.Transaction` has already refused anything unbalanced, so
        this cannot normally write a bad set. It is written anyway as though it
        could, because `ck_ledger_entry_balanced` is what protects the table from
        the code that does not go through here.
        """
        row = LedgerTransaction(
            kind=movement.kind.value,
            currency=movement.currency,
            currency_exponent=ledger.exponent_for(movement.currency),
            payment_id=payment_id,
            match_result_id=match_result_id,
        )
        session.add(row)
        await session.flush()
        for posting in movement.postings:
            session.add(
                LedgerEntry(
                    transaction_id=row.id,
                    account=posting.account.value,
                    amount_minor=posting.amount_minor,
                )
            )
        await session.flush()
        return row

    async def in_flight(self, session: AsyncSession) -> tuple[dict[str, int], float | None]:
        """Payments by state, and how long the oldest unanswered one has waited.

        Read on the worker's event loop and cached, because the observable
        gauges that publish it run their callbacks on the exporter's thread
        where there is no loop to await a database on. The same arrangement the
        queue depth uses, and for the same reason.

        The age is measured from `submitted_at`, which is stamped when the row
        enters `SUBMITTING` -- so it is the age of the *uncertainty*, not the age
        of the payment. A payment authorised last week and submitted a minute
        ago has waited a minute.
        """
        counts = {
            str(state): int(total)
            for state, total in (
                await session.execute(select(Payment.state, func.count()).group_by(Payment.state))
            ).all()
        }
        oldest = (
            await session.execute(
                select(func.min(Payment.submitted_at)).where(
                    Payment.state == PaymentState.SUBMITTING.value
                )
            )
        ).scalar_one_or_none()
        age = None if oldest is None else (datetime.now(UTC) - oldest).total_seconds()
        return counts, age

    async def trial_balance(self, session: AsyncSession) -> dict[Account, int]:
        """Every account's balance, summed in the database.

        The oldest check in accounting: the totals must themselves sum to zero.
        Computed with SQL rather than by loading the ledger, because a trial
        balance that cannot be run on a full-size ledger is a trial balance
        nobody runs.
        """
        rows = (
            await session.execute(
                select(LedgerEntry.account, func.sum(LedgerEntry.amount_minor)).group_by(
                    LedgerEntry.account
                )
            )
        ).all()
        return {Account(account): int(total) for account, total in rows}


def _movement(kind: TransactionKind, payment: Payment) -> Transaction:
    """The transaction a payment's transition writes, at that payment's amount."""
    build = {
        TransactionKind.PAYMENT_SUBMITTED: ledger.submit,
        TransactionKind.PAYMENT_FAILED: ledger.fail,
        TransactionKind.PAYMENT_SETTLED: ledger.settle,
    }[kind]
    return build(amount_minor=payment.amount_minor, currency=payment.currency)
