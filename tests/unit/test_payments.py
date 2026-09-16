"""The payment state machine, walked exhaustively.

The interesting tests are the ones about the transitions that must be refused.
A machine that permits everything is a boolean with extra steps, and the move
that has to be impossible -- a settled payment submitting again -- is the one an
ad-hoc `if` forgets.
"""

from __future__ import annotations

import uuid

import pytest

from threshold.domain.ledger import Account, TransactionKind
from threshold.domain.payments import (
    ALLOWED,
    PAYMENT_ACCOUNTS,
    POSTS,
    TERMINAL,
    PaymentError,
    PaymentRequest,
    PaymentState,
    posting_for,
    transition,
)

IDS = {name: uuid.uuid4() for name in ("approval", "match", "document", "vendor", "account")}


def a_request(**overrides: object) -> PaymentRequest:
    fields: dict[str, object] = {
        "approval_decision_id": IDS["approval"],
        "match_result_id": IDS["match"],
        "document_id": IDS["document"],
        "vendor_id": IDS["vendor"],
        "vendor_bank_account_id": IDS["account"],
        "amount_minor": 482_000,
        "currency": "EUR",
        "currency_exponent": 2,
    }
    fields.update(overrides)
    return PaymentRequest(**fields)  # type: ignore[arg-type]


class TestTheMachineIsTotalAndClosed:
    def test_every_state_appears_in_the_table(self) -> None:
        """A state with no entry would raise KeyError in `transition` rather
        than refusing, which is a crash where a refusal was meant."""
        assert set(ALLOWED) == set(PaymentState)

    def test_every_target_is_a_real_state(self) -> None:
        for source, targets in ALLOWED.items():
            for target in targets:
                assert target in PaymentState, f"{source.value} -> {target}"

    def test_every_state_is_reachable_except_the_first(self) -> None:
        """An unreachable state is a row shape that can never exist, and a
        report grouping by it shows an empty bucket forever."""
        reachable = {target for targets in ALLOWED.values() for target in targets}
        unreachable = set(PaymentState) - reachable - {PaymentState.PENDING}
        assert not unreachable, f"nothing can produce {sorted(s.value for s in unreachable)}"

    def test_the_terminal_states_are_the_ones_with_no_exits(self) -> None:
        derived = {state for state, targets in ALLOWED.items() if not targets}
        assert derived == TERMINAL

    def test_a_terminal_payment_goes_nowhere(self) -> None:
        for state in TERMINAL:
            for target in PaymentState:
                with pytest.raises(PaymentError, match="terminal"):
                    transition(state, target)

    def test_a_settled_payment_cannot_be_submitted_again(self) -> None:
        """Spelled out separately from the loop above because it is the one that
        moves money twice, and a test named after it is a test somebody reads
        before deleting the rule."""
        with pytest.raises(PaymentError, match="cannot become submitting"):
            transition(PaymentState.SETTLED, PaymentState.SUBMITTING)

    def test_a_failed_payment_cannot_settle(self) -> None:
        with pytest.raises(PaymentError, match="cannot become settled"):
            transition(PaymentState.FAILED, PaymentState.SETTLED)

    def test_a_pending_payment_cannot_skip_straight_to_settled(self) -> None:
        """The skip that would leave the ledger without a submission posting."""
        with pytest.raises(PaymentError, match="cannot become settled"):
            transition(PaymentState.PENDING, PaymentState.SETTLED)

    def test_the_happy_path_walks(self) -> None:
        state = PaymentState.PENDING
        for target in (PaymentState.SUBMITTING, PaymentState.SUBMITTED, PaymentState.SETTLED):
            state = transition(state, target)
        assert state is PaymentState.SETTLED

    def test_a_rail_that_did_not_answer_leaves_the_payment_where_it_was(self) -> None:
        """`SUBMITTING -> SUBMITTING` is legal and load-bearing. A rail that
        timed out may still have taken the money, so the only safe move is to
        stay put and ask again with the same key."""
        assert transition(PaymentState.SUBMITTING, PaymentState.SUBMITTING) is (
            PaymentState.SUBMITTING
        )

    def test_a_rejection_is_reachable_only_from_submitting(self) -> None:
        """Because a rejection reverses a posting, and only `SUBMITTING` has one
        to reverse."""
        sources = [state for state, targets in ALLOWED.items() if PaymentState.FAILED in targets]
        assert sources == [PaymentState.SUBMITTING]


