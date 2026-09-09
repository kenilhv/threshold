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
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.db import get_session
from threshold.adapters.extraction.port import ExtractionError
from threshold.api import idempotency
from threshold.api.dependencies import get_ingest_service
from threshold.domain.models import Document, Extraction
from threshold.services.ingest import (
    DocumentTooLargeError,
    IngestService,
    UnsupportedDocumentTypeError,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/documents", tags=["documents"])


class IngestResponse(BaseModel):
    document_id: str
    content_sha256: str
    created: bool = Field(
        description="False when these exact bytes were already known to the system."
    )
    extraction_id: str
    extraction_version: int
    lowest_decision_confidence: str = Field(
        description=(
            "Minimum confidence across the fields a payment decision depends on. "
            "A string, because it is a Decimal and JSON floats lose precision."
        )
    )
    meets_confidence_gate: bool = Field(
        description="Whether every decision-relevant field cleared the provisional threshold."
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


@router.post(
    "",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a document for extraction",
    responses={
        200: {"description": "These bytes were already ingested; nothing was created."},
        409: {"description": "The Idempotency-Key was reused for a different request."},
        413: {"description": "Document exceeds the size limit."},
        415: {"description": "Unsupported content type."},
        422: {"description": "The document could not be read at all."},
    },
)
async def submit_document(
    response: Response,
    file: Annotated[UploadFile, File(description="The invoice document.")],
    session: Annotated[AsyncSession, Depends(get_session)],
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
        extraction_id=outcome.extraction_id,
        extraction_version=outcome.extraction_version,
        lowest_decision_confidence=str(outcome.lowest_decision_confidence),
        meets_confidence_gate=outcome.meets_confidence_gate,
    )

    # 201 only when something was actually created. A duplicate is a successful
    # 200 — the caller's intent is satisfied and nothing new exists.
    status_code = status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
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
    session: Annotated[AsyncSession, Depends(get_session)],
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
        fields=extraction.fields if extraction else None,
    )
