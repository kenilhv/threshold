"""Recording decisions, and refusing the ones that should not be recorded.

Three things can go wrong when two people work the same queue, and they are
different problems with different answers:

* **The evidence moved.** A receipt arrived, or the policy changed, so the
  matcher wrote a new `match_result` version while the reviewer was reading the
  old one. Their judgement was about a body of evidence that is no longer
  current. This is a 409 naming the version they saw and the version that
  exists, because the right next step is to look again — not to retry.
* **Someone already decided.** The partial unique index refuses the second
  terminal row. This is also a 409, and the response says who decided and what
  they decided, because the loser of that race needs to know the outcome, not
  to be told to try again.
* **They may not decide this one.** Answered by `domain/approval.py` from data
  already on the rows, and returned as a 403 naming the roles the invoice needs.

None of the three is a retry. A retry loop around any of them would either
record a decision about evidence nobody read or overwrite a colleague's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.queue import PostgresJobQueue
from threshold.domain.approval import (
    PAYING,
    TERMINAL,
    Authorisation,
    Decision,
    OpenException,
    authorise,
    requires_reason,
)
from threshold.domain.jobs import JobKind
from threshold.domain.models import ApprovalDecision, MatchException, MatchResult
from threshold.domain.roles import Role
from threshold.domain.taxonomy import ExceptionCode, OwnerRole, Severity
from threshold.observability.metrics import record_approval_decision
from threshold.services.audit import Actor, record

log = structlog.get_logger(__name__)

#: The decisions `uq_approval_decision_terminal` is scoped to, as the strings
#: the column holds. Derived from `TERMINAL` rather than written out, so a
#: fifth decision added to the enum cannot make this quietly incomplete.
TERMINAL_VALUES: Final[frozenset[str]] = frozenset(d.value for d in TERMINAL)


class ApprovalError(RuntimeError):
    """Base for refusals that are the caller's to resolve."""


class UnknownMatchResultError(ApprovalError):
    pass


class StaleEvidenceError(ApprovalError):
    """The reviewer decided on a match result that is no longer the latest."""

    def __init__(self, seen: int, current: int) -> None:
        super().__init__(
            f"this decision was made on version {seen}, but version {current} is current: "
            "the evidence changed while it was being reviewed"
        )
        self.seen = seen
        self.current = current


class AlreadyDecidedError(ApprovalError):
    """Somebody else decided this invoice first.

    ``existing`` is the decision that won, when this transaction can see it --
    which is the ordinary case, because the database runs READ COMMITTED and a
    statement issued after the conflict takes a fresh snapshot.

    It is ``None`` only when the caller holds a snapshot older than the winning
    commit, under REPEATABLE READ or tighter. Then the refusal can still say
    *that* the invoice was decided, because the unique index just said so, and
    cannot say by whom. Saying so is the point: the version this replaced passed
    the loser's own uncommitted row and confidently reported the wrong verb and
    the wrong name.
    """

    def __init__(self, existing: ApprovalDecision | None) -> None:
        if existing is None:
            super().__init__(
                "this invoice was already decided by somebody else, and this "
                "transaction's snapshot predates their decision, so it cannot say who"
            )
        else:
            super().__init__(
                f"this invoice was already {existing.decision} by "
                f"{existing.actor_label or existing.actor_type}"
            )
        self.existing = existing


class NotPermittedError(ApprovalError):
    def __init__(self, authorisation: Authorisation) -> None:
        super().__init__(authorisation.reason)
        self.authorisation = authorisation


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    match_result_id: uuid.UUID
    decision: Decision
    #: The `match_result.version` the reviewer was looking at. Optimistic
    #: concurrency: absent means the caller is not claiming to have read
    #: anything, which is only acceptable for the system's own decision.
    seen_version: int | None
    reason: str | None


async def _request_payment(session: AsyncSession, decision: ApprovalDecision) -> None:
    """Ask for this authorisation to be executed, in its own transaction.

    Enqueued here rather than called here, and in the same transaction as the
    decision, for the reason every other chain in this system is: if the
    approval rolls back, so does the request to act on it. A payment executed
    inline would mean money moving before the authorisation that permitted it
    was durable.

    The dedupe key is the decision, which is also what the payment row is unique
    on and what the rail's idempotency key is derived from. Three layers saying
    the same thing in three places, which is what lets them catch each other
    failing rather than all failing together.
    """
    if Decision(decision.decision) not in PAYING:
        return
    await PostgresJobQueue().enqueue(
        session,
        kind=JobKind.EXECUTE_PAYMENT,
        payload={"approval_decision_id": str(decision.id)},
        dedupe_key=f"pay:{decision.id}",
    )