class TestTheLedgerPostingsMatchTheMachine:
    def test_every_posting_is_on_a_legal_transition(self) -> None:
        """A posting keyed off a move the machine cannot make is a ledger entry
        nothing will ever write."""
        for (source, target), kind in POSTS.items():
            assert target in ALLOWED[source], f"{kind.value} posts on an illegal transition"

    def test_entering_submitting_posts_the_money_out(self) -> None:
        """Before the rail is called, not after. A payment that left the
        building with no entry against it is the failure this ordering exists
        to prevent."""
        assert (
            posting_for(PaymentState.PENDING, PaymentState.SUBMITTING)
            is TransactionKind.PAYMENT_SUBMITTED
        )

    def test_a_rejection_posts_the_reversal(self) -> None:
        assert (
            posting_for(PaymentState.SUBMITTING, PaymentState.FAILED)
            is TransactionKind.PAYMENT_FAILED
        )

    def test_acceptance_posts_nothing(self) -> None:
        """The money was accounted for on the way into `SUBMITTING`. Posting
        again here would double it."""
        assert posting_for(PaymentState.SUBMITTING, PaymentState.SUBMITTED) is None

    def test_a_retry_posts_nothing(self) -> None:
        """The case that would silently double the ledger on every timeout."""
        assert posting_for(PaymentState.SUBMITTING, PaymentState.SUBMITTING) is None

    def test_settlement_posts_the_cash_out(self) -> None:
        assert (
            posting_for(PaymentState.SUBMITTED, PaymentState.SETTLED)
            is TransactionKind.PAYMENT_SETTLED
        )

    def test_the_accounts_a_payment_touches_are_the_ones_declared(self) -> None:
        """`PAYMENT_ACCOUNTS` is what the reporting endpoint reads. An account
        added to a movement without being added here would be money moving
        somewhere no report looks."""
        from threshold.domain import ledger

        touched: set[Account] = set()
        for kind in POSTS.values():
            build = {
                TransactionKind.PAYMENT_SUBMITTED: ledger.submit,
                TransactionKind.PAYMENT_FAILED: ledger.fail,
                TransactionKind.PAYMENT_SETTLED: ledger.settle,
            }[kind]
            touched |= {p.account for p in build(amount_minor=1, currency="EUR").postings}
        assert touched == PAYMENT_ACCOUNTS


class TestARequestValidatesBeforeARowExists:
    def test_a_well_formed_request_is_accepted(self) -> None:
        assert a_request().amount_minor == 482_000

    @pytest.mark.parametrize("amount", [0, -1, -482_000])
    def test_a_non_positive_amount_is_refused(self, amount: int) -> None:
        with pytest.raises(PaymentError, match=r"not a payment|refund"):
            a_request(amount_minor=amount)

    def test_a_negative_exponent_is_refused(self) -> None:
        with pytest.raises(PaymentError, match="exponent"):
            a_request(currency_exponent=-2)

    def test_the_idempotency_key_is_stable_across_attempts(self) -> None:
        """A key generated per attempt is the same as having no key: the rail
        would read each retry as a new instruction and pay again."""
        assert a_request().idempotency_key() == a_request().idempotency_key()

    def test_the_key_is_different_for_a_different_authorisation(self) -> None:
        other = a_request(approval_decision_id=uuid.uuid4())
        assert other.idempotency_key() != a_request().idempotency_key()

    def test_the_key_does_not_depend_on_the_amount(self) -> None:
        """Deliberately. If a corrected amount produced a new key, a resubmission
        after a correction would be a second payment at the rail rather than the
        same one -- and the correction is exactly when that matters."""
        assert a_request(amount_minor=1).idempotency_key() == a_request().idempotency_key()

    def test_the_key_is_namespaced(self) -> None:
        """So a value found in a rail's dashboard is traceable to this system
        rather than to whichever service happened to use a bare UUID."""
        assert a_request().idempotency_key().startswith("thr-pay-")
