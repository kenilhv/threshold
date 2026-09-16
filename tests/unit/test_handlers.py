"""The handler registry is total.

A job kind with nothing to run it is enqueued successfully, claimed
successfully, and then fails on every attempt until it reaches the dead-letter
queue -- which looks like a poison message and is actually a missing line in a
dictionary. The failure is silent at the moment it is introduced and expensive
later, which is exactly the shape a one-line test is for.

Written when `triage_invoice` was added and nothing broke.
"""

from __future__ import annotations

import inspect

from threshold.domain.jobs import JobKind
from threshold.workers.handlers import HANDLERS


def test_every_job_kind_has_a_handler() -> None:
    missing = [kind.value for kind in JobKind if kind.value not in HANDLERS]
    assert not missing, (
        f"these kinds can be enqueued and never run: {missing}. A job with no handler "
        "exhausts its attempts and lands in the dead-letter queue looking like a bad payload."
    )


def test_every_handler_answers_to_a_job_kind() -> None:
    """The other direction. A handler keyed off a string nothing enqueues is
    dead code that reads as coverage."""
    known = {kind.value for kind in JobKind}
    assert not set(HANDLERS) - known


def test_every_handler_takes_a_session_and_a_payload() -> None:
    """The runtime calls all of them the same way, so a handler with a different
    signature fails at claim time rather than at import time."""
    for name, handler in HANDLERS.items():
        parameters = list(inspect.signature(handler).parameters)
        assert parameters == ["session", "payload"], f"{name} has parameters {parameters}"
        assert inspect.iscoroutinefunction(handler), f"{name} is not async"


def test_the_registry_is_not_empty() -> None:
    """A vacuity guard: every assertion above passes against no handlers."""
    assert len(HANDLERS) == len(JobKind) >= 3
