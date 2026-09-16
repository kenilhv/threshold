"""The payment state machine, and the one state that makes it worth having.

Four of these states are bookkeeping. The fifth, `SUBMITTING`, is the reason
this is a state machine rather than a boolean.

## The state that exists because networks do

A payment executor calls a rail. The call can succeed, it can be refused, and it
can fail to answer. The third is not a variant of the second: a rail that did not
answer may still have taken the money, and a system that writes `failed` on a
timeout is a system that will pay the same invoice twice the moment it retries.

So the row is moved to `SUBMITTING` **and committed** before the rail is called.
If the process dies at any point after that, the row says "we may have sent
this", and recovery resubmits with the same idempotency key rather than deciding
what probably happened. That is the whole design, and everything else here is in
service of it.

The ledger posting is made at the same moment, for the same reason. Posting
after the rail answers would mean a payment that left the building with no entry
against it; posting before means a rejection has something to reverse, which is
what `ledger.fail` is for.

## Transitions are a table, not a set of `if`s

`ALLOWED` is the whole machine. A transition not in it is refused by
`transition`, and `test_payments.py` walks the table asserting every state is
reachable and every terminal state is terminal. A state machine whose legal
moves are scattered through a service is a state machine nobody can read, and
the illegal transition that matters -- `SETTLED` back to `SUBMITTING` -- is
exactly the one an ad-hoc `if` forgets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from threshold.domain.ledger import Account, TransactionKind


class PaymentError(ValueError):
    """A payment asked to do something a payment cannot do."""


class PaymentState(StrEnum):
    #: Authorised and not yet attempted. Nothing has been posted.
    PENDING = "pending"
    #: The rail has been called, or is about to be, and the outcome is unknown.
    #: The ledger already carries the submission. Committed before the call.
    SUBMITTING = "submitting"
    #: The rail accepted it. The money is in transit and not yet confirmed.
    SUBMITTED = "submitted"
    #: Confirmed by the rail. Terminal.
    SETTLED = "settled"
    #: The rail refused it, and the liability has been posted back. Terminal.
    FAILED = "failed"


#: Terminal states, which is to say: states no transition leaves.
TERMINAL: Final[frozenset[PaymentState]] = frozenset({PaymentState.SETTLED, PaymentState.FAILED})

#: The entire machine.
#:
#: `SUBMITTING -> SUBMITTING` is present and is not a typo. A rail that did not
#: answer leaves the payment exactly where it was, and the retry is a real event
#: worth being legal rather than an exception worth catching.
ALLOWED: Final[dict[PaymentState, frozenset[PaymentState]]] = {
    PaymentState.PENDING: frozenset({PaymentState.SUBMITTING}),
    PaymentState.SUBMITTING: frozenset(
        {PaymentState.SUBMITTING, PaymentState.SUBMITTED, PaymentState.FAILED}
    ),
    PaymentState.SUBMITTED: frozenset({PaymentState.SETTLED}),
    PaymentState.SETTLED: frozenset(),
    PaymentState.FAILED: frozenset(),
}

#: What each transition writes to the ledger, if anything.
#:
#: A table rather than a branch in the service, so that "which movements exist"
#: is answerable by reading one dictionary. `SUBMITTING -> SUBMITTED` posts
#: nothing: the money was already accounted for on the way into `SUBMITTING`,
#: and the rail merely confirmed what we had already assumed.
POSTS: Final[dict[tuple[PaymentState, PaymentState], TransactionKind]] = {
    (PaymentState.PENDING, PaymentState.SUBMITTING): TransactionKind.PAYMENT_SUBMITTED,
    (PaymentState.SUBMITTING, PaymentState.FAILED): TransactionKind.PAYMENT_FAILED,
    (PaymentState.SUBMITTED, PaymentState.SETTLED): TransactionKind.PAYMENT_SETTLED,
}


def transition(current: PaymentState, target: PaymentState) -> PaymentState:
    """Move, or refuse and say what was legal.

    Refuses rather than coercing, and the case that matters is a settled payment
    being asked to submit again. There is no repair for that -- the money has
    gone -- so the only useful behaviour is to stop loudly.
    """
    allowed = ALLOWED[current]
    if target not in allowed:
        legal = sorted(state.value for state in allowed)
        raise PaymentError(
            f"a {current.value} payment cannot become {target.value}; legal from here: "
            + (", ".join(legal) if legal else "nothing, it is terminal")
        )
    return target


def posting_for(current: PaymentState, target: PaymentState) -> TransactionKind | None:
    """Which ledger movement this transition makes, if any."""
    return POSTS.get((current, target))


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """What has to be true before a payment row may exist at all.

    Built from an approval decision, and validated here so that the reasons a
    payment is refused are one list in one place rather than a series of guards
    discovered while reading a service.
    """

    approval_decision_id: uuid.UUID
    match_result_id: uuid.UUID
    document_id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_bank_account_id: uuid.UUID
    amount_minor: int
    currency: str
    currency_exponent: int

    def __post_init__(self) -> None:
        if self.amount_minor <= 0:
            raise PaymentError(
                f"amount is {self.amount_minor}: a payment of nothing is not a payment, "
                "and a negative one is a refund, which this system does not do"
            )
        if self.currency_exponent < 0:
            raise PaymentError(f"currency exponent {self.currency_exponent} is negative")

    def idempotency_key(self) -> str:
        """Stable across every attempt at this payment, and unique to it.

        Derived from the authorising decision rather than generated, because a
        key generated per attempt is the same as having no key at all -- the
        rail would see each retry as a new instruction and pay again.

        The approval decision is the right root: it is append-only, it is what
        authorised the money, and `uq_payment_authorisation` already makes one
        payment per decision a database fact. So the key and the uniqueness
        constraint say the same thing, which is what makes them able to catch
        each other's failures.
        """
        return f"thr-pay-{self.approval_decision_id}"


#: Accounts a payment ever touches. Asserted against `POSTS` in the tests, so an
#: account added to the chart without a movement that reaches it is visible.
PAYMENT_ACCOUNTS: Final[frozenset[Account]] = frozenset(
    {Account.ACCOUNTS_PAYABLE, Account.CASH_IN_TRANSIT, Account.CASH}
)
