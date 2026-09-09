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
from threshold.api.dependencies import get_ingest_service
from threshold.domain.jobs import JobKind, PermanentJobError, TransientJobError
from threshold.domain.models import Document

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
    raw_id = payload.get("document_id")
    if not isinstance(raw_id, str):
        raise PermanentJobError("payload has no document_id")

    try:
        document_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise PermanentJobError(f"document_id is not a uuid: {raw_id!r}") from exc

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
        await service.extract_existing(session, document, content)
    except ExtractionError as exc:
        # The document cannot be read at all. Retrying re-reads the same bytes
        # and fails identically, so this goes straight to the dead-letter queue
        # where a human can look at it.
        raise PermanentJobError(str(exc)) from exc

    log.info("handler.extracted", document_id=str(document_id))


HANDLERS: Final[dict[str, Handler]] = {
    JobKind.EXTRACT_DOCUMENT.value: extract_document,
}
