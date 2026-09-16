"""Document ingestion.

``POST /v1/documents`` is the front door. It is idempotent at two independent
layers, which answer different questions:

* The ``Idempotency-Key`` header answers *"is this the same request?"* — a
  client retrying after a timeout gets its original response back rather than a
  second document.
* The content hash answers *"are these the same bytes?"* — a supplier resending
  the same PDF under a different filename, or an inbox polled twice, resolves to
  one document with no key involved.

Either alone leaves a hole. A retry with a fresh key would create a duplicate
without the hash; two genuinely different documents submitted with the same key
would silently collide without the fingerprint.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Header, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from threshold.adapters.extraction.port import ExtractionError
from threshold.api import idempotency
from threshold.api.dependencies import SessionDep, get_ingest_service
from threshold.domain.classification import Sensitivity
from threshold.domain.extraction import redacted_fields
from threshold.domain.models import (
    Document,
    Escalation,
    Extraction,
    MatchException,
    MatchResult,
    PurchaseOrder,
    Vendor,
)
from threshold.domain.projection import project
from threshold.services.ingest import (
    DocumentTooLargeError,
    IngestService,
    UnsupportedDocumentTypeError,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/documents", tags=["documents"])


class IngestResponse(BaseModel):
    """Accepted, not processed.

    Deliberately reports nothing about extraction: the request stores the
    document and enqueues the work, and a response carrying a confidence score
    would be claiming knowledge the system does not have yet. Poll
    GET /v1/documents/{id} for the result.
    """

    document_id: str
    content_sha256: str
    created: bool = Field(
        description="False when these exact bytes were already known to the system."
    )
    job_id: str | None = Field(
        description=(
            "The enqueued extraction job. None when a job for this document was "
            "already queued and the dedupe key suppressed a second one."
        )
    )


class FieldView(BaseModel):
    value: object | None
    confidence: float
    raw_text: str | None = None


class DocumentDetail(BaseModel):
    document_id: str
    content_sha256: str
    original_filename: str
    content_type: str
    byte_size: int
    source: str
    received_at: str
    extraction_version: int | None
    extractor_id: str | None
    lowest_decision_confidence: str | None
    fields: dict[str, object] | None


class ExceptionView(BaseModel):
    code: str
    severity: str
    owner_role: str
    detail: str
    due_at: str
    invoice_line_number: int | None = None
    purchase_order_line_number: int | None = None
    #: Expected, actual, variance and which threshold it tripped. Present only
    #: on exceptions that came from a tolerance comparison.
    breach: dict[str, object] | None = None


class VendorView(BaseModel):
    """Who is billing, as much of it as a reviewer may see.

    Built through the review projection rather than field by field, so the tax
    identifier is dropped because it is declared RESTRICTED — not because
    somebody remembered to leave it out here.
    """

    vendor_number: str
    display_name: str
    country_code: str
    status: str


class EscalationView(BaseModel):
    """What the triage agent said, marked as an opinion rather than a finding.

    Every field here is advisory. `owner_role` does not override the ownership
    on the exceptions -- those are authoritative and are already in
    ``exceptions`` above with their own deadlines. This says which of the desks
    already involved the agent thinks should look first.

    ``uncited_codes`` is the part a reviewer needs and would not think to ask
    for: the exceptions the agent did *not* mention. An agent that stays quiet
    about a payment redirect while escalating a price query produces a packet
    that reads as a complete account of the invoice, and this is what makes that
    visible without reading the audit trail.

    ``summary`` is free text written by a model whose input included a document
    the supplier controls. The console renders it attributed and never as
    system prose -- see `domain/triage.py`.
    """

    owner_role: str
    codes: list[str]
    uncited_codes: list[str]
    summary: str
    #: The tier that produced it: a ruleset version or a model version.
    triager_id: str
    created_at: str


class MatchView(BaseModel):
    """What the matcher concluded, and everything needed to argue with it.

    ``skipped_checks`` is here rather than in a log because a reviewer looking
    at a clean result has to be able to tell whether every check ran. An absent
    check and a passing check look identical otherwise, and the difference is
    the whole value of the answer.
    """

    #: The decision endpoint acts on this, not on the document: a decision is
    #: about one body of evidence, and a new version is different evidence.
    match_result_id: str
    document_id: str
    version: int
    disposition: str
    policy_version: str
    decided_at: str
    line_pairs: list[dict[str, int]]
    skipped_checks: list[str]
    exceptions: list[ExceptionView]
    #: None when the invoice's vendor did not resolve, which is itself an
    #: exception on the list above.
    vendor: VendorView | None = None
    #: None when no agent ran, or when it had nothing to add -- which is the
    #: common case, because the deterministic tier only speaks when more than
    #: one desk is involved. Its absence is not a degraded answer: the
    #: exceptions above carry their own owners and deadlines either way.
    escalation: EscalationView | None = None


@router.post(
    "",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a document for extraction",
    responses={
        200: {"description": "These bytes were already ingested; nothing was created."},
        202: {"description": "Stored and queued. Poll GET /v1/documents/{id} for the result."},
        409: {"description": "The Idempotency-Key was reused for a different request."},
        413: {"description": "Document exceeds the size limit."},
        415: {"description": "Unsupported content type."},
        422: {"description": "The document could not be read at all."},
    },
)
async def submit_document(
    response: Response,
    file: Annotated[UploadFile, File(description="The invoice document.")],
    session: SessionDep,
    service: Annotated[IngestService, Depends(get_ingest_service)],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "Optional. Retrying with the same key replays the original response "
                "instead of ingesting twice. Reusing a key for different content is a 409."
            ),
        ),
    ] = None,
) -> IngestResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")

    if idempotency_key is not None:
        if len(idempotency_key) > idempotency.MAX_KEY_LENGTH:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Idempotency-Key exceeds {idempotency.MAX_KEY_LENGTH} characters",
            )
        fingerprint = idempotency.fingerprint_request(
            "POST", "/v1/documents", hashlib.sha256(content).hexdigest()
        )
        try:
            replayed = await idempotency.find_replayable(session, idempotency_key, fingerprint)
        except idempotency.IdempotencyConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if replayed is not None:
            stored_status, stored_body = replayed
            response.status_code = stored_status
            return IngestResponse.model_validate(stored_body)

    try:
        outcome = await service.ingest(
            session,
            content=content,
            filename=file.filename or "upload.pdf",
            content_type=file.content_type or "application/octet-stream",
        )
    except DocumentTooLargeError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    except UnsupportedDocumentTypeError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    except ExtractionError as exc:
        # Distinct from low confidence. Low confidence is a routable business
        # outcome; this means there was nothing to route.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    body = IngestResponse(
        document_id=outcome.document_id,
        content_sha256=outcome.content_sha256,
        created=outcome.created,
        job_id=outcome.job_id,
    )

    # 202 when work was queued, because processing has not finished and the
    # caller must poll. 200 for a duplicate: nothing was created and nothing
    # new was queued.
    status_code = status.HTTP_202_ACCEPTED if outcome.created else status.HTTP_200_OK
    response.status_code = status_code

    if idempotency_key is not None:
        await idempotency.remember(
            session, idempotency_key, fingerprint, status_code, body.model_dump()
        )

    return body


@router.get(
    "/{document_id}",
    response_model=DocumentDetail,
    summary="Fetch a document and its most recent extraction",
)
async def get_document(
    document_id: uuid.UUID,
    session: SessionDep,
) -> DocumentDetail:
    document = (
        await session.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such document")

    extraction = (
        await session.execute(
            select(Extraction)
            .where(Extraction.document_id == document_id)
            .order_by(Extraction.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return DocumentDetail(
        document_id=str(document.id),
        content_sha256=document.content_sha256,
        original_filename=document.original_filename,
        content_type=document.content_type,
        byte_size=document.byte_size,
        source=document.source,
        received_at=document.received_at.isoformat(),
        extraction_version=extraction.version if extraction else None,
        extractor_id=extraction.extractor_id if extraction else None,
        lowest_decision_confidence=(
            str(extraction.lowest_decision_confidence) if extraction else None
        ),
        # Payment details are dropped, not masked. Four digits is a short
        # enough candidate list that masking them protects nothing.
        fields=redacted_fields(extraction.fields) if extraction else None,
    )


@router.get(
    "/{document_id}/match",
    response_model=MatchView,
    summary="Fetch the most recent matching decision for a document",
    responses={
        404: {"description": "No such document, or it has not been matched yet."},
    },
)
async def get_match(
    document_id: uuid.UUID,
    session: SessionDep,
) -> MatchView:
    """The latest version only.

    Earlier versions are kept and are the audit trail, but a caller asking "can
    this be paid" wants the current answer. Serving a history by default would
    make the common question the awkward one.
    """
    result = (
        await session.execute(
            select(MatchResult)
            .where(MatchResult.document_id == document_id)
            .order_by(MatchResult.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no matching decision for this document yet",
        )

    exceptions = (
        (
            await session.execute(
                select(MatchException)
                .where(MatchException.match_result_id == result.id)
                .order_by(MatchException.due_at, MatchException.code)
            )
        )
        .scalars()
        .all()
    )

    vendor_view: VendorView | None = None
    if result.purchase_order_id is not None:
        vendor = (
            await session.execute(
                select(Vendor)
                .join(PurchaseOrder, PurchaseOrder.vendor_id == Vendor.id)
                .where(PurchaseOrder.id == result.purchase_order_id)
            )
        ).scalar_one_or_none()
        if vendor is not None:
            # Everything a reviewer may see, decided by the column declarations
            # rather than by this function.
            visible = project(vendor, level=Sensitivity.INTERNAL)
            vendor_view = VendorView(
                vendor_number=visible["vendor_number"],
                display_name=visible["display_name"],
                country_code=visible["country_code"],
                status=visible["status"],
            )

    escalation = (
        await session.execute(select(Escalation).where(Escalation.match_result_id == result.id))
    ).scalar_one_or_none()
    escalation_view: EscalationView | None = None
    if escalation is not None:
        cited = set(escalation.codes)
        escalation_view = EscalationView(
            owner_role=escalation.owner_role,
            codes=list(escalation.codes),
            # Computed here rather than stored, because it is a fact about two
            # tables and storing it would give it a third chance to disagree.
            uncited_codes=sorted({item.code for item in exceptions} - cited),
            summary=escalation.summary,
            triager_id=escalation.triager_id,
            created_at=escalation.created_at.isoformat(),
        )

    return MatchView(
        match_result_id=str(result.id),
        document_id=str(result.document_id),
        version=result.version,
        disposition=result.disposition,
        policy_version=result.policy_version,
        decided_at=result.decided_at.isoformat(),
        line_pairs=list(result.line_pairs),
        skipped_checks=list(result.skipped_checks),
        vendor=vendor_view,
        escalation=escalation_view,
        exceptions=[
            ExceptionView(
                code=item.code,
                severity=item.severity,
                owner_role=item.owner_role,
                detail=item.detail,
                due_at=item.due_at.isoformat(),
                invoice_line_number=item.invoice_line_number,
                purchase_order_line_number=item.purchase_order_line_number,
                breach=item.breach,
            )
            for item in exceptions
        ],
    )
