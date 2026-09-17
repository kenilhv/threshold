"""The queue a person works, and the decision they record.

The queue is filtered by the same function that authorises the decision. Showing
somebody work they will then be refused is a worse experience than showing them
nothing, and two predicates that are meant to agree eventually will not.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from threshold.api.auth import Principal, require_principal
from threshold.api.dependencies import SessionDep
from threshold.domain.approval import Decision, authorise
from threshold.domain.ledger import to_major
from threshold.domain.models import (
    ApprovalDecision,
    MatchException,
    MatchResult,
    PurchaseOrder,
    Vendor,
)
from threshold.services.approvals import (
    AlreadyDecidedError,
    ApprovalService,
    DecisionRequest,
    NotPermittedError,
    StaleEvidenceError,
    UnknownMatchResultError,
)

router = APIRouter(prefix="/v1", tags=["approvals"])


class QueueItem(BaseModel):
    """One row of somebody's work.

    ``vendor`` and ``amount`` are here because without them every row of a
    queue looks the same. A reviewer scans for who is billing and how much
    before they read anything else, and a list of forty identical rows reading
    "blocked / price variance / in 4h" is a list nobody can triage -- which is
    the failure this queue exists to prevent.

    Both are nullable and for the same reason: an invoice whose vendor did not
    resolve, or whose total could not be read, is precisely the invoice that
    raised an exception. The absence is the story, so it is rendered as an
    absence rather than filled in with a zero or a placeholder.
    """

    match_result_id: str
    document_id: str
    version: int
    #: Who is billing. Null when nothing on the invoice matched a vendor on file.
    vendor: str | None = None
    #: Major units, as a string, formatted once on this side so a client cannot
    #: reintroduce a rounding decision that was already made.
    amount: str | None = None
    currency: str | None = None
    disposition: str
    decided_at: str
    #: Earliest deadline among this invoice's open exceptions. The queue sorts
    #: by it, because an exception with no deadline is a queue that grows.
    due_at: str | None
    exception_codes: list[str]
    owners: list[str]
    #: Whether *this* principal may decide it, and why not when they may not.
    can_decide: bool
    blocked_reason: str | None = None


class DecisionBody(BaseModel):
    decision: Decision
    reason: str = Field(
        min_length=3,
        max_length=2000,
        description=(
            "Why. Required: it is the only part of the record a later reader "
            "cannot reconstruct from the others."
        ),
    )
    seen_version: int = Field(
        description=(
            "The match_result version the reviewer was looking at. If a newer "
            "one exists the decision is refused, because the evidence moved "
            "while it was being read."
        )
    )


class DecisionView(BaseModel):
    id: str
    match_result_id: str
    document_id: str
    decision: str
    actor_label: str | None
    reason: str | None
    overrode: list[str]
    decided_at: str


@router.get(
    "/queue",
    response_model=list[QueueItem],
    summary="Invoices waiting for a person",
)
async def get_queue(
    principal: Annotated[Principal, Depends(require_principal)],
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    mine: Annotated[bool, Query(description="Only invoices this principal may decide.")] = False,
) -> list[QueueItem]:
    """Everything undecided, newest evidence first, soonest deadline first.

    `can_decide` is computed with the same function the decision endpoint uses,
    so the console never renders a button that the API would refuse.
    """
    service = ApprovalService()

    # Latest version per document, with no terminal decision against it.
    latest = (
        select(MatchResult.document_id, MatchResult.version.label("v"))
        .distinct(MatchResult.document_id)
        .order_by(MatchResult.document_id, MatchResult.version.desc())
        .subquery()
    )
    decided = select(ApprovalDecision.match_result_id).where(
        ApprovalDecision.decision.in_(["approved", "rejected"])
    )
    rows = (
        (
            await session.execute(
                select(MatchResult)
                .join(
                    latest,
                    (MatchResult.document_id == latest.c.document_id)
                    & (MatchResult.version == latest.c.v),
                )
                .where(MatchResult.id.not_in(decided))
                .where(MatchResult.disposition != "auto_approve")
                .order_by(MatchResult.decided_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # One query for every vendor on the page rather than one per row. The N+1
    # would be invisible at demo size and obvious at the size this is for.
    order_ids = {r.purchase_order_id for r in rows if r.purchase_order_id is not None}
    vendors: dict[uuid.UUID, str] = {}
    if order_ids:
        found = await session.execute(
            select(PurchaseOrder.id, Vendor.display_name)
            .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
            .where(PurchaseOrder.id.in_(order_ids))
        )
        for order_id, display_name in found.all():
            vendors[order_id] = display_name

    items: list[QueueItem] = []
    for result in rows:
        exceptions = await service.open_exceptions(session, result.id)
        decision = authorise(
            holder=principal.roles, exceptions=exceptions, decision=Decision.APPROVED
        )
        due = (
            await session.execute(
                select(MatchException.due_at)
                .where(MatchException.match_result_id == result.id)
                .where(MatchException.resolved_at.is_(None))
                .order_by(MatchException.due_at)
                .limit(1)
            )
        ).scalar_one_or_none()

        if mine and not decision.permitted:
            continue

        items.append(
            QueueItem(
                match_result_id=str(result.id),
                document_id=str(result.document_id),
                version=result.version,
                vendor=(
                    vendors.get(result.purchase_order_id)
                    if result.purchase_order_id is not None
                    else None
                ),
                amount=(
                    None
                    if result.amount_minor is None or result.currency is None
                    else str(to_major(result.amount_minor, result.currency))
                ),
                currency=result.currency,
                disposition=result.disposition,
                decided_at=result.decided_at.isoformat(),
                due_at=due.isoformat() if due else None,
                exception_codes=sorted({e.code.value for e in exceptions}),
                owners=sorted({e.owner.value for e in exceptions}),
                can_decide=decision.permitted,
                blocked_reason=None if decision.permitted else decision.reason,
            )
        )

    items.sort(key=lambda i: (i.due_at or "9999", i.decided_at))
    return items


@router.post(
    "/match-results/{match_result_id}/decision",
    response_model=DecisionView,
    status_code=status.HTTP_201_CREATED,
    summary="Approve, reject or hold an invoice",
    responses={
        403: {"description": "This principal does not own every open exception."},
        404: {"description": "No such match result."},
        409: {"description": "The evidence moved, or somebody already decided."},
    },
)
async def decide(
    match_result_id: uuid.UUID,
    body: DecisionBody,
    principal: Annotated[Principal, Depends(require_principal)],
    session: SessionDep,
) -> DecisionView:
    try:
        decision = await ApprovalService().decide(
            session,
            DecisionRequest(
                match_result_id=match_result_id,
                decision=body.decision,
                seen_version=body.seen_version,
                reason=body.reason,
            ),
            actor_id=principal.subject,
            actor_label=principal.username,
            roles=principal.roles,
        )
    except UnknownMatchResultError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except StaleEvidenceError as exc:
        # 409 with the numbers, not a bare "conflict". The right next action is
        # to re-read the invoice, and the response should say so.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"detail": str(exc), "seen_version": exc.seen, "current_version": exc.current},
        ) from exc
    except AlreadyDecidedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "detail": str(exc),
                "decision": exc.existing.decision,
                "actor": exc.existing.actor_label,
            },
        ) from exc
    except NotPermittedError as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {
                "detail": str(exc),
                "required_roles": sorted(r.value for r in exc.authorisation.required_roles),
            },
        ) from exc

    return DecisionView(
        id=str(decision.id),
        match_result_id=str(decision.match_result_id),
        document_id=str(decision.document_id),
        decision=decision.decision,
        actor_label=decision.actor_label,
        reason=decision.reason,
        overrode=list(decision.overrode),
        decided_at=decision.decided_at.isoformat(),
    )


@router.get(
    "/documents/{document_id}/decisions",
    response_model=list[DecisionView],
    summary="Every decision recorded about a document, machine and human alike",
)
async def get_decisions(
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: SessionDep,
) -> list[DecisionView]:
    """Append-only, so this is the whole history and not the current state."""
    rows = (
        (
            await session.execute(
                select(ApprovalDecision)
                .where(ApprovalDecision.document_id == document_id)
                .order_by(ApprovalDecision.decided_at)
            )
        )
        .scalars()
        .all()
    )
    return [
        DecisionView(
            id=str(row.id),
            match_result_id=str(row.match_result_id),
            document_id=str(row.document_id),
            decision=row.decision,
            actor_label=row.actor_label or row.actor_type,
            reason=row.reason,
            overrode=list(row.overrode),
            decided_at=row.decided_at.isoformat(),
        )
        for row in rows
    ]