class ApprovalService:
    async def record_auto_approval(
        self, session: AsyncSession, result: MatchResult
    ) -> ApprovalDecision:
        """The machine's decision, in the same table as everyone else's."""
        decision = ApprovalDecision(
            match_result_id=result.id,
            document_id=result.document_id,
            decision=Decision.AUTO_APPROVED.value,
            actor_type="system",
            overrode=[],
        )
        session.add(decision)
        await session.flush()
        await record(
            session,
            actor=Actor.system(),
            action="invoice.auto_approved",
            entity_type="approval_decision",
            entity_id=decision.id,
            after={
                "match_result_id": str(result.id),
                "document_id": str(result.document_id),
                "policy_version": result.policy_version,
            },
        )
        record_approval_decision(decision=Decision.AUTO_APPROVED.value, actor_type="system")
        await _request_payment(session, decision)
        return decision

    async def decide(
        self,
        session: AsyncSession,
        request: DecisionRequest,
        *,
        actor_id: str,
        actor_label: str,
        roles: frozenset[Role],
    ) -> ApprovalDecision:
        """Record a human decision, or refuse it and say which of the three it was."""
        result = (
            await session.execute(
                select(MatchResult).where(MatchResult.id == request.match_result_id)
            )
        ).scalar_one_or_none()
        if result is None:
            raise UnknownMatchResultError(f"no such match result: {request.match_result_id}")

        current = (
            await session.execute(
                select(func.max(MatchResult.version)).where(
                    MatchResult.document_id == result.document_id
                )
            )
        ).scalar_one()
        if request.seen_version is not None and request.seen_version != current:
            raise StaleEvidenceError(seen=request.seen_version, current=int(current))
        if result.version != current:
            raise StaleEvidenceError(seen=result.version, current=int(current))

        existing = (
            await session.execute(
                select(ApprovalDecision)
                .where(ApprovalDecision.match_result_id == result.id)
                .where(ApprovalDecision.decision.in_(TERMINAL_VALUES))
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AlreadyDecidedError(existing)

        exceptions = await self.open_exceptions(session, result.id)
        authorisation = authorise(holder=roles, exceptions=exceptions, decision=request.decision)
        if not authorisation.permitted:
            raise NotPermittedError(authorisation)

        if requires_reason(request.decision) and not (request.reason or "").strip():
            raise NotPermittedError(
                Authorisation(
                    permitted=False,
                    required_roles=frozenset(),
                    overrides=authorisation.overrides,
                    reason=(
                        "a decision must say why: the reason is the only part of the "
                        "record a later reader cannot reconstruct"
                    ),
                )
            )

        decision = ApprovalDecision(
            match_result_id=result.id,
            document_id=result.document_id,
            decision=request.decision.value,
            actor_type="human",
            actor_id=actor_id,
            actor_label=actor_label,
            reason=(request.reason or "").strip(),
            overrode=[code.value for code in authorisation.overrides],
        )
        # A savepoint, so losing the race is recoverable rather than terminal.
        #
        # Without it the IntegrityError poisons the session and the only row
        # available to describe the conflict is `decision` -- the one that just
        # failed to insert. That is the loser's own row, so the refusal read
        # "this invoice was already rejected by <the person being refused>"
        # when it had in fact been approved by somebody else. Both the verb and
        # the name were wrong, in a message about whether money moves.
        try:
            async with session.begin_nested():
                session.add(decision)
                await session.flush()
        except IntegrityError as exc:
            # Lost the race between the check above and this write. The database
            # is the arbiter, not the read -- so ask it who won.
            winner = (
                await session.execute(
                    select(ApprovalDecision)
                    .where(ApprovalDecision.match_result_id == result.id)
                    .where(ApprovalDecision.decision.in_(TERMINAL_VALUES))
                )
            ).scalar_one_or_none()
            # `winner` is None when this transaction's snapshot predates the
            # commit that beat it. The refusal is still correct -- the index has
            # just proved a terminal decision exists -- and `AlreadyDecidedError`
            # says it cannot name one rather than naming the wrong one.
            raise AlreadyDecidedError(winner) from exc

        await record(
            session,
            actor=Actor(type="human", id=actor_id, label=actor_label),
            action=f"invoice.{request.decision.value}",
            entity_type="approval_decision",
            entity_id=decision.id,
            after={
                "match_result_id": str(result.id),
                "document_id": str(result.document_id),
                "overrode": decision.overrode,
                "match_version": result.version,
            },
            reason=decision.reason,
        )
        log.info(
            "approval.decided",
            decision=request.decision.value,
            actor=actor_label,
            overrode=decision.overrode,
        )
        # Counted against `actor_type`, which is the whole point: this and the
        # system's own decision above land on one metric, so "what share of
        # invoices cleared without a person" is a ratio between two labels of
        # the same counter rather than a comparison between two different ones.
        #
        # Refusals are deliberately not counted here. Every path that raises
        # returns before this line, so the counter means "a decision was
        # recorded" -- a 403 or a 409 is a fact about a request, which the HTTP
        # metrics already carry, not a decision about an invoice.
        record_approval_decision(decision=request.decision.value, actor_type="human")
        await _request_payment(session, decision)
        return decision

    async def open_exceptions(
        self, session: AsyncSession, match_result_id: uuid.UUID
    ) -> list[OpenException]:
        rows = (
            await session.execute(
                select(
                    MatchException.code,
                    MatchException.severity,
                    MatchException.owner_role,
                ).where(MatchException.match_result_id == match_result_id)
            )
        ).all()
        return [
            OpenException(
                code=ExceptionCode(row.code),
                severity=Severity(row.severity),
                owner=OwnerRole(row.owner_role),
            )
            for row in rows
        ]
