"""The payment rail boundary: the one place this system talks to a bank.

Everything upstream of here is bookkeeping this repository controls. Past here is
a third party that can be slow, can be down, can accept a payment and fail to
tell us, and cannot be asked to forget. That asymmetry is what the interface is
shaped around.

## Three outcomes, not two

A rail call returns accepted or rejected, or it raises. The distinction that
matters is not success versus failure -- it is *known* versus *unknown*.

* A `RailResult` means the rail answered. Accepted or rejected, we know which,
  and the payment moves to a terminal-ish state.
* `RailUnavailableError` means the rail did not answer. The payment may or may
  not have been taken. This is the state that makes payment systems hard, and
  collapsing it into "failed" is how a supplier gets paid twice.

So an unavailable rail leaves the payment `submitted` and retries with **the same
idempotency key**, which is the entire mechanism: the rail is asked again, and a
rail that already took the money returns the original result rather than taking
it again.

## The instruction carries no account digits

`payee_account_id` is this system's own `vendor_bank_account.id`. The account
number and routing number on that row are RESTRICTED and are not read here, not
logged here, and not passed here. A real rail integration resolves them at the
last possible moment inside its own adapter; this repository's sandbox never
needs them at all.

That is not squeamishness. `domain/projection.py` drops restricted columns from
anything a model sees, and an instruction object carrying the digits would be
the obvious next thing somebody logs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class RailUnavailableError(RuntimeError):
    """The rail did not answer, so the payment's fate is unknown.

    Deliberately distinct from a rejection. A rejection is a business outcome --
    the rail looked at the instruction and said no. This is an incident, and the
    only safe response to it is to ask again with the same idempotency key.

    A payment executor that treated this as a failure would reverse the ledger
    for a payment that may well have gone out.
    """


class RailOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PaymentInstruction:
    """What we ask the rail to do. Carries no account digits -- see the module docstring."""

    #: Ours, stable across every retry of this payment, and the thing that makes
    #: asking twice safe. Derived from the payment row rather than generated per
    #: attempt, because a fresh key per attempt is the same as having none.
    idempotency_key: str
    amount_minor: int
    currency: str
    #: `vendor_bank_account.id` -- an identifier in this system, not at the bank.
    payee_account_id: uuid.UUID
    #: What the supplier sees on their statement.
    reference: str


@dataclass(frozen=True, slots=True)
class RailResult:
    """The rail's answer. Its existence is the point: it answered."""

    outcome: RailOutcome
    #: The rail's own identifier for this payment. Stored so a dispute can be
    #: taken to the rail with something it recognises.
    rail_reference: str
    #: Present on a rejection, absent on an acceptance.
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.outcome is RailOutcome.REJECTED) != (self.reason is not None):
            raise ValueError(
                "a rejection must say why and an acceptance must not invent a reason; "
                f"got {self.outcome.value} with reason={self.reason!r}"
            )


@runtime_checkable
class PaymentRail(Protocol):
    """Somewhere money can be sent."""

    @property
    def rail_id(self) -> str:
        """Identifies the rail and its version, stored on every payment row.

        A payment that cannot say which rail took it is a payment nobody can
        chase.
        """
        ...

    async def submit(self, instruction: PaymentInstruction) -> RailResult:
        """Send it, or raise `RailUnavailableError` if the rail did not answer.

        Must be idempotent on `instruction.idempotency_key`: called twice with
        the same key, it returns the same result and moves money once.
        """
        ...
