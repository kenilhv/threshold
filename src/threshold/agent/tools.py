"""What the agent can reach, and why it cannot reach anything else.

Three mechanisms, in increasing order of how much they are worth.

**The registry is a closed set.** A model can only call what is in `TOOLS`, and
a name it invents is a refusal rather than an attempt. This is worth the least,
because it is the one a future edit can widen without noticing.

**Nothing here takes a session it could write through.** Every tool receives an
`AsyncSession` obtained from `read_only`, which opens its transaction with
`SET TRANSACTION READ ONLY`. That is Postgres refusing, not this module
refusing: an `INSERT` issued through it raises `read-only sql transaction`
regardless of what the tool meant to do. A mislabelled tool, a helper that grew
a write, an ORM flush somebody forgot about — all of them fail at the database.
`test_agent_boundary.py` proves it by trying.

**The agent does not perform the escalation.** `escalate` is not in this
registry, and that is deliberate: it is not a tool, it is how the loop ends. The
agent returns an `EscalationPacket` and the calling service decides what to do
with it. So there is no write path to remove, no approve tool to guard, and no
prompt to harden — a completely captured model can request a person's attention
and can do nothing else.

The residual risk is named rather than hidden: within that surface a hostile
model can still escalate to the wrong owner, or write a reassuring summary that
persuades the person who reads it. Those are bounded, and they are bounded by
the vocabulary in `domain/triage.py` and by how the console renders agent text.
They are not eliminated, and the red-team suite asserts the bound rather than
claiming the risk is gone.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from threshold.adapters.db import get_sessionmaker
from threshold.domain.classification import Sensitivity
from threshold.domain.models import Document, MatchException, MatchResult, PurchaseOrder
from threshold.domain.projection import project

log = structlog.get_logger(__name__)

#: What the agent is shown: INTERNAL, minus the columns that name somebody.
#:
#: RESTRICTED is dropped, so no bank digits and no tax identifier -- the agent
#: sees that a `bank_details_changed` exception fired and never a bank number.
#: Identity columns are dropped too: the vendor's name, the buyer's email
#: address, the filename the supplier chose.
#:
#: This is the privacy control and the exfiltration control at once. A model
#: cannot leak what was never put in front of it, and that holds however
#: thoroughly it has been captured. Withholding the filename also removes an
#: injection channel rather than defending it: that string is chosen by whoever
#: sent the document.
#:
#: What this does *not* claim, because an earlier version of this comment did
#: and it was false: invoice numbers, amounts, purchase-order numbers and the
#: supplier's own line descriptions still reach the agent, inside the free text
#: of an exception's `detail` and `breach`. They are the evidence it reasons
#: about. `test_agent_red_team.py` treats `detail` as hostile input for exactly
#: that reason.
AGENT_SENSITIVITY: Final = Sensitivity.INTERNAL

#: Identity columns are withheld. D-036 says why this is a drop rather than the
#: keyed alias scheme the README promised.
AGENT_IDENTITIES: Final = False


class UnknownToolError(LookupError):
    """The model asked for something that is not a tool."""


@asynccontextmanager
async def read_only(
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """A session whose transaction the database itself will not let you write.

    `SET TRANSACTION READ ONLY` is issued before anything else, so every
    statement in this scope is covered. It is the difference between a rule this
    module enforces and a rule Postgres enforces, and only the second one
    survives somebody adding a helper without reading this docstring.

    Rolled back rather than committed on the way out. There is nothing to
    commit, and calling commit on a read-only transaction would be a claim that
    there might have been.
    """
    async with (sessions or get_sessionmaker())() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield session
        finally:
            await session.rollback()


@dataclass(frozen=True, slots=True)
class Tool:
    """One thing the agent may do. All of them read."""

    name: str
    description: str
    handler: Callable[..., Awaitable[Any]]


async def get_match(session: AsyncSession, match_result_id: uuid.UUID) -> dict[str, Any] | None:
    """The decision that failed, projected to what the agent may see."""
    row = (
        await session.execute(select(MatchResult).where(MatchResult.id == match_result_id))
    ).scalar_one_or_none()
    return (
        None if row is None else project(row, level=AGENT_SENSITIVITY, identities=AGENT_IDENTITIES)
    )


async def get_exceptions(session: AsyncSession, match_result_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every control that fired on this invoice, and what each one says."""
    rows = (
        (
            await session.execute(
                select(MatchException)
                .where(MatchException.match_result_id == match_result_id)
                .order_by(MatchException.code)
            )
        )
        .scalars()
        .all()
    )
    return [project(row, level=AGENT_SENSITIVITY, identities=AGENT_IDENTITIES) for row in rows]


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> dict[str, Any] | None:
    """The document record. Not its bytes, and not its extraction."""
    row = (
        await session.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    return (
        None if row is None else project(row, level=AGENT_SENSITIVITY, identities=AGENT_IDENTITIES)
    )


async def get_purchase_order(
    session: AsyncSession, purchase_order_id: uuid.UUID
) -> dict[str, Any] | None:
    """The order the invoice was judged against."""
    row = (
        await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == purchase_order_id))
    ).scalar_one_or_none()
    return (
        None if row is None else project(row, level=AGENT_SENSITIVITY, identities=AGENT_IDENTITIES)
    )


#: Everything the agent can call. Adding to this is a security change, and
#: `test_agent_boundary.py` is written so that adding a tool which writes fails
#: the suite rather than passing it quietly.
TOOLS: Final[Mapping[str, Tool]] = {
    tool.name: tool
    for tool in (
        Tool(
            name="get_match",
            description="The match result that failed, as the reviewer sees it.",
            handler=get_match,
        ),
        Tool(
            name="get_exceptions",
            description="The controls that fired, with severity and owner.",
            handler=get_exceptions,
        ),
        Tool(
            name="get_document",
            description="The document record: filename, size, when it arrived.",
            handler=get_document,
        ),
        Tool(
            name="get_purchase_order",
            description="The purchase order the invoice was judged against.",
            handler=get_purchase_order,
        ),
    )
}


def resolve(name: str) -> Tool:
    """Look up a tool, or refuse by name.

    Refuses rather than returning None so that a model inventing `approve` or
    `pay_invoice` produces a loud, logged, attributable event instead of a
    quiet no-op that looks like the tool ran and did nothing.
    """
    tool = TOOLS.get(name)
    if tool is None:
        log.warning("agent.tool_refused", tool=name, available=sorted(TOOLS))
        raise UnknownToolError(f"no tool named {name!r}; the agent has {sorted(TOOLS)}")
    return tool
