"""Running the triage agent, and keeping it on its side of the wall.

The agent is a function from an invoice to an opinion. This is everything
around it: reading what it may see, calling it, checking what it said against
what was recorded, and writing the result down. Each of those is here rather
than in the agent for the same reason -- an agent that fetched its own evidence
could fetch different evidence, and an agent that wrote its own conclusion would
be a different system.

## Two sessions, and the reason there are two

The evidence is read inside `tools.read_only`, whose transaction Postgres itself
will not let anything write. The escalation goes into the caller's session,
which is a different connection and is only touched after the read-only
transaction has closed. So the connection the agent's data arrived on is
provably incapable of carrying the agent's output back.

That is stronger than it sounds. The usual shape of this bug is a single session
threaded through a pipeline until some helper deep inside it flushes, and no
review catches it because the write looks local and correct. Here there is
nothing to catch: the readable session is never in scope where the write is.

## The reads go through the registry

`_gather` calls `tools.resolve("get_match")` rather than `tools.get_match`, and
the difference is not decorative. It makes the registry the production path, so
`test_agent_boundary.py`'s assertions about it are assertions about what runs
rather than about a table nothing consults. A tool added to that registry is
reachable from here; a tool removed from it breaks here.

## The agent does not decide whether to escalate, only what to say

Every invoice with exceptions is already routed and clocked. This service runs
after the match, records what the triager returned, and changes nothing about
the invoice either way -- `record_escalation` writes one row in one table that
no authorisation path reads. If the triager returns ``None``, or raises, or is
never called at all, the invoice sits in exactly the queues it was already in.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from threshold.adapters.triage.port import Triager, TriageRequest
from threshold.adapters.triage.rules import RuleBasedTriager
from threshold.agent import tools
from threshold.domain.approval import OpenException
from threshold.domain.models import Escalation, MatchResult
from threshold.domain.triage import EscalationPacket, EscalationRefusedError
from threshold.services.approvals import ApprovalService
from threshold.services.audit import Actor, record

log = structlog.get_logger(__name__)


class TriageService:
    """Reads, calls, validates, writes. In that order, and never overlapping."""

    def __init__(self, triager: Triager | None = None) -> None:
        #: Defaults to the tier that needs nothing, so every environment that
        #: can run the tests can run the agent path end to end.
        self._triager = triager or RuleBasedTriager()

    async def triage(
        self,
        writing: AsyncSession,
        match_result_id: uuid.UUID,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> Escalation | None:
        """Escalate one decided invoice, or record that there was nothing to say.

        The caller supplies the session the escalation is written into, and does
        not supply the one it is read through. That asymmetry is the design: the
        write belongs in the caller's transaction, so a job that fails after
        escalating rolls the escalation back and its retry does not collide with
        it -- while the read happens somewhere the caller cannot reach and could
        not have written through if it tried.
        """
        async with tools.read_only(sessions) as reading:
            request = await self._gather(reading, match_result_id)
        if request is None:
            return None

        packet = await self._ask(request)
        if packet is None:
            log.info(
                "triage.no_opinion",
                match_result_id=str(match_result_id),
                triager=self._triager.triager_id,
                owners=len({row.owner.value for row in request.recorded}),
            )
            return None

        return await self.record_escalation(writing, packet)

    async def _gather(
        self, session: AsyncSession, match_result_id: uuid.UUID
    ) -> TriageRequest | None:
        """Read the agent's whole world, through the registry, read-only.

        Returns ``None`` when there is nothing to triage: no such match result,
        or one that raised no exceptions. The second is not an error -- it is
        every invoice that passed.
        """
        result = (
            await session.execute(select(MatchResult).where(MatchResult.id == match_result_id))
        ).scalar_one_or_none()
        if result is None:
            log.warning("triage.match_result_missing", match_result_id=str(match_result_id))
            return None

        recorded: Sequence[OpenException] = await ApprovalService().open_exceptions(
            session, match_result_id
        )
        if not recorded:
            return None

        match = await tools.resolve("get_match").handler(session, match_result_id)
        exceptions = await tools.resolve("get_exceptions").handler(session, match_result_id)
        document = await tools.resolve("get_document").handler(session, result.document_id)
        order = (
            None
            if result.purchase_order_id is None
            else await tools.resolve("get_purchase_order").handler(
                session, result.purchase_order_id
            )
        )

        if match is None:
            # The row was read a moment ago and the projection dropped it, which
            # would mean the projection is broken rather than that the invoice is.
            log.error("triage.projection_returned_nothing", match_result_id=str(match_result_id))
            return None

        return TriageRequest(
            match_result_id=match_result_id,
            recorded=tuple(recorded),
            match=match,
            exceptions=exceptions,
            document=document,
            purchase_order=order,
        )

    async def _ask(self, request: TriageRequest) -> EscalationPacket | None:
        """Call the triager and treat a refusal as a fact about the triager.

        A packet that fails validation is not repaired and not retried. It is the
        clearest signal this system can get that a tier is behaving strangely --
        a captured model, a bad prompt, a provider returning someone else's
        response -- and the one thing that must not happen to it is a quiet
        recovery that makes it look like nothing happened.
        """
        try:
            return await self._triager.triage(request)
        except EscalationRefusedError as refusal:
            log.warning(
                "triage.refused",
                match_result_id=str(request.match_result_id),
                triager=self._triager.triager_id,
                refusal=str(refusal),
            )
            return None

    async def record_escalation(
        self, session: AsyncSession, packet: EscalationPacket
    ) -> Escalation:
        """Write down what the agent said. Not what it decided; it decided nothing.

        The audit row is written in the same transaction, with an ``agent``
        actor -- a value that has been legal in the schema since M0 and had no
        producer until here. "The system did this" and "an agent suggested this"
        are different claims, and an audit trail that merged them would make the
        second unfalsifiable.
        """
        escalation = Escalation(
            match_result_id=packet.match_result_id,
            owner_role=packet.owner.value,
            codes=[code.value for code in packet.codes],
            summary=packet.summary.untrusted,
            triager_id=self._triager.triager_id,
        )
        session.add(escalation)
        await session.flush()

        await record(
            session,
            actor=Actor.agent(self._triager.triager_id),
            action="invoice.escalated",
            entity_type="match_result",
            entity_id=packet.match_result_id,
            after={
                "owner_role": packet.owner.value,
                "codes": [code.value for code in packet.codes],
                # The codes recorded but not cited. The only durable trace an
                # agent that stayed quiet about something leaves behind.
                "uncited": sorted(
                    code.value
                    for code in {row.code for row in packet.grounded_in} - set(packet.codes)
                ),
                "triager_id": self._triager.triager_id,
            },
        )
        return escalation
