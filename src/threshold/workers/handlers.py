"""What each job kind actually does.

A handler's contract is narrow on purpose:

* It receives its payload and a session, and it runs inside that session's
  transaction. Committing is the runtime's job, not the handler's, so a handler
  cannot half-finish.
* It raises ``PermanentJobError`` for anything a retry cannot fix, and
  ``TransientJobError`` for anything it might. Everything else that escapes is
  treated as transient, because an unclassified failure is more likely a bug in
  the handler than a poison message — and the attempt budget bounds the cost of
  being wrong.

That classification is the difference between a queue that recovers and one that
grinds: retrying a corrupt PDF three times delays it reaching a human by the
length of the backoff and helps nobody.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.extraction.port import ExtractionError
from threshold.adapters.queue import PostgresJobQueue
from threshold.api.dependencies import get_ingest_service, get_matching_service
from threshold.domain.jobs import JobKind, PermanentJobError, TransientJobError
from threshold.domain.models import Document
from threshold.services.matching import MatchingError

log = structlog.get_logger(__name__)

Handler = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]


async def extract_document(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Re-read a stored document and persist a new extraction version.

    The document already exists — ingest stored it before enqueueing this — so
    the handler fetches the bytes back from object storage rather than carrying
    them through the queue. A payload holding a 20 MB PDF would make the job
    table the document store, and every claim would drag those bytes across the
    wire whether or not the job ran.
    """
    document_id = _document_id_from(payload)

    document = (
        await session.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if document is None:
        # The row is gone. No retry brings it back.
        raise PermanentJobError(f"no such document: {document_id}")

    service = get_ingest_service()
    try:
        content = await service.store.get(document.storage_key)
    except Exception as exc:
        # Storage being unreachable is the archetypal transient failure: the
        # object is almost certainly still there.
        raise TransientJobError(f"could not read {document.storage_key}: {exc}") from exc

    try:
        extraction = await service.extract_existing(session, document, content)
    except ExtractionError as exc:
        # The document cannot be read at all. Retrying re-reads the same bytes
        # and fails identically, so this goes straight to the dead-letter queue
        # where a human can look at it.
        raise PermanentJobError(str(exc)) from exc

    # Enqueued in the same transaction as the extraction it depends on. If the
    # extraction rolls back, so does the request to match it -- which is the
    # whole reason the queue is a table. Chaining through a broker here would
    # mean either matching a reading that no longer exists, or losing the
    # trigger for one that does.
    #
    # The dedupe key is the extraction, not the document: re-extracting a
    # document *should* produce a fresh match, because the thing being judged
    # changed. Keying on the document would suppress exactly that.
    await PostgresJobQueue().enqueue(
        session,
        kind=JobKind.MATCH_INVOICE,
        payload={"document_id": str(document_id)},
        dedupe_key=f"match:{extraction.id}",
    )

    log.info("handler.extracted", document_id=str(document_id), extraction_id=str(extraction.id))


async def match_invoice(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Judge the document's latest extraction against its purchase order.

    Failing to match is not a failure of this job. An invoice with a price
    variance produces a match result with exceptions attached and the handler
    returns normally -- that is the system working. The job only fails when
    there was nothing to judge.
    """
    document_id = _document_id_from(payload)

    try:
        result = await get_matching_service().match_document(session, document_id)
    except MatchingError as exc:
        # No document, or no extraction to judge. Neither is fixed by retrying.
        raise PermanentJobError(str(exc)) from exc

    log.info(
        "handler.matched",
        document_id=str(document_id),
        disposition=result.disposition,
        version=result.version,
    )


def _document_id_from(payload: dict[str, Any]) -> uuid.UUID:
    raw = payload.get("document_id")
    if not isinstance(raw, str):
        raise PermanentJobError("payload has no document_id")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise PermanentJobError(f"document_id is not a uuid: {raw!r}") from exc


HANDLERS: Final[dict[str, Handler]] = {
    JobKind.EXTRACT_DOCUMENT.value: extract_document,
    JobKind.MATCH_INVOICE.value: match_invoice,
}
