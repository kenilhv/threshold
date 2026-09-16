"""The ledger, as a table.

Two kinds of claim here. The first is that a transaction which does not balance
cannot be constructed -- which is the whole of double-entry and is worth one
test per way of breaking it. The second is that the four movements compose into
the story an accounts-payable system actually tells: we owed it, we sent it, it
arrived, and here is what the accounts say at each point.

The database enforces the same balance rule independently, at COMMIT, through a
deferred constraint trigger. `tests/integration/test_ledger.py` is where that is
proved. This file is the half that needs no Postgres.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from threshold.domain.ledger import (
    KIND_OF,
    MINOR_UNITS,
    Account,
    AccountKind,
    LedgerError,
    Posting,
    Transaction,
    TransactionKind,
    accrue,
    fail,
    opening,
    settle,
    submit,
    to_major,
    to_minor,
    trial_balance,
)

#: 4,820.00 EUR, the sort of number an invoice in this system actually carries.
AMOUNT = 482_000
EUR = "EUR"


class TestATransactionMustBalance:
    def test_a_balanced_pair_is_accepted(self) -> None:
        transaction = Transaction(
            kind=TransactionKind.ACCRUAL,
            currency=EUR,
            postings=(
                Posting(account=Account.EXPENSE, amount_minor=AMOUNT),
                Posting(account=Account.ACCOUNTS_PAYABLE, amount_minor=-AMOUNT),
            ),
        )
        assert transaction.amount_minor == AMOUNT

    def test_a_balanced_set_of_more_than_two_is_accepted(self) -> None:
        """Nothing here needs a split posting yet, and the rule is about the sum
        rather than about the count -- a type that only allowed pairs would have
        to be rewritten the first time a payment covers two invoices."""
        transaction = Transaction(
            kind=TransactionKind.PAYMENT_SUBMITTED,
            currency=EUR,
            postings=(
                Posting(account=Account.ACCOUNTS_PAYABLE, amount_minor=300_00),
                Posting(account=Account.EXPENSE, amount_minor=200_00),
                Posting(account=Account.CASH_IN_TRANSIT, amount_minor=-500_00),
            ),
        )
        assert transaction.amount_minor == 500_00

    # -AMOUNT is deliberately absent: it would zero the posting, and the zero rule
    # fires first, so the case would pass while proving nothing about balance.
    @pytest.mark.parametrize("drift", [1, -1, 100, -241_000])
    def test_postings_that_do_not_cancel_are_refused(self, drift: int) -> None:
        """One cent out is the case that matters. A rule that only caught gross
        errors would miss every rounding bug it exists to catch."""
        with pytest.raises(LedgerError, match=r"does not.*balance|sum to"):
            Transaction(
                kind=TransactionKind.ACCRUAL,
                currency=EUR,
                postings=(
                    Posting(account=Account.EXPENSE, amount_minor=AMOUNT + drift),
                    Posting(account=Account.ACCOUNTS_PAYABLE, amount_minor=-AMOUNT),
                ),
            )

    def test_a_single_posting_is_refused(self) -> None:
        with pytest.raises(LedgerError, match="single-entry"):
            Transaction(
                kind=TransactionKind.ACCRUAL,
                currency=EUR,
                postings=(Posting(account=Account.EXPENSE, amount_minor=0 + AMOUNT),),
            )

    def test_no_postings_at_all_is_refused(self) -> None:
        """An empty tuple sums to zero, so the balance check passes it. Without
        the count check this would be a transaction recording nothing."""
        with pytest.raises(LedgerError, match="single-entry"):
            Transaction(kind=TransactionKind.ACCRUAL, currency=EUR, postings=())

    def test_a_zero_posting_is_refused(self) -> None:
        with pytest.raises(LedgerError, match="moves nothing"):
            Posting(account=Account.CASH, amount_minor=0)

    def test_the_same_account_twice_is_refused(self) -> None:
        """It balances, and it is how the transaction was built rather than what
        happened. Netting first keeps the ledger readable."""
        with pytest.raises(LedgerError, match="twice"):
            Transaction(
                kind=TransactionKind.ACCRUAL,
                currency=EUR,
                postings=(
                    Posting(account=Account.EXPENSE, amount_minor=AMOUNT),
                    Posting(account=Account.EXPENSE, amount_minor=-AMOUNT),
                ),
            )

    def test_an_unknown_currency_is_refused(self) -> None:
        with pytest.raises(LedgerError, match="unknown currency"):
            Transaction(
                kind=TransactionKind.ACCRUAL,
                currency="XXX",
                postings=(
                    Posting(account=Account.EXPENSE, amount_minor=AMOUNT),
                    Posting(account=Account.ACCOUNTS_PAYABLE, amount_minor=-AMOUNT),
                ),
            )

    def test_the_constructors_refuse_a_negative_amount(self) -> None:
        """The direction of a movement is which accounts it touches. A negative
        amount would express it twice and let the two disagree."""
        for build in (accrue, submit, settle, fail, opening):
            with pytest.raises(LedgerError, match="must be positive"):
                build(amount_minor=-1, currency=EUR)
            with pytest.raises(LedgerError, match="must be positive"):
                build(amount_minor=0, currency=EUR)


class TestMinorUnits:
    @pytest.mark.parametrize(
        ("major", "minor"),
        [
            ("0.01", 1),
            ("1.00", 100),
            ("4820.00", 482_000),
            ("10.99", 1099),
            ("999999.99", 99_999_999),
        ],
    )
    def test_conversion_is_exact(self, major: str, minor: int) -> None:
        assert to_minor(Decimal(major), EUR) == minor
        assert to_major(minor, EUR) == Decimal(major)

    def test_a_round_trip_returns_the_same_number(self) -> None:
        for minor in (1, 99, 100, 482_000, 99_999_999):
            assert to_minor(to_major(minor, EUR), EUR) == minor

    def test_a_half_cent_rounds_up_and_not_to_even(self) -> None:
        """Half-even distributes error better across many roundings, which is
        what a statistic wants. A supplier paid 10.00 on an invoice for 10.005
        asks why, and "it rounds to even" is not an answer about their money.

        Nothing upstream should reach here unquantised -- these amounts come off
        a page that printed two decimals -- so this is about which way a bug
        goes, not about which way business runs.
        """
        assert to_minor(Decimal("10.005"), EUR) == 1001
        assert to_minor(Decimal("10.015"), EUR) == 1002

    def test_a_currency_with_no_exponent_is_refused(self) -> None:
        """Defaulting to two would turn 1000 JPY into 10 JPY silently, and JPY
        is the reason this is a table rather than a constant."""
        with pytest.raises(LedgerError, match="no minor-unit exponent"):
            to_minor(Decimal("1000"), "JPY")
        with pytest.raises(LedgerError, match="no minor-unit exponent"):
            to_major(1000, "JPY")

    def test_the_table_is_not_empty(self) -> None:
        """A vacuity guard: with an empty table every test above passes by
        refusing everything."""
        assert MINOR_UNITS
        assert all(exponent >= 0 for exponent in MINOR_UNITS.values())


class TestTheAccountsAreComplete:
    def test_every_account_has_a_kind(self) -> None:
        """An account whose normal side is unknown is an account no report can
        present, because the sign alone does not say whether it is healthy."""
        missing = [account.value for account in Account if account not in KIND_OF]
        assert not missing, f"no AccountKind for {missing}"

    def test_every_kind_is_used(self) -> None:
        """The other direction. A kind nothing is classified as is a distinction
        this system does not actually make."""
        assert {KIND_OF[account] for account in Account} == set(AccountKind)

    def test_every_transaction_kind_is_producible(self) -> None:
        """The check this repository keeps needing: a value declared and never
        written. A `TransactionKind` no constructor emits is a row type that will
        never appear, and a report grouping by it would show an empty bucket
        forever.
        """
        produced = {
            accrue(amount_minor=1, currency=EUR).kind,
            submit(amount_minor=1, currency=EUR).kind,
            settle(amount_minor=1, currency=EUR).kind,
            fail(amount_minor=1, currency=EUR).kind,
            opening(amount_minor=1, currency=EUR).kind,
        }
        assert produced == set(TransactionKind)


class TestTheMovementsTellTheRightStory:
    def test_an_accrual_creates_a_liability(self) -> None:
        balances = trial_balance((accrue(amount_minor=AMOUNT, currency=EUR),))
        assert balances[Account.EXPENSE] == AMOUNT
        assert balances[Account.ACCOUNTS_PAYABLE] == -AMOUNT
        assert KIND_OF[Account.ACCOUNTS_PAYABLE] is AccountKind.LIABILITY

    def test_a_submission_moves_the_liability_into_transit(self) -> None:
        balances = trial_balance(
            (
                accrue(amount_minor=AMOUNT, currency=EUR),
                submit(amount_minor=AMOUNT, currency=EUR),
            )
        )
        assert balances[Account.ACCOUNTS_PAYABLE] == 0, "we no longer owe it"
        assert balances[Account.CASH_IN_TRANSIT] == -AMOUNT, "and it has not arrived"
        assert Account.CASH not in balances, "cash has not moved yet"

    def test_a_settlement_closes_the_story(self) -> None:
        balances = trial_balance(
            (
                opening(amount_minor=1_000_000, currency=EUR),
                accrue(amount_minor=AMOUNT, currency=EUR),
                submit(amount_minor=AMOUNT, currency=EUR),
                settle(amount_minor=AMOUNT, currency=EUR),
            )
        )
        assert balances[Account.ACCOUNTS_PAYABLE] == 0
        assert balances[Account.CASH_IN_TRANSIT] == 0
        assert balances[Account.CASH] == 1_000_000 - AMOUNT
        assert balances[Account.EXPENSE] == AMOUNT

    def test_a_failure_gives_the_liability_back(self) -> None:
        """Not by deleting the submission. Three transactions saying what
        happened, rather than none saying it never did."""
        transactions = (
            accrue(amount_minor=AMOUNT, currency=EUR),
            submit(amount_minor=AMOUNT, currency=EUR),
            fail(amount_minor=AMOUNT, currency=EUR),
        )
        balances = trial_balance(transactions)
        assert balances[Account.ACCOUNTS_PAYABLE] == -AMOUNT, "we owe it again"
        assert balances[Account.CASH_IN_TRANSIT] == 0
        assert Account.CASH not in balances, "no money ever left"
        assert len(transactions) == 3, "and the ledger still says all three things"

    def test_the_trial_balance_sums_to_zero_in_every_state(self) -> None:
        """The oldest check in accounting, and the one number that goes non-zero
        the moment anything anywhere has gone wrong."""
        history = [
            opening(amount_minor=1_000_000, currency=EUR),
            accrue(amount_minor=AMOUNT, currency=EUR),
            submit(amount_minor=AMOUNT, currency=EUR),
            settle(amount_minor=AMOUNT, currency=EUR),
            accrue(amount_minor=250_00, currency=EUR),
            submit(amount_minor=250_00, currency=EUR),
            fail(amount_minor=250_00, currency=EUR),
        ]
        for length in range(1, len(history) + 1):
            balances = trial_balance(tuple(history[:length]))
            assert sum(balances.values()) == 0, f"unbalanced after {length} transactions"

    def test_the_trial_balance_of_nothing_is_nothing(self) -> None:
        assert trial_balance(()) == {}
