"""Dependency wiring.

The concrete adapters are chosen here and nowhere else. Everything downstream
depends on the port, which is what lets the whole ingest path run in a unit test
against an in-memory store and a hermetic extractor.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.db import get_session
from threshold.adapters.extraction.deterministic import DeterministicExtractor
from threshold.adapters.extraction.port import Extractor
from threshold.adapters.object_store import ObjectStore, S3ObjectStore
from threshold.adapters.queue import PostgresJobQueue, Queue
from threshold.config.settings import get_settings
from threshold.services.ingest import IngestService
from threshold.services.matching import MatchingService


@lru_cache
def get_object_store() -> ObjectStore:
    return S3ObjectStore(get_settings())


@lru_cache
def get_extractor() -> Extractor:
    # M7 swaps this for a model-backed tier with the deterministic one behind it
    # as a fallback. The interface is why that is a one-line change here rather
    # than a change in the service.
    return DeterministicExtractor()


@lru_cache
def get_queue() -> Queue:
    return PostgresJobQueue()


@lru_cache
def get_ingest_service() -> IngestService:
    return IngestService(store=get_object_store(), extractor=get_extractor(), queue=get_queue())


def get_matching_service() -> MatchingService:
    return MatchingService()


#: The request's database session. Use this, never ``Depends(get_session)``.
#:
#: ``scope="function"`` is load-bearing. A ``yield`` dependency defaults to
#: request scope, which runs its exit code -- here, the COMMIT -- after the
#: response has already been sent. Measured against this API before the fix:
#: 6 of 8 approvals returned 201 for a row that a read issued immediately
#: afterwards could not see. Worse than a stale read, it meant a commit that
#: failed at commit time (a constraint, a dropped connection) still left the
#: client holding a 201 for a decision that was never recorded.
#:
#: Function scope commits when the route returns and before the response is
#: written, so a failed commit becomes a 500 and a success is readable the
#: instant the client has it. ``test_session_scope.py`` walks every route and
#: fails if any of them takes a session any other way.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
