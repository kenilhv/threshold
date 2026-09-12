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
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.extraction.port import Extractor
from threshold.adapters.object_store import ObjectStore, storage_key_for
from threshold.adapters.queue import Queue
from threshold.domain.jobs import JobKind
from threshold.domain.models import Document, Extraction
from threshold.services.audit import Actor, record

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
    """What happened, in enough detail for the caller to be honest about it.

    Deliberately says nothing about extraction. The request stores the document
    and enqueues the work; extraction happens on a worker moments later. A
    response that reported a confidence score would be claiming knowledge the
    system does not have yet.
    """

    document_id: str
    content_sha256: str
    #: False when the bytes were already known. The caller returns 200 rather
    #: than 201, because nothing was created.
    created: bool
    #: None when a live job already covers this document — the dedupe key
    #: suppressed a second enqueue.
    job_id: str | None


class IngestService:
    def __init__(self, store: ObjectStore, extractor: Extractor, queue: Queue) -> None:
        self._store = store
        self._extractor = extractor
        self._queue = queue

    @property
    def store(self) -> ObjectStore:
        """Exposed so the extraction job can fetch the bytes back.

        The queue payload carries a document id, never the document. A payload
        holding a 20 MB PDF would make the job table the document store and drag
        those bytes across the wire on every claim.
        """
        return self._store

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
            # error, and it must not create a second document — nor a second
            # extraction job, which the dedupe key below suppresses.
            log.info("ingest.duplicate_content", document_id=str(existing.id), sha256=digest)
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

            await record(
                session,
                actor=Actor.system(),
                action="document.received",
                entity_type="document",
                entity_id=document.id,
                after={
                    "content_sha256": digest,
                    "byte_size": len(content),
                    "source": source,
                },
            )
            created = True
            log.info("ingest.stored", document_id=str(document.id), bytes=len(content))

        # Nothing to do for bytes that already have an extraction.
        #
        # The dedupe key alone does not cover this: the partial unique index
        # only holds while a job is pending or claimed, so once the first
        # extraction succeeds the key frees and a re-submission would enqueue a
        # second identical run. Re-extracting the same bytes with the same
        # extractor produces the same answer at version 2 — pure waste.
        #
        # Deliberately NOT a guard against re-extraction in general: that is a
        # real operation, and it is how a document gets replayed against a new
        # model in M6. It just should not happen by accident on an upload.
        already_extracted = await self._latest_extraction(session, document) is not None

        job_id: uuid.UUID | None = None
        if not already_extracted:
            # Enqueued in the SAME transaction as the document row. That is the
            # entire reason the queue is a table: there is no window in which
            # the document exists and the work to process it does not.
            job_id = await self._queue.enqueue(
                session,
                kind=JobKind.EXTRACT_DOCUMENT,
                payload={"document_id": str(document.id)},
                dedupe_key=f"extract:{digest}",
            )

        return IngestOutcome(
            document_id=str(document.id),
            content_sha256=digest,
            created=created,
            job_id=str(job_id) if job_id else None,
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

    async def extract_existing(
        self,
        session: AsyncSession,
        document: Document,
        content: bytes,
    ) -> Extraction:
        """Read a stored document and persist the next extraction version.

        Called from the worker, never from a request handler. Extraction takes
        tens of milliseconds on the deterministic tier and is budgeted at ninety
        seconds on the model tier, and a ninety-second HTTP request is the
        clearest possible sign that a system has never been operated.
        """
        return await self._extract_and_store(session, document, content, document.content_type)

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

        await record(
            session,
            actor=Actor.system(),
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

        log.info(
            "ingest.extracted",
            document_id=str(document.id),
            version=next_version,
            confidence=float(confidence),
            duration_ms=result.duration_ms,
        )
        return extraction
