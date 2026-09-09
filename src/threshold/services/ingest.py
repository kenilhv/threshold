"""Taking a document in, once.

The orchestration layer: it owns the sequence and the transaction boundary, and
it depends on ports rather than on concrete adapters, so the whole path runs in
a unit test against an in-memory store with no container.

The ordering here is deliberate and worth stating, because getting it wrong is
how documents go missing:

1. Hash first. The content hash is the identity, and it is computed before
   anything is written anywhere.
2. Object store before database. If the write to storage fails, no row exists
   pointing at a missing object. The reverse order leaves a document row whose
   bytes were never stored — a dangling reference that surfaces later as a
   mysterious 404 in the reviewer's console.
3. Audit event in the same transaction as the row it describes. Never after.

An orphaned object with no row is the acceptable failure of this ordering: it
wastes storage and is trivially reclaimable by a sweep, which is a far better
outcome than a row that promises bytes nobody has.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.extraction.port import Extractor
from threshold.adapters.object_store import ObjectStore, storage_key_for
from threshold.domain.models import AuditEvent, Document, Extraction

log = structlog.get_logger(__name__)

#: Above this, a "document" is almost certainly not an invoice. A bounded read
#: is also the cheapest defence against a memory-exhaustion upload.
MAX_DOCUMENT_BYTES: Final = 25 * 1024 * 1024

_EXTENSION_BY_TYPE: Final = {
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/tiff": "tif",
}


class DocumentTooLargeError(ValueError):
    def __init__(self, size: int) -> None:
        super().__init__(f"document is {size} bytes; the limit is {MAX_DOCUMENT_BYTES}")
        self.size = size


class UnsupportedDocumentTypeError(ValueError):
    def __init__(self, content_type: str) -> None:
        super().__init__(f"unsupported content type: {content_type}")
        self.content_type = content_type


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What happened, in enough detail for the caller to be honest about it."""

    document_id: str
    content_sha256: str
    #: False when the bytes were already known. The caller returns 200 rather
    #: than 201, because nothing was created.
    created: bool
    extraction_id: str
    extraction_version: int
    lowest_decision_confidence: Decimal
    #: Whether every decision-relevant field cleared the confidence gate. In M3
    #: this becomes the input to auto-approval; for now it is reported so the
    #: behaviour is visible before anything depends on it.
    meets_confidence_gate: bool


#: Derived from a calibration curve in M6. Stated as a constant now, and
#: deliberately named so it is obvious this is a placeholder rather than a
#: measured operating point.
PROVISIONAL_CONFIDENCE_GATE: Final = Decimal("0.80")


class IngestService:
    def __init__(self, store: ObjectStore, extractor: Extractor) -> None:
        self._store = store
        self._extractor = extractor

    async def ingest(
        self,
        session: AsyncSession,
        *,
        content: bytes,
        filename: str,
        content_type: str,
        source: str = "upload",
    ) -> IngestOutcome:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise DocumentTooLargeError(len(content))
        if content_type not in _EXTENSION_BY_TYPE:
            raise UnsupportedDocumentTypeError(content_type)

        digest = hashlib.sha256(content).hexdigest()

        existing = (
            await session.execute(select(Document).where(Document.content_sha256 == digest))
        ).scalar_one_or_none()

        if existing is not None:
            # The same bytes arriving twice is the most common real duplicate:
            # a supplier resending a PDF, or an inbox polled twice. It is not an
            # error, and it must not create a second document.
            log.info("ingest.duplicate_content", document_id=str(existing.id), sha256=digest)
            extraction = await self._latest_extraction(session, existing)
            if extraction is not None:
                return IngestOutcome(
                    document_id=str(existing.id),
                    content_sha256=digest,
                    created=False,
                    extraction_id=str(extraction.id),
                    extraction_version=extraction.version,
                    lowest_decision_confidence=extraction.lowest_decision_confidence,
                    meets_confidence_gate=(
                        extraction.lowest_decision_confidence >= PROVISIONAL_CONFIDENCE_GATE
                    ),
                )
            document = existing
            created = False
        else:
            key = storage_key_for(digest, _EXTENSION_BY_TYPE[content_type])
            # Storage before the row. See the module docstring on why this
            # ordering is the safe one.
            await self._store.put(key, content, content_type)

            document = Document(
                content_sha256=digest,
                storage_key=key,
                original_filename=filename[:400],
                content_type=content_type,
                byte_size=len(content),
                source=source,
            )
            session.add(document)
            await session.flush()

            session.add(
                AuditEvent(
                    actor_type="system",
                    action="document.received",
                    entity_type="document",
                    entity_id=document.id,
                    after={
                        "content_sha256": digest,
                        "byte_size": len(content),
                        "source": source,
                    },
                )
            )
            created = True
            log.info("ingest.stored", document_id=str(document.id), bytes=len(content))

        extraction = await self._extract_and_store(session, document, content, content_type)

        return IngestOutcome(
            document_id=str(document.id),
            content_sha256=digest,
            created=created,
            extraction_id=str(extraction.id),
            extraction_version=extraction.version,
            lowest_decision_confidence=extraction.lowest_decision_confidence,
            meets_confidence_gate=(
                extraction.lowest_decision_confidence >= PROVISIONAL_CONFIDENCE_GATE
            ),
        )

    async def _latest_extraction(
        self, session: AsyncSession, document: Document
    ) -> Extraction | None:
        stmt = (
            select(Extraction)
            .where(Extraction.document_id == document.id)
            .order_by(Extraction.version.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _extract_and_store(
        self,
        session: AsyncSession,
        document: Document,
        content: bytes,
        content_type: str,
    ) -> Extraction:
        result = await self._extractor.extract(content, content_type)

        # Next version for this document. Re-extracting never mutates an
        # existing row — that immutability is what makes replaying a document
        # against a new model and diffing the two results possible.
        next_version = (
            await session.execute(
                select(func.coalesce(func.max(Extraction.version), 0) + 1).where(
                    Extraction.document_id == document.id
                )
            )
        ).scalar_one()

        confidence = Decimal(str(round(result.invoice.lowest_decision_confidence(), 4)))

        extraction = Extraction(
            document_id=document.id,
            version=next_version,
            extractor_kind=result.extractor_kind.value,
            extractor_id=result.extractor_id,
            prompt_version=result.prompt_version,
            fields=result.invoice.model_dump(mode="json"),
            lowest_decision_confidence=confidence,
            duration_ms=result.duration_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        session.add(extraction)
        await session.flush()

        session.add(
            AuditEvent(
                actor_type="system",
                action="document.extracted",
                entity_type="extraction",
                entity_id=extraction.id,
                after={
                    "document_id": str(document.id),
                    "version": next_version,
                    "extractor_id": result.extractor_id,
                    "lowest_decision_confidence": str(confidence),
                },
            )
        )

        log.info(
            "ingest.extracted",
            document_id=str(document.id),
            version=next_version,
            confidence=float(confidence),
            duration_ms=result.duration_ms,
        )
        return extraction
