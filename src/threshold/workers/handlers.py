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
from threshold.domain.matching import Disposition
from threshold.domain.models import Document
from threshold.services.matching import MatchingError
from threshold.services.payments import PaymentRefusedError, PaymentService
from threshold.services.triage import TriageService

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

    if result.disposition != Disposition.AUTO_APPROVE.value:
        # Enqueued in the same transaction as the decision, for the same reason
        # matching is enqueued with the extraction: if the decision rolls back
        # there is nothing to triage, and a broker here would eventually ask an
        # agent to explain a match result that does not exist.
        #
        # Keyed on the match result rather than the document, because a document
        # re-decided after a correction is a new thing to have an opinion about.
        await PostgresJobQueue().enqueue(
            session,
            kind=JobKind.TRIAGE_INVOICE,
            payload={"match_result_id": str(result.id)},
            dedupe_key=f"triage:{result.id}",
        )

    log.info(
        "handler.matched",
        document_id=str(document_id),
        disposition=result.disposition,
        version=result.version,
    )


async def triage_invoice(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Ask the triage agent which desk should look at this invoice first.

    Nothing here is load-bearing for the invoice. The exceptions were routed and
    clocked when they were raised, so a failure -- a model tier down, a packet
    that would not validate -- costs a reviewer some context and costs the
    invoice nothing. That is why it is a separate job: a job that can fail
    without consequence must not share a transaction with one that cannot.
    """
    match_result_id = _uuid_from(payload, "match_result_id")

    escalation = await TriageService().triage(session, match_result_id)
    if escalation is None:
        log.info("handler.triaged_without_opinion", match_result_id=str(match_result_id))
        return

    log.info(
        "handler.triaged",
        match_result_id=str(match_result_id),
        owner_role=escalation.owner_role,
        codes=escalation.codes,
        triager_id=escalation.triager_id,
    )


async def execute_payment(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Advance one payment by one state, then ask to be run again if more remains.

    One transition per run is not a simplification -- it is the mechanism. The
    payment has to be committed as `submitting` before the rail is called, and a
    handler owns exactly one transaction, so the commit between those two things
    is the queue's to provide. `services/payments.py` argues it at length.

    The re-enqueue goes in the same transaction as the transition, so a run that
    rolls back also un-asks for its successor. That is the same property the
    extraction handler relies on when it enqueues the match.
    """
    approval_decision_id = _uuid_from(payload, "approval_decision_id")

    try:
        payment, more = await PaymentService().advance(session, approval_decision_id)
    except PaymentRefusedError as refusal:
        # A missing fact: no amount was read, no payment details are on file.
        # Re-reading the same rows produces the same absence, so this goes to a
        # person rather than round the retry loop three times first.
        raise PermanentJobError(str(refusal)) from refusal

    if more:
        await PostgresJobQueue().enqueue(
            session,
            kind=JobKind.EXECUTE_PAYMENT,
            payload={"approval_decision_id": str(approval_decision_id)},
            # The state is in the key because the next run is a different piece
            # of work. Without it the key would collide with the job currently
            # running and the payment would stall one step from the end.
            dedupe_key=f"pay:{payment.id}:{payment.state}",
        )

    log.info(
        "handler.payment_advanced",
        payment_id=str(payment.id),
        state=payment.state,
        more=more,
        rail_attempts=payment.rail_attempts,
    )


def _document_id_from(payload: dict[str, Any]) -> uuid.UUID:
    return _uuid_from(payload, "document_id")


def _uuid_from(payload: dict[str, Any], key: str) -> uuid.UUID:
    raw = payload.get(key)
    if not isinstance(raw, str):
        raise PermanentJobError(f"payload has no {key}")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise PermanentJobError(f"{key} is not a uuid: {raw!r}") from exc


HANDLERS: Final[dict[str, Handler]] = {
    JobKind.EXTRACT_DOCUMENT.value: extract_document,
    JobKind.MATCH_INVOICE.value: match_invoice,
    JobKind.TRIAGE_INVOICE.value: triage_invoice,
    JobKind.EXECUTE_PAYMENT.value: execute_payment,
}
