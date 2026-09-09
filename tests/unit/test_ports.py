"""The adapters still satisfy the ports the application depends on.

These assertions are mostly for the type checker. Annotating a concrete adapter
as its protocol makes ``mypy`` verify the full signature — parameter names,
keyword-only markers, defaults and return types — so widening or renaming an
argument on an adapter fails the build here instead of at the call site of
whichever consumer happened to be checked first.

The ``isinstance`` calls are the weaker runtime half: ``runtime_checkable``
protocols only confirm the attributes exist. Both are cheap; neither needs a
container.
"""

from __future__ import annotations

from threshold.adapters.extraction.deterministic import DeterministicExtractor
from threshold.adapters.extraction.port import Extractor
from threshold.adapters.object_store import ObjectStore, S3ObjectStore
from threshold.adapters.queue import PostgresJobQueue, Queue


def test_the_postgres_queue_satisfies_the_queue_port() -> None:
    queue: Queue = PostgresJobQueue()
    assert isinstance(queue, Queue)


def test_the_queue_port_is_narrower_than_the_adapter() -> None:
    """Operational verbs are deliberately absent from the port.

    Reap, replay, purge and stats belong to an operator running ``queuectl``,
    not to a request handler. If one of them ever appears on the protocol, a
    service could purge the queue while answering an HTTP request.
    """
    for operational in ("reap_expired_leases", "replay_dead_letter", "purge_finished", "stats"):
        assert hasattr(PostgresJobQueue, operational)
        assert not hasattr(Queue, operational), f"{operational} must not be on the port"


def test_the_s3_object_store_satisfies_the_object_store_port() -> None:
    assert isinstance(S3ObjectStore.__new__(S3ObjectStore), ObjectStore)


def test_the_deterministic_extractor_satisfies_the_extractor_port() -> None:
    extractor: Extractor = DeterministicExtractor()
    assert isinstance(extractor, Extractor)
