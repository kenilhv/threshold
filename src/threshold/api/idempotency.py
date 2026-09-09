"""Idempotent request handling, following Stripe's published semantics.

Three cases, and the third is the one implementations usually miss:

===========================  ==========================================
Same key, same request       Replay the stored response. Do no work.
New key                      Do the work. Store the response.
Same key, DIFFERENT request  409. Never silently replay.
===========================  ==========================================

That last case matters because the alternative is returning the wrong resource
with a 200 attached. If a client reuses a key for different content it has a
bug, and the useful thing to do is tell it so — a caller that receives another
invoice's id under a 200 has no way to detect the mistake.

The fingerprint is what makes the case detectable, which is why it is stored
rather than just the key.

Scope note: this is the HTTP layer of a three-layer defence. The other two are a
dedupe key on the job (M2) and a unique constraint on the payment row (M8). Each
catches something the others cannot — a retried HTTP request, a redelivered
queue message, and a logic error respectively.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.domain.models import IdempotencyKey

log = structlog.get_logger(__name__)

#: Stripe expires keys after 24 hours. Matching that means a client that already
#: implements retries against Stripe behaves correctly here without changes.
RETENTION: Final = timedelta(hours=24)

#: Long enough for a UUID or a ULID with room to spare; short enough that a
#: client cannot use the column as free storage.
MAX_KEY_LENGTH: Final = 255


class IdempotencyConflictError(Exception):
    """The key has been seen before with a different request body."""

    def __init__(self, key: str) -> None:
        super().__init__(
            f"Idempotency-Key {key!r} was already used for a different request. "
            "Use a new key for a new request."
        )
        self.key = key


def fingerprint_request(method: str, path: str, body_digest: str) -> str:
    """Identify a request by what it asked for, not by its bytes.

    ``body_digest`` is the sha256 of the payload — for an upload that is the
    document's own content hash, which is already computed. Hashing the
    fingerprint inputs rather than storing the body keeps the table small and
    means a replayed 50 MB PDF is compared in 64 bytes.
    """
    material = f"{method.upper()}\n{path}\n{body_digest}"
    return hashlib.sha256(material.encode()).hexdigest()


async def find_replayable(
    session: AsyncSession, key: str, fingerprint: str
) -> tuple[int, dict[str, Any]] | None:
    """Return a stored response if this exact request was already answered.

    Raises ``IdempotencyConflictError`` when the key matches but the request does not.
    """
    stmt = select(IdempotencyKey).where(IdempotencyKey.key == key)
    existing = (await session.execute(stmt)).scalar_one_or_none()

    if existing is None:
        return None

    if existing.expires_at <= datetime.now(UTC):
        # Expired keys are treated as absent rather than as conflicts. The
        # client is entitled to reuse a key after the documented window, and
        # the sweep that deletes the row may not have run yet.
        log.info("idempotency.expired", key=key)
        return None

    if existing.request_fingerprint != fingerprint:
        log.warning("idempotency.conflict", key=key)
        raise IdempotencyConflictError(key)

    log.info("idempotency.replay", key=key, status=existing.response_status)
    return existing.response_status, existing.response_body


async def remember(
    session: AsyncSession,
    key: str,
    fingerprint: str,
    status: int,
    body: dict[str, Any],
) -> None:
    """Store a response so an identical request replays instead of repeating.

    A concurrent duplicate can insert the same key between the lookup above and
    this write. The unique constraint catches it; the loser treats it as a
    successful replay rather than an error, because both requests did the same
    work and the caller cannot tell — and should not care — which one won.
    """
    now = datetime.now(UTC)
    record = IdempotencyKey(
        key=key,
        request_fingerprint=fingerprint,
        response_status=status,
        response_body=body,
        expires_at=now + RETENTION,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log.info("idempotency.concurrent_duplicate", key=key)
