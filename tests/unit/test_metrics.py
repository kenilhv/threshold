"""What the instruments actually do.

Three of these pin behaviour that was measured rather than assumed, and each one
was a design decision:

* A histogram that accepts the SDK's default buckets and declares seconds can
  only report "under five seconds", because the defaults are millisecond-scaled.
* A synchronous gauge vanishes from an exposition between scrapes, which is why
  queue depth is observable and published from a snapshot.
* An observable callback runs on the exporter's thread, so it may not touch a
  database — hence the snapshot, and hence the need for it to expire.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from tests.metrics_reader import Recording

from threshold.api.main import create_app
from threshold.domain.jobs import JobKind, JobState
from threshold.observability import metrics as m

#: What the SDK resolves when nobody says otherwise. Millisecond-scaled, and
#: quoted here so the test that rejects them names what it is rejecting.
_SDK_DEFAULT_BOUNDS = (
    0.0, 5.0, 10.0, 25.0, 50.0, 75.0, 100.0, 250.0, 500.0, 750.0,
    1000.0, 2500.0, 5000.0, 7500.0, 10000.0,
)  # fmt: skip


class TestQueueDepthIsZeroFilled:
    """A state that empties has to say so.

    The worker's reading comes from a `GROUP BY`, which returns only the states
    that currently have rows. Publishing that verbatim means the last non-zero
    value stands for ever: the dead-letter count drops to zero in the database
    and stays at five on the dashboard, which is precisely the direction that
    matters.
    """

    def test_every_state_is_reported_even_when_empty(self, recording: Recording) -> None:
        m.publish_queue_snapshot(
            depth_by_state={JobState.PENDING.value: 3},
            oldest_pending_age_seconds=None,
            dead_letters_unreplayed=0,
        )
        reported = {tuple(sorted(a.items())) for a in recording.labels(m.QUEUE_DEPTH)}
        assert {(("state", state.value),) for state in JobState} <= reported

    def test_the_empty_states_report_zero_and_not_the_last_value(
        self, recording: Recording
    ) -> None:
        m.publish_queue_snapshot(
            depth_by_state={JobState.DEAD.value: 5},
            oldest_pending_age_seconds=None,
            dead_letters_unreplayed=5,
        )
        assert recording.value(m.QUEUE_DEPTH, state=JobState.DEAD.value) == 5

        m.publish_queue_snapshot(
            depth_by_state={},
            oldest_pending_age_seconds=None,
            dead_letters_unreplayed=0,
        )
        assert recording.value(m.QUEUE_DEPTH, state=JobState.DEAD.value) == 0

    def test_a_state_the_code_does_not_know_is_not_published(self, recording: Recording) -> None:
        """`Job.state` is a string column, so it can hold anything."""
        m.publish_queue_snapshot(
            depth_by_state={"quantum": 99},
            oldest_pending_age_seconds=None,
            dead_letters_unreplayed=0,
        )
        assert all(a["state"] != "quantum" for a in recording.labels(m.QUEUE_DEPTH))


class TestTheSnapshotExpires:
    """A frozen gauge is worse than an absent one.

    If the worker that publishes queue depth stops looking, the last reading
    would otherwise stand for ever and report a healthy queue indefinitely.
    That is the failure that teaches people not to trust a dashboard. An absent
    series is a question Prometheus can ask with `absent()`.
    """

    def test_no_snapshot_means_no_observations(self, recording: Recording) -> None:
        m.reset_for_testing()
        assert recording.labels(m.QUEUE_DEPTH) == []
        assert recording.labels(m.DEAD_LETTERS) == []

    def test_a_stale_snapshot_is_not_published(
        self, recording: Recording, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m.publish_queue_snapshot(
            depth_by_state={JobState.PENDING.value: 7},
            oldest_pending_age_seconds=4.0,
            dead_letters_unreplayed=2,
        )
        assert recording.labels(m.QUEUE_DEPTH), "nothing was published, so this proves nothing"

        later = time.monotonic() + m.SNAPSHOT_TTL.total_seconds() + 1
        monkeypatch.setattr(m.time, "monotonic", lambda: later)
        fresh = Recording(before={})
        assert fresh.labels(m.QUEUE_DEPTH) == []

    def test_the_clock_is_monotonic(self) -> None:
        """A host clock correction must not expire a fresh snapshot, or keep a
        dead one alive. This stack runs on a laptop that sleeps."""
        m.publish_queue_snapshot(
            depth_by_state={}, oldest_pending_age_seconds=None, dead_letters_unreplayed=0
        )
        assert m._queue._taken_at <= time.monotonic()


class TestOldestAgeDistinguishesEmptyFromNew:
    def test_an_empty_queue_reports_nothing_rather_than_zero(self, recording: Recording) -> None:
        """Zero would mean "the oldest job arrived this instant", which is the
        opposite of "there are no jobs" for an alert that fires on age."""
        m.publish_queue_snapshot(
            depth_by_state={}, oldest_pending_age_seconds=None, dead_letters_unreplayed=0
        )
        assert recording.labels(m.QUEUE_OLDEST_AGE) == []

    def test_an_age_is_reported_when_something_is_waiting(self, recording: Recording) -> None:
        m.publish_queue_snapshot(
            depth_by_state={JobState.PENDING.value: 1},
            oldest_pending_age_seconds=12.5,
            dead_letters_unreplayed=0,
        )
        assert recording.value(m.QUEUE_OLDEST_AGE) == 12.5


class TestHistogramsResolveWhereTheTrafficIs:
    """The SDK's default boundaries are millisecond-scaled: 0, 5, 10, 25 ...

    A histogram that declares seconds and takes them puts every number this
    system produces into the first real bucket. It is named `_seconds`, it
    scrapes cleanly, and it can only ever report "somewhere under five seconds".
    """

    def test_job_duration_does_not_use_the_millisecond_defaults(self, recording: Recording) -> None:
        m.record_job_execution(
            kind=JobKind.EXTRACT_DOCUMENT.value, outcome=m.JobOutcome.SUCCEEDED, seconds=0.007
        )
        resolved = recording.bounds(m.JOB_DURATION)
        assert resolved == m.JOB_DURATION_BUCKETS
        assert resolved != _SDK_DEFAULT_BOUNDS
        assert max(m.JOB_DURATION_BUCKETS) <= 30.0, "a job that runs for 10 000 s is not a job"

    def test_measured_durations_land_in_different_buckets(self, recording: Recording) -> None:
        """The property the defaults would break.

        Measured over 317 jobs: execution p50 is 3-7 ms and the maximum is
        52 ms. Under the default boundaries every one of those is in `le=5`
        along with a five-second job, so a regression from 7 ms to 900 ms would
        not move the histogram at all.
        """
        fast, slow = 0.007, 0.9
        assert self._bucket_of(fast) != self._bucket_of(slow)
        assert self._bucket_of(fast) != self._bucket_of(0.4)

    @staticmethod
    def _bucket_of(seconds: float) -> int:
        for index, bound in enumerate(m.JOB_DURATION_BUCKETS):
            if seconds <= bound:
                return index
        return len(m.JOB_DURATION_BUCKETS)

    def test_queue_wait_resolves_the_poll_interval_and_the_backoffs(self) -> None:
        """The boundaries have to fall where the system's behaviour changes.

        A job normally waits less than the one-second poll; a retried job waits
        the five- or ten-second backoff; a job whose worker died waits the
        two-minute lease. Those are three different incidents with three
        different runbooks, so each needs a boundary to sit behind.
        """
        for landmark in (1.0, 5.0, 10.0, 120.0):
            assert landmark in m.QUEUE_WAIT_BUCKETS, f"no boundary at {landmark}s"


class TestTheApiDoesNotExplodeTheLabelSpace:
    """The cardinality proof, against the real application.

    This is the failure the registry exists to prevent, and it is the one place
    the project does not control the label itself: the RED metrics come from
    `opentelemetry-instrumentation-fastapi`, which this module deliberately does
    not duplicate. Trusting it without checking would be trusting a library to
    keep a promise nobody wrote down.

    The key is a trap worth naming. On these metrics the route TEMPLATE arrives
    under `http.target`; on spans, that same key holds the raw path. Anyone
    reading one and reasoning about the other is wrong.
    """

    @staticmethod
    def _client() -> TestClient:
        # raise_server_exceptions=False so a 500 is a response to measure rather
        # than an exception in the test. These routes have no engine, which is
        # the point: they answer in milliseconds and never touch a network.
        return TestClient(create_app(), raise_server_exceptions=False)

    def test_a_route_parameter_does_not_become_a_series(self, recording: Recording) -> None:
        client = self._client()
        for _ in range(3):
            client.get(f"/v1/documents/{uuid.uuid4()}")

        targets = {a.get("http.target") for a in recording.labels(m.HTTP_DURATION)}
        assert targets == {"/v1/documents/{document_id}"}, (
            f"three invoices produced {len(targets)} series: {targets}"
        )

    def test_a_path_that_matches_no_route_carries_no_route_label(
        self, recording: Recording
    ) -> None:
        """A port scanner must not be able to grow the exposition.

        Unrouted requests collapse into one data point with no route attribute
        at all, which is the behaviour that makes this safe to expose.
        """
        client = self._client()
        for index in range(12):
            client.get(f"/no/such/path/{index}")

        unrouted = [
            a
            for a in recording.labels(m.HTTP_DURATION)
            if a.get("http.status_code") == 404 and "http.target" not in a
        ]
        assert len(unrouted) == 1, recording.labels(m.HTTP_DURATION)

    def test_the_probes_are_excluded_from_metrics_and_not_only_from_traces(
        self, recording: Recording
    ) -> None:
        """Compose polls these every five seconds for ever.

        `EXCLUDED_URLS` was introduced for spans; that it also suppresses
        metrics is a property of the instrumentation rather than of this code,
        so it is checked rather than assumed. Without it, probe traffic would
        dominate every latency percentile the API reports.
        """
        client = self._client()
        for _ in range(5):
            client.get("/healthz")
            client.get("/readyz")
        # A route that is NOT excluded, so "nothing was recorded because the app
        # is not instrumented" cannot be mistaken for "the probes were excluded".
        client.get(f"/v1/documents/{uuid.uuid4()}")

        targets = {a.get("http.target") for a in recording.labels(m.HTTP_DURATION)}
        assert targets == {"/v1/documents/{document_id}"}, (
            f"expected only the un-excluded route, got {targets}"
        )

    def test_the_size_histograms_are_dropped(self, recording: Recording) -> None:
        client = self._client()
        client.post(
            "/v1/match-results/8c7b1e1a-0000-4000-8000-000000000000/decision",
            json={"decision": "approved"},
        )
        # The request happened and was measured; only the size histograms are
        # missing, which is the difference between a drop and a dead test.
        assert m.HTTP_DURATION in recording.names()
        assert not set(m.DROPPED) & recording.names()

    def test_only_declared_keys_reach_the_exposition(self, recording: Recording) -> None:
        """The views, applied to labels this project did not choose."""
        client = self._client()
        client.get(f"/v1/documents/{uuid.uuid4()}")

        points = list(recording.every_point())
        assert points, "the exposition is empty, so this checks nothing"
        for name, attributes in points:
            spec = m.BY_NAME.get(name)
            assert spec is not None, f"{name} is exported but not declared in REGISTRY"
            assert set(attributes) <= set(spec.labels), (
                f"{name} carries undeclared keys {set(attributes) - set(spec.labels)}"
            )


class TestJobOutcomes:
    def test_a_retry_and_a_dead_letter_are_different_outcomes(self) -> None:
        """An alert on "jobs are failing" that cannot tell them apart fires the
        same way for a transient blip and for a poison message."""
        assert m.JobOutcome.RETRYING != m.JobOutcome.DEAD_LETTERED

    def test_every_outcome_the_runtime_can_produce_is_declared(self) -> None:
        import threshold.workers.runtime as runtime

        assert runtime._outcome_of(dead_lettered=True) is m.JobOutcome.DEAD_LETTERED
        assert runtime._outcome_of(dead_lettered=False) is m.JobOutcome.RETRYING
