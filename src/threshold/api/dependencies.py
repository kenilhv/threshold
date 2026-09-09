"""Dependency wiring.

The concrete adapters are chosen here and nowhere else. Everything downstream
depends on the port, which is what lets the whole ingest path run in a unit test
against an in-memory store and a hermetic extractor.
"""

from __future__ import annotations

from functools import lru_cache

from threshold.adapters.extraction.deterministic import DeterministicExtractor
from threshold.adapters.extraction.port import Extractor
from threshold.adapters.object_store import ObjectStore, S3ObjectStore
from threshold.config.settings import get_settings
from threshold.services.ingest import IngestService


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
def get_ingest_service() -> IngestService:
    return IngestService(store=get_object_store(), extractor=get_extractor())
