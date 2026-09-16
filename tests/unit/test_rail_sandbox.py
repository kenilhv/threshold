"""The sandbox rail, and the one property that makes it worth testing against.

It is idempotent without keeping state, because its answer is a pure function of
the idempotency key. That is the same property a real rail provides for the same
reason -- the key identifies the payment, not the attempt -- and having it here
means "resubmitting with the same key is safe" is a claim CI can check on every
commit rather than a paragraph in a runbook.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from threshold.adapters.rail.port import (
    PaymentInstruction,
    PaymentRail,
    RailOutcome,
    RailResult,
)
from threshold.adapters.rail.sandbox import (
    REJECT_ONE_IN,
    SANDBOX_ID,
    SandboxRail,
    decide,
)


def instruct(key: str) -> PaymentInstruction:
    return PaymentInstruction(
        idempotency_key=key,
        amount_minor=482_000,
        currency="EUR",
        payee_account_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        reference="INV-4471",
    )


def submit(key: str) -> RailResult:
    return asyncio.run(SandboxRail().submit(instruct(key)))


class TestItIsIdempotent:
    def test_the_same_key_gets_the_same_answer(self) -> None:
        """The property the retry path depends on. Asserted over many keys
        rather than one, because one key is a coincidence."""
        for index in range(200):
            key = f"thr-pay-{index}"
            assert submit(key) == submit(key)

    def test_different_keys_get_their_own_answers(self) -> None:
        """A vacuity guard: a rail that returned one constant would satisfy the
        test above perfectly."""
        references = {submit(f"thr-pay-{index}").rail_reference for index in range(50)}
        assert len(references) == 50

    def test_it_keeps_no_state(self) -> None:
        """A fresh instance answers identically, which is what makes the
        property survive a process restart -- the case the retry path is for."""
        key = "thr-pay-restart"
        first = asyncio.run(SandboxRail().submit(instruct(key)))
        second = asyncio.run(SandboxRail().submit(instruct(key)))
        assert first == second

    def test_the_answer_does_not_depend_on_the_amount(self) -> None:
        """Because the key is what identifies the payment. A rail whose answer
        moved with the amount would make a corrected resubmission a different
        payment, which is the opposite of idempotent."""
        key = "thr-pay-amount"
        one = asyncio.run(SandboxRail().submit(instruct(key)))
        other = asyncio.run(
            SandboxRail().submit(
                PaymentInstruction(
                    idempotency_key=key,
                    amount_minor=1,
                    currency="EUR",
                    payee_account_id=uuid.uuid4(),
                    reference="something else",
                )
            )
        )
        assert one == other


class TestItRejectsSomeOfThem:
    def test_the_rejection_rate_is_neither_zero_nor_everything(self) -> None:
        """A sandbox that accepted everything would leave the rejection path,
        the reversing ledger transaction and the failed state as code nothing
        ever runs -- which is a defect this repository has found five times in
        other guises."""
        outcomes = [decide(f"thr-pay-{index}") for index in range(2000)]
        rejected = outcomes.count(RailOutcome.REJECTED)
        assert 0 < rejected < len(outcomes)

    def test_the_rate_is_near_the_declared_one(self) -> None:
        """Loose bounds on purpose: this asserts the knob is connected, not that
        SHA-256 is uniform."""
        sample = 4000
        rejected = sum(
            decide(f"thr-pay-{index}") is RailOutcome.REJECTED for index in range(sample)
        )
        expected = sample / REJECT_ONE_IN
        assert expected * 0.6 < rejected < expected * 1.6, f"{rejected} of {sample}"

    def test_a_rejection_says_why(self) -> None:
        key = next(
            f"thr-pay-{i}" for i in range(500) if decide(f"thr-pay-{i}") is RailOutcome.REJECTED
        )
        result = submit(key)
        assert result.outcome is RailOutcome.REJECTED
        assert result.reason

    def test_an_acceptance_invents_no_reason(self) -> None:
        key = next(
            f"thr-pay-{i}" for i in range(500) if decide(f"thr-pay-{i}") is RailOutcome.ACCEPTED
        )
        result = submit(key)
        assert result.outcome is RailOutcome.ACCEPTED
        assert result.reason is None


class TestTheResultTypeDefendsItself:
    def test_a_rejection_without_a_reason_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must say why"):
            RailResult(outcome=RailOutcome.REJECTED, rail_reference="X", reason=None)

    def test_an_acceptance_with_a_reason_is_refused(self) -> None:
        """A reason on an acceptance reads as a warning, and somebody will
        eventually surface it as one."""
        with pytest.raises(ValueError, match="invent a reason"):
            RailResult(outcome=RailOutcome.ACCEPTED, rail_reference="X", reason="fine")


class TestItIsARail:
    def test_it_satisfies_the_port(self) -> None:
        assert isinstance(SandboxRail(), PaymentRail)

    def test_it_identifies_itself(self) -> None:
        """A payment that cannot say which rail took it is a payment nobody can
        chase."""
        assert SandboxRail().rail_id == SANDBOX_ID
        assert SANDBOX_ID.startswith("sandbox-")

    def test_every_reference_is_traceable_to_this_rail(self) -> None:
        assert submit("thr-pay-1").rail_reference.startswith("SBX-")
