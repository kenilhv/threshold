"""A payment rail that moves no money and behaves like one that does.

The interesting property is that it is **idempotent without keeping state**. Its
answer is a pure function of the idempotency key, so asking twice returns the
same result for the same reason a real rail does -- because the key identifies
the payment rather than the attempt -- and it does so without a table, a cache,
or a process that has to stay alive.

That makes the retry path testable in CI, hermetically, which is the whole
reason this tier exists. A rail that answered randomly would make
"resubmitting with the same key is safe" a claim no test could check.

## Why it rejects some payments

About one in thirty-two, chosen by the key's own hash. A sandbox that accepted
everything would leave `PAYMENT_FAILED`, the reversing ledger transaction and
the whole rejection path as code nothing ever runs -- and this repository has
found that failure mode five times in other guises.

It is deterministic per key, so the same invoice is always rejected and the
demo is reproducible. `REJECT_ONE_IN` is the knob and
`test_rail_sandbox.py` asserts the rate is neither zero nor everything.

## Why it never raises

`RailUnavailableError` is the state that makes payment systems hard, and a
sandbox that produced it at random would make every test flaky. The executor's
handling of it is proved instead by `tests/unit/test_payments.py`, which uses a
rail that always raises -- a stub written for that one purpose, where randomness
would be the wrong tool.
"""

from __future__ import annotations

import hashlib
from typing import Final

from threshold.adapters.rail.port import (
    PaymentInstruction,
    PaymentRail,
    RailOutcome,
    RailResult,
)

#: Bumped when the acceptance rule or the reference format changes, because both
#: change what a stored payment row means.
SANDBOX_ID: Final = "sandbox-2026.09.1"

#: One in thirty-two. High enough that a demo of a few dozen payments shows the
#: rejection path, low enough that it does not read as broken.
REJECT_ONE_IN: Final = 32

REJECTION_REASON: Final = "beneficiary account rejected the credit"


def _digest(idempotency_key: str) -> bytes:
    return hashlib.sha256(idempotency_key.encode("utf-8")).digest()


def decide(idempotency_key: str) -> RailOutcome:
    """Accept or reject, as a pure function of the key.

    Exported so the tests can assert the rate without reaching into the adapter,
    and so a demo script can say in advance which invoices will fail.
    """
    return (
        RailOutcome.REJECTED
        if _digest(idempotency_key)[0] % REJECT_ONE_IN == 0
        else RailOutcome.ACCEPTED
    )


class SandboxRail:
    """Deterministic, stateless, and idempotent because of both."""

    @property
    def rail_id(self) -> str:
        return SANDBOX_ID

    async def submit(self, instruction: PaymentInstruction) -> RailResult:
        outcome = decide(instruction.idempotency_key)
        reference = f"SBX-{_digest(instruction.idempotency_key).hex()[:16].upper()}"
        return RailResult(
            outcome=outcome,
            rail_reference=reference,
            reason=REJECTION_REASON if outcome is RailOutcome.REJECTED else None,
        )


# Conformance to the port, checked by mypy rather than asserted at runtime. The
# Protocol is structural, so nothing else would notice a signature drifting.
_CONFORMS: PaymentRail = SandboxRail()

__all__ = ["REJECTION_REASON", "REJECT_ONE_IN", "SANDBOX_ID", "SandboxRail", "decide"]
