"""The triage boundary.

One interface, two tiers, exactly as `adapters/extraction/port.py` does it. The
deterministic tier is worse and always available; a model tier would be better
at the one thing that actually needs judgment -- the sentence -- and needs
weights, credentials and network. Both return the same validated packet, and
every stored escalation records which tier produced it, so an evaluation can
never silently average them together.

## Why the whole surface is fetched before the triager runs

`triage()` receives the invoice already read, rather than a session to read it
with. The agent's readable surface for one invoice is four rows -- the match
result, its exceptions, the document record and the purchase order -- and there
is nothing to choose between them. A tool-calling loop over four known rows buys
no selectivity and costs a class of failures: partial reads, loops that never
terminate, a model that decides not to look at the exceptions.

The tools are not bypassed. `services/triage.py` builds the request by calling
`tools.resolve(...)` for each one, through a session opened `SET TRANSACTION
READ ONLY`, so the registry stays the production path and the perimeter is
enforced once for every tier rather than trusted to each of them.

## What a triager may return

An `EscalationPacket`, or `None`. `None` means "no opinion", and it is safe
precisely because it is uninteresting: the exceptions were routed and clocked
when they were raised, so an agent that says nothing leaves the system in the
state it would have been in with no agent at all. `tests/unit/test_agent_red_
team.py` holds that line.

A triager may not raise to signal "nothing to say". `TriageError` means the tier
broke -- a provider that stayed down through its retries, a response that would
not parse -- which is an incident, where `None` is a business outcome. The two
must not collapse, for the same reason `ExtractionError` is separate from a
low-confidence result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from threshold.domain.approval import OpenException
from threshold.domain.triage import EscalationPacket


class TriageError(RuntimeError):
    """The tier could not produce an answer at all.

    Distinct from returning ``None``. ``None`` is a triager that read the
    invoice and had nothing useful to add, which is a normal outcome on a
    single-exception invoice. This means the tier is broken.
    """


@dataclass(frozen=True, slots=True)
class TriageRequest:
    """One invoice, as the agent is allowed to see it.

    The four projections are plain dictionaries because that is what
    ``domain/projection.py`` returns and what a model tier would serialise. They
    have already been through the projection at INTERNAL, so nothing restricted
    is in here -- which is why this dataclass can be handed to a third party's
    API without a second review.
    """

    match_result_id: Any
    #: The exceptions as recorded, with the owner written at raise time. This is
    #: the evidence a packet is validated against, and it does not come from the
    #: triager.
    recorded: tuple[OpenException, ...]
    match: dict[str, Any]
    exceptions: list[dict[str, Any]]
    document: dict[str, Any] | None
    purchase_order: dict[str, Any] | None


@runtime_checkable
class Triager(Protocol):
    """Reads one failed invoice and says who should look first, or says nothing."""

    @property
    def triager_id(self) -> str:
        """Identifies this triager precisely enough to reproduce a result.

        A ruleset version, or a model name and version. Stored on every
        escalation row so a change in output can be attributed to a change in
        triager rather than investigated as a mystery.
        """
        ...

    async def triage(self, request: TriageRequest) -> EscalationPacket | None: ...
