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

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.rail.port import (
    PaymentInstruction,
    PaymentRail,
    RailOutcome,
    RailUnavailableError,
)
from threshold.adapters.rail.sandbox import SandboxRail
from threshold.domain import ledger
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


class PaymentRefusedError(PaymentError):
    """This invoice cannot be paid, and not because of a transient failure.

    Separate from `PaymentError` so a handler can send it to the dead-letter
    queue rather than retrying. Every reason here is a missing fact -- no amount
    was read, no payment details are on file -- and re-reading the same rows
    produces the same absence.
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
            payment.submitted_at = datetime.now(UTC)
            return payment, True

        if state is PaymentState.SUBMITTING:
            return payment, await self._ask_the_rail(session, payment)

        if state is PaymentState.SUBMITTED:
            await self._move(session, payment, PaymentState.SETTLED)
            payment.settled_at = datetime.now(UTC)
            return payment, False

        # Terminal. Reaching here means a job outlived its payment, which is
        # normal after a replay and is not worth an error.
        log.info("payment.already_finished", payment_id=str(payment.id), state=payment.state)
        return payment, False

    async def _create(self, session: AsyncSession, approval_decision_id: uuid.UUID) -> Payment:
        """Turn an authorisation into a payable row, or refuse with a reason.

        Every refusal here is a missing fact rather than a policy judgment, and
        each is stated separately so the dead-letter queue says which one.
        """
        decision = (
            await session.execute(
                select(ApprovalDecision).where(ApprovalDecision.id == approval_decision_id)
            )
        ).scalar_one_or_none()
        if decision is None:
            raise PaymentRefusedError(f"no approval decision {approval_decision_id}")

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
        session.add(payment)
        await session.flush()

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
