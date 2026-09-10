"""One way to write an audit row.

`AuditEvent` has existed since M0 with an append-only trigger behind it, and
until now there were two hand-rolled `session.add(AuditEvent(...))` blocks and
no third. M4 adds four more call sites, which is the point at which three
shapes becomes a guarantee that they will disagree.

Two things this fixes beyond consistency.

`trace_id` was declared, indexed, and never written by anything. The column, the
index and the docstring promising it "is the same id as the distributed trace"
were all present; no code populated it. Correlating an audit row to the request
that produced it was therefore impossible, which is most of what an audit trail
is for. It is now read from the request context automatically, so a caller
cannot forget it.

And the actor. `actor_type` was always `'system'` because nothing else could act
yet. `Actor` makes the human case as easy to write as the machine one, which
matters for the property M4 is really about: the machine and the person write
to the same table, in the same shape, and a query that asks "what did this
system decide without asking anybody" is one `WHERE`.

The row is always written in the caller's transaction, never in its own. An
audit trail that commits separately from the change it describes will eventually
record something that did not happen, or miss something that did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.domain.models import AuditEvent

log = structlog.get_logger(__name__)

ActorType = Literal["system", "human", "agent"]

#: `AuditEvent.trace_id` is String(32).
_TRACE_ID_MAX: Final = 32


@dataclass(frozen=True, slots=True)
class Actor:
    """Who did it.

    ``id`` is the issuer's subject for a human and ``None`` for the system. It
    is deliberately not the username: usernames are reassigned, and an audit row
    that says "clerk" three years later may mean a different person.
    """

    type: ActorType
    id: str | None = None
    #: Kept alongside the subject purely so a reader does not have to resolve a
    #: UUID against an identity provider to learn who this was. INTERNAL.
    label: str | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls(type="system")


def current_trace_id() -> str | None:
    """The request id bound by the API middleware, if this is a request.

    Returns ``None`` in the worker, which has no request. That is honest rather
    than convenient: a worker's audit rows genuinely have no request to
    correlate to until M5 propagates a trace through the queue payload.
    """
    bound: dict[str, Any] = structlog.contextvars.get_contextvars()
    value = bound.get("request_id")
    if not isinstance(value, str) or not value:
        return None
    return value[:_TRACE_ID_MAX]


async def record(
    session: AsyncSession,
    *,
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AuditEvent:
    """Append one row describing one state change.

    ``action`` is a dotted verb in the past tense — ``document.received``,
    ``match.decided``, ``invoice.approved``. Treat these the way the exception
    codes are treated: add, never repurpose. They are what a query groups by.
    """
    event = AuditEvent(
        actor_type=actor.type,
        actor_id=actor.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        reason=reason,
        trace_id=current_trace_id(),
    )
    session.add(event)
    return event
