"""Double-entry bookkeeping, as a closed set of balanced transactions.

Every movement of money is a set of postings that sums to zero. That is the
whole idea, it is five hundred years old, and the reason to use it here rather
than a `paid` boolean is that it makes a class of bug arithmetically impossible:
you cannot pay an invoice without recording where the money came from, because
the transaction will not balance and the database will refuse it.

## Signs, and why debit is positive

Postings carry a signed integer. Debit is positive, credit is negative, and a
transaction is balanced when the signs cancel. That is one convention among
several and the only thing that matters is that it is stated once and never
argued with: a codebase where half the code treats a credit as positive is a
codebase with a sign bug in it somewhere, waiting.

A consequence worth knowing when reading a balance: an account's *normal* side
determines whether a healthy balance is positive or negative. Assets and
expenses run debit-positive, liabilities and equity run credit-negative. So
`accounts_payable` sitting at -4_820_00 means we owe 4,820.00, which is correct
and looks alarming until you have read this paragraph. `trial_balance` reports
the sign as stored and `AccountKind` is what tells a reader how to hold it.

## Integers, not Decimal

Amounts are integer minor units -- cents, pence, sen. `Decimal` is the right
type for tolerance arithmetic, where the question is "how far apart are these
two numbers", and the wrong one for a ledger, where the question is "do these
numbers cancel exactly". A quantised `Decimal` cancels exactly too, right up
until somebody divides one.

The conversion happens at exactly one place, `to_minor`, and that is where the
rounding decision is made and recorded. Not every currency has two decimal
places: JPY has none and BHD has three, so the exponent is a property of the
currency rather than a constant, and a currency this system has never seen is a
refusal rather than a guess at two.

## The transaction kinds are closed

`TransactionKind` is an enum for the same reason `ExceptionCode` is: reports key
off these strings. A payment run that invented a kind would be a payment run
whose money did not appear in anybody's totals.

Nothing here touches a database, a clock or a model. `services/payments.py` is
what persists these, and `migrations/` carries a deferred constraint trigger so
that the balance rule is enforced by Postgres at COMMIT rather than by this
module being called correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final


class LedgerError(ValueError):
    """A transaction that is not a transaction.

    An error rather than a correction, always. A ledger that quietly balanced
    itself would be a ledger whose totals are its own opinion.
    """


class AccountKind(StrEnum):
    """Which side an account's healthy balance sits on.

    Carried so a reader -- or a report -- can tell a liability of 4,820 from an
    asset of -4,820 without knowing the sign convention by heart.
    """

    ASSET = "asset"
    LIABILITY = "liability"
    EXPENSE = "expense"
    EQUITY = "equity"


class Account(StrEnum):
    """The chart of accounts. Small on purpose.

    This is an accounts-payable system, so it needs exactly enough accounts to
    say: we owe somebody, we sent money, the money arrived. A general ledger
    with sub-accounts per cost centre is a different product.
    """

    #: What we owe suppliers. Credited when an invoice is approved, debited when
    #: it is paid.
    ACCOUNTS_PAYABLE = "accounts_payable"
    #: The bank account the money leaves.
    CASH = "cash"
    #: Submitted to the rail, not yet confirmed settled. The account that exists
    #: because "sent" and "arrived" are different facts, and the window between
    #: them is where a payment system either has an answer or does not.
    CASH_IN_TRANSIT = "cash_in_transit"
    #: Where an approved invoice lands on the other side of the accrual.
    EXPENSE = "expense"
    #: The opening position. Without it the first payment overdraws an account
    #: that was never funded, and every balance reads as a deficit.
    OPENING_BALANCE = "opening_balance"


KIND_OF: Final[dict[Account, AccountKind]] = {
    Account.ACCOUNTS_PAYABLE: AccountKind.LIABILITY,
    Account.CASH: AccountKind.ASSET,
    Account.CASH_IN_TRANSIT: AccountKind.ASSET,
    Account.EXPENSE: AccountKind.EXPENSE,
    Account.OPENING_BALANCE: AccountKind.EQUITY,
}


class TransactionKind(StrEnum):
    """Why money moved. Added, never repurposed -- reports key off these."""

    #: An approved invoice becomes a liability.
    ACCRUAL = "accrual"
    #: A payment leaves, and is not yet known to have arrived.
    PAYMENT_SUBMITTED = "payment_submitted"
    #: The rail confirmed it.
    PAYMENT_SETTLED = "payment_settled"
    #: The rail rejected it, and the liability comes back.
    PAYMENT_FAILED = "payment_failed"
    #: The opening position, written once by the seed.
    OPENING = "opening"


#: How many minor units make one major unit, by ISO 4217 exponent.
#:
#: Deliberately only the currencies this system has actually seen. A currency
#: nobody has tested is a rounding bug nobody has seen either, and defaulting to
#: two silently turns 1000 JPY into 10 JPY.
#:
#: There is a second exponent table in this repository, `_ZERO_DECIMAL` in
#: `seeds/layouts/multipage_credit.py`, and it is deliberately not reused. Its
#: own comment says it drives formatting only, and it is right to: it lists HUF,
#: which is printed without decimals and is formally a two-decimal currency under
#: ISO 4217. Copying it here would introduce a real rounding error on Hungarian
#: invoices in order to save five lines.
MINOR_UNITS: Final[dict[str, int]] = {
    "EUR": 2,
    "GBP": 2,
    "USD": 2,
    "CHF": 2,
}


def exponent_for(currency: str) -> int:
    """The exponent in force for this currency, or refuse.

    Exported because every persisted amount stores the exponent it was written
    with, rather than looking it up when read. That is the same argument
    `policy_version` on `match_result` and `owner_role` on `match_exception`
    already make: this table is a present-tense fact, and re-deriving a
    historical row from it silently restates what that row meant.

    It matters more here than in either of those. If a currency were ever
    corrected -- the ISO exponent changed, or an entry here was wrong -- reading
    old rows through the new table would move every decimal point in them.
    """
    exponent = MINOR_UNITS.get(currency.upper())
    if exponent is None:
        raise LedgerError(
            f"no minor-unit exponent for {currency!r}; add it to MINOR_UNITS with a test "
            "rather than letting it default to two"
        )
    return exponent


def to_minor(amount: Decimal, currency: str) -> int:
    """Convert a quantised decimal into integer minor units, or refuse.

    ``ROUND_HALF_UP`` rather than banker's rounding, and the reason is not
    statistical. Half-even distributes error better across many roundings, which
    is what you want for a statistic and not what you want for a payment: a
    supplier asked for 10.005 and was paid 10.00 will ask why, and "it rounds to
    even" is not an answer anybody accepts about their own invoice.

    In practice nothing should round at all -- the amounts arriving here come
    from a document that printed two decimals. Rounding here means an upstream
    quantisation was missed, so it is worth knowing which way it goes.
    """
    scaled = amount * (10 ** exponent_for(currency))
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_major(minor: int, currency: str) -> Decimal:
    """The inverse, for display. Exact: no division of a Decimal by a Decimal."""
    return Decimal(minor).scaleb(-exponent_for(currency))


@dataclass(frozen=True, slots=True)
class Posting:
    """One side of a movement. Debit positive, credit negative."""

    account: Account
    amount_minor: int

    def __post_init__(self) -> None:
        if self.amount_minor == 0:
            raise LedgerError(
                f"a zero posting to {self.account.value} moves nothing and balances "
                "nothing; it is a line in a report that says something happened"
            )


@dataclass(frozen=True, slots=True)
class Transaction:
    """A balanced set of postings. The only thing that may be written.

    Validates in ``__post_init__`` rather than in a factory, so there is no door
    into the type that skips the rule -- the same reasoning as
    `domain/triage.py`'s escalation packet, and for the same reason: a caller in
    a hurry reaches for the constructor.
    """

    kind: TransactionKind
    currency: str
    postings: tuple[Posting, ...]

    def __post_init__(self) -> None:
        if self.currency not in MINOR_UNITS:
            raise LedgerError(f"unknown currency {self.currency!r}")
        if len(self.postings) < 2:
            raise LedgerError(
                f"a transaction with {len(self.postings)} posting(s) is single-entry "
                "bookkeeping; money came from somewhere"
            )
        total = sum(posting.amount_minor for posting in self.postings)
        if total != 0:
            raise LedgerError(
                f"postings sum to {total}, not zero. A {self.kind.value} that does not "
                "balance is money appearing or disappearing"
            )
        accounts = [posting.account for posting in self.postings]
        if len(accounts) != len(set(accounts)):
            raise LedgerError(
                "an account appears twice in one transaction; net the postings first "
                "so the ledger reads as what happened rather than as how it was built"
            )

    @property
    def amount_minor(self) -> int:
        """The size of the movement: the total debited, which equals the total credited."""
        return sum(p.amount_minor for p in self.postings if p.amount_minor > 0)


def _movement(
    kind: TransactionKind, *, debit: Account, credit: Account, amount_minor: int, currency: str
) -> Transaction:
    if amount_minor <= 0:
        raise LedgerError(
            f"{kind.value} of {amount_minor}: an amount must be positive, and the "
            "direction is the accounts rather than the sign"
        )
    return Transaction(
        kind=kind,
        currency=currency,
        postings=(
            Posting(account=debit, amount_minor=amount_minor),
            Posting(account=credit, amount_minor=-amount_minor),
        ),
    )


def accrue(*, amount_minor: int, currency: str) -> Transaction:
    """An approved invoice becomes a liability.

    Written at approval rather than at payment, which is the whole point of
    accrual accounting: the obligation exists from the moment somebody accepts
    it, and a ledger that only records payments cannot answer "what do we owe".
    """
    return _movement(
        TransactionKind.ACCRUAL,
        debit=Account.EXPENSE,
        credit=Account.ACCOUNTS_PAYABLE,
        amount_minor=amount_minor,
        currency=currency,
    )


def submit(*, amount_minor: int, currency: str) -> Transaction:
    """Money leaves the liability and enters transit.

    Not `cash` yet. The gap between "we sent it" and "it arrived" is real, it is
    where every payment incident lives, and an account for it is how the ledger
    stays able to answer "how much is in flight right now".
    """
    return _movement(
        TransactionKind.PAYMENT_SUBMITTED,
        debit=Account.ACCOUNTS_PAYABLE,
        credit=Account.CASH_IN_TRANSIT,
        amount_minor=amount_minor,
        currency=currency,
    )


def settle(*, amount_minor: int, currency: str) -> Transaction:
    """The rail confirmed it. Transit becomes cash leaving."""
    return _movement(
        TransactionKind.PAYMENT_SETTLED,
        debit=Account.CASH_IN_TRANSIT,
        credit=Account.CASH,
        amount_minor=amount_minor,
        currency=currency,
    )


def fail(*, amount_minor: int, currency: str) -> Transaction:
    """The rail rejected it, and we owe the supplier again.

    A reversing transaction rather than a deletion or an update. The ledger is
    append-only, so a failed payment leaves three rows saying what happened
    instead of no rows saying it never did -- which is the difference between an
    audit trail and a current-state table.
    """
    return _movement(
        TransactionKind.PAYMENT_FAILED,
        debit=Account.CASH_IN_TRANSIT,
        credit=Account.ACCOUNTS_PAYABLE,
        amount_minor=amount_minor,
        currency=currency,
    )


def opening(*, amount_minor: int, currency: str) -> Transaction:
    """Fund the cash account once, so the first payment is not an overdraft."""
    return _movement(
        TransactionKind.OPENING,
        debit=Account.CASH,
        credit=Account.OPENING_BALANCE,
        amount_minor=amount_minor,
        currency=currency,
    )


def trial_balance(transactions: tuple[Transaction, ...]) -> dict[Account, int]:
    """Sum every posting by account. The totals must themselves sum to zero.

    The oldest check in accounting and still the useful one: it is a single
    number that goes non-zero the moment anything anywhere has gone wrong, which
    is why `services/payments.py` exposes it and the integration suite asserts it
    after every scenario rather than only at the end.
    """
    balances: dict[Account, int] = {}
    for transaction in transactions:
        for posting in transaction.postings:
            balances[posting.account] = balances.get(posting.account, 0) + posting.amount_minor
    return balances
