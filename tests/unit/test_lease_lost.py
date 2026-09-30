"""What the worker does when it finishes a job it no longer owns, without a database.

`tests/integration/test_queue.py` proves the whole path against Postgres: the
lease really is stolen, the audit row really is rolled back. This file pins the
two decisions inside the runtime that the integration test can only observe
from outside, and runs in `make test`, where there is no stack:

* losing is a rollback, never a commit -- which depends on `LeaseLostError`
  being raised *inside* `session_scope`, and on it being an `Exception`;
* losing is not a failure -- `fail()` is not called, because the job belongs to
  the worker that holds it now, and the outcome recorded says so.

Also here: `_violates`, the chain walk that recognises the one IntegrityError
`PaymentService` turns into a refusal. Recognising it too eagerly would dress
any other constraint violation up as "already being paid".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from tests.metrics_reader import Recording

from threshold.adapters.queue import ClaimedJob
from threshold.domain.jobs import JobKind, PermanentJobError, TransientJobError
from threshold.observability import metrics as m
from threshold.services.payments import LIVE_PER_DOCUMENT, _violates
from threshold.workers.handlers import HANDLERS
from threshold.workers.runtime import LeaseLostError, WorkerRuntime

KIND = JobKind.EXTRACT_DOCUMENT.value


class _Session:
    """Just enough session for `session_scope`: it commits or it rolls back."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        self._events.append("commit")

    async def rollback(self) -> None:
        self._events.append("rollback")


class _Queue:
    """A queue whose only opinion is whether this worker still holds the job."""

    def __init__(self, *, still_held: bool) -> None:
        self._still_held = still_held
        self.failures: list[str] = []

    async def succeed(self, session: Any, job_id: uuid.UUID, *, worker_id: str) -> bool:
        return self._still_held

    async def fail(
        self, session: Any, job: ClaimedJob, *, worker_id: str, error: str, permanent: bool
    ) -> bool:
        self.failures.append(error)
        return False


def _job() -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        kind=KIND,
        payload={},
        attempts=1,
        max_attempts=3,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        dedupe_key=None,
        waited=timedelta(0),
    )


async def _execute(
    monkeypatch: pytest.MonkeyPatch, *, still_held: bool
) -> tuple[list[str], _Queue]:
    events: list[str] = []

    async def handler(session: Any, payload: dict[str, Any]) -> None:
        events.append("handler wrote")

    monkeypatch.setitem(HANDLERS, KIND, handler)
    queue = _Queue(still_held=still_held)
    runtime = WorkerRuntime(
        queue=queue,  # type: ignore[arg-type]
        sessions=lambda: _Session(events),  # type: ignore[arg-type, return-value]
    )
    await runtime._execute(_job())
    return events, queue


class TestAWorkerThatLostItsLease:
    async def test_rolls_back_what_its_handler_wrote(
        self, monkeypatch: pytest.MonkeyPatch, recording: Recording
    ) -> None:
        events, _ = await _execute(monkeypatch, still_held=False)

        assert events == ["handler wrote", "rollback"], (
            "the handler's transaction was committed by a worker that no longer held the job"
        )
        assert recording.increase(m.JOB_DURATION, kind=KIND, outcome="lease_lost") == 1

    async def test_does_not_settle_a_failure_for_a_job_it_no_longer_holds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_settle_failure` would call `fail()`, which now finds nothing to fail
        -- but calling it at all would count this as a retry in the metrics and
        log a failure for a job that is running fine on another worker."""
        _, queue = await _execute(monkeypatch, still_held=False)
        assert queue.failures == []

    async def test_a_worker_that_kept_its_lease_commits(
        self, monkeypatch: pytest.MonkeyPatch, recording: Recording
    ) -> None:
        """The vacuity guard. A runtime that rolled back every job would pass
        both tests above."""
        events, queue = await _execute(monkeypatch, still_held=True)

        assert events == ["handler wrote", "commit"]
        assert queue.failures == []
        assert recording.increase(m.JOB_DURATION, kind=KIND, outcome="succeeded") == 1

    def test_the_error_is_one_session_scope_rolls_back_on(self) -> None:
        """`session_scope` rolls back on `except Exception`. A `BaseException` --
        which is what `CancelledError` is, and one reason cancelling the handler
        was rejected -- would skip that rollback."""
        assert issubclass(LeaseLostError, Exception)

    def test_the_error_is_not_a_job_failure(self) -> None:
        """Both job-failure classes end in `fail()`. This must not."""
        assert not issubclass(LeaseLostError, PermanentJobError | TransientJobError)

    def test_a_lost_lease_is_its_own_outcome(self) -> None:
        """Not a success, and not a retry: the job is still running, elsewhere.
        A steady rate of these is the signal that handlers outlive the lease."""
        others = {m.JobOutcome.SUCCEEDED, m.JobOutcome.RETRYING, m.JobOutcome.DEAD_LETTERED}
        assert m.JobOutcome.LEASE_LOST not in others
        assert m.JobOutcome.LEASE_LOST.value == "lease_lost"


class _DriverError(Exception):
    """asyncpg's shape: the constraint is an attribute, not a sentence."""

    def __init__(self, message: str, constraint_name: str | None) -> None:
        super().__init__(message)
        self.constraint_name = constraint_name


def _wrapped(driver: BaseException) -> IntegrityError:
    """SQLAlchemy's shape: `IntegrityError.orig` is an adapter whose cause is
    the driver's exception, which is the layer that knows the constraint."""
    adapter = Exception("adapter")
    adapter.__cause__ = driver
    return IntegrityError("INSERT INTO payment ...", {}, adapter)


class TestRecognisingTheRefusal:
    def test_the_live_per_document_index_is_recognised_through_the_wrapping(self) -> None:
        exc = _wrapped(_DriverError("duplicate key", LIVE_PER_DOCUMENT))
        assert _violates(exc, LIVE_PER_DOCUMENT)

    def test_another_constraint_is_not(self) -> None:
        """`uq_payment_authorisation` firing is a different defect -- one
        decision paid twice -- and must surface as itself."""
        exc = _wrapped(_DriverError("duplicate key", "uq_payment_authorisation"))
        assert not _violates(exc, LIVE_PER_DOCUMENT)

    def test_the_name_in_the_message_is_not_enough(self) -> None:
        """By attribute, not by prose. A message that merely mentions the index
        -- or a longer name that contains it -- is not this index refusing."""
        exc = _wrapped(_DriverError(f"violates {LIVE_PER_DOCUMENT}", f"{LIVE_PER_DOCUMENT}_v2"))
        assert not _violates(exc, LIVE_PER_DOCUMENT)

    def test_a_chain_that_loops_terminates(self) -> None:
        first = Exception("first")
        second = Exception("second")
        first.__cause__ = second
        second.__cause__ = first
        assert not _violates(first, LIVE_PER_DOCUMENT)
