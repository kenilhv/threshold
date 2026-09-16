"""Metrics, behind a registry that the SDK enforces.

The third signal. Logs say what happened to one request, traces say where the
time went, and metrics say what is true of all of them at once — which is the
only one of the three that can answer "is the auto-approval rate falling" or
"is the queue keeping up".

The organising idea here is that **the label set is a schema**. Cardinality is
what kills a metrics backend, and it is killed by a single careless label: one
``document_id`` on one counter turns a 6-series metric into a series per
invoice, for ever, and the damage is done before anyone notices. Code review is
not a control for that, because the reviewer has to notice an attribute that
looks perfectly reasonable at the call site.

So every instrument is declared in ``REGISTRY`` with the exact attribute keys it
may carry, and ``configure_metrics`` turns each declaration into an SDK ``View``
with ``attribute_keys=``. An attribute that is not declared is **dropped by the
SDK before it reaches an exporter**. The registry is enforcement, not
documentation, and ``tests/unit/test_metric_labels.py`` guards the registry
itself against someone declaring ``document_id`` into it.

Each entry also names the thing that reads it. This codebase's recurring defect
is "declared and never read" — ``trace_id`` held the wrong value for three
milestones, ``DeadLetter.dedupe_key`` has never been written — and a metric
nobody plots is the same defect wearing a different hat. A declaration with no
consumer is a bug, and the test says so.

Four things were measured rather than assumed, and each changed the design:

**The SDK's default histogram buckets are millisecond-scaled**: ``(0, 5, 10,
25, ... 10000)``. A histogram declared in seconds that accepts them puts every
number this system produces — job execution runs 3 to 52 ms — into the ``le=5``
bucket, so the metric is named ``_seconds``, is scraped happily, and can only
report "somewhere under five seconds". Every histogram here passes explicit
boundaries derived from measured distributions, recorded beside each one.

**A synchronous ``Gauge`` disappears from an exposition between scrapes.** If
``set()`` was not called since the last scrape the series is simply absent, so a
queue depth sampled every 30 s and scraped every 15 s would flap in and out of
existence. Observable gauges re-run their callback on every collection and do
not, which is why queue depth is observable here.

**Observable callbacks run on the exporter's thread** — and on the caller's
thread during ``force_flush``. A callback that reached for the database would be
doing async I/O from a thread with no event loop, on a timer, with its
exceptions swallowed and logged. So the callbacks here are pure reads of a
snapshot that the worker refreshes from its own loop.

**FastAPI instrumentation already emits RED metrics**, so this module does not.
Hand-rolling a second set would double-count and would disagree about labels.
Two details of what it emits matter: the route arrives as a **template**, so
``/v1/documents/{document_id}/match`` is one series rather than one per invoice,
and a request that matched no route carries no route attribute at all — meaning
a port scanner cannot inflate the metric. The trap is the key: on metrics the
template lives under ``http.target``, and on *spans* that same key holds the raw
path.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

import structlog
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, View

from threshold.config.settings import Settings
from threshold.domain.jobs import JobKind, JobState
from threshold.domain.payments import PaymentState
from threshold.observability.resource import resource_for

log = structlog.get_logger(__name__)


# --------------------------------------------------------------- the registry


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One instrument, and everything that is true of it by declaration."""

    name: str
    #: What Prometheus calls this series once its OTLP receiver has translated
    #: the name. Written down rather than derived, because the translation is
    #: done in Go by Prometheus and is not obvious: dots become underscores, a
    #: counter gains ``_total``, unit ``s`` appends ``_seconds`` -- but a unit
    #: whose word is already a token in the name appends nothing, and an
    #: annotation unit like ``{job}`` appends nothing either. Every value here
    #: is checked against a running Prometheus by
    #: ``tests/integration/test_telemetry_stack.py``, so a wrong guess fails the
    #: gate rather than an empty dashboard panel.
    prometheus_name: str
    #: Exactly the attribute keys this instrument may carry. Anything else is
    #: dropped by the SDK before export.
    labels: frozenset[str]
    #: Dashboard panels that plot this, by exact panel title. A metric nobody
    #: plots and nobody alerts on is this repository's favourite defect wearing
    #: a new hat, so at least one consumer is required -- and these are matched
    #: against the provisioned Grafana JSON rather than taken on trust.
    dashboards: tuple[str, ...]
    #: Alert rules that fire on this, by exact rule name. Matched against the
    #: provisioned rules file the same way.
    alerts: tuple[str, ...]
    #: Which process emits it, so a test knows where to go looking.
    emitted_by: str

    @property
    def consumers(self) -> tuple[str, ...]:
        return self.dashboards + self.alerts


#: Keys that are never a label, whatever an instrument declares.
#:
#: The View allowlist already stops an undeclared attribute at the call site.
#: This guards the other direction: someone editing ``REGISTRY`` to *declare*
#: one of these. Each is unbounded in the only way that matters — it grows with
#: the number of invoices, people, or requests the system has ever seen — and
#: each is already available on the trace and on the audit row, which are the
#: two places built to hold per-entity detail.
FORBIDDEN_LABELS: Final = frozenset(
    {
        "actor_id",
        "actor_label",
        "dedupe_key",
        "document_id",
        "email",
        "extraction_id",
        "invoice_number",
        "job_id",
        "match_result_id",
        "po_number",
        "purchase_order_id",
        "reason",
        "request_id",
        "span_id",
        "storage_key",
        "trace_id",
        "user_id",
        "vendor_id",
        "vendor_name",
    }
)

MATCH_DECISIONS: Final = "threshold.match.decisions"
MATCH_EXCEPTIONS: Final = "threshold.match.exceptions"
APPROVAL_DECISIONS: Final = "threshold.approval.decisions"
JOB_DURATION: Final = "threshold.job.duration"
QUEUE_WAIT: Final = "threshold.job.queue.wait"
QUEUE_DEPTH: Final = "threshold.job.queue.depth"
QUEUE_OLDEST_AGE: Final = "threshold.job.queue.oldest.age"
DEAD_LETTERS: Final = "threshold.job.dead_letters"
PAYMENTS_IN_FLIGHT: Final = "threshold.payment.in_flight"
PAYMENT_OLDEST_SUBMITTING: Final = "threshold.payment.oldest_submitting.age"

HTTP_DURATION: Final = "http.server.duration"
HTTP_ACTIVE_REQUESTS: Final = "http.server.active_requests"
DB_CONNECTIONS: Final = "db.client.connections.usage"

#: Emitted by instrumentation this project installs but does not want. Their
#: default buckets stop at 10 000 bytes and the documents this API accepts are
#: larger than that, so every observation lands in the overflow bucket: four
#: series per route to say "bigger than 10 KB". If document size becomes
#: interesting it belongs on an instrument that knows what a document is.
DROPPED: Final = ("http.server.request.size", "http.server.response.size")

REGISTRY: Final[tuple[MetricSpec, ...]] = (
    # -- what the system decided, which is what it is for ---------------------
    MetricSpec(
        name=MATCH_DECISIONS,
        prometheus_name="threshold_match_decisions_total",
        labels=frozenset({"disposition", "policy_version"}),
        dashboards=("Automation rate", "Match dispositions"),
        alerts=("AutoApprovalRateCollapsed", "BlockingExceptionSurge"),
        emitted_by="worker",
    ),
    MetricSpec(
        name=MATCH_EXCEPTIONS,
        prometheus_name="threshold_match_exceptions_total",
        labels=frozenset({"code", "severity", "owner_role"}),
        dashboards=("Exceptions by owner",),
        alerts=("BlockingExceptionSurge",),
        emitted_by="worker",
    ),
    MetricSpec(
        name=APPROVAL_DECISIONS,
        prometheus_name="threshold_approval_decisions_total",
        # The one metric this project exists to produce: the share of invoices
        # that clear without a person. `actor_type` is what makes that a ratio
        # rather than a count, and it is two values for ever -- the machine
        # writes its decision into the same table a person writes to.
        labels=frozenset({"decision", "actor_type"}),
        dashboards=("Machine versus human",),
        alerts=(),
        emitted_by="api, worker",
    ),
    # -- whether the work is getting done -------------------------------------
    MetricSpec(
        name=JOB_DURATION,
        prometheus_name="threshold_job_duration_seconds",
        labels=frozenset({"kind", "outcome"}),
        dashboards=("Job latency", "Job outcomes"),
        alerts=("JobsDeadLettering",),
        emitted_by="worker",
    ),
    MetricSpec(
        name=QUEUE_WAIT,
        prometheus_name="threshold_job_queue_wait_seconds",
        labels=frozenset({"kind"}),
        dashboards=("Queue wait",),
        alerts=(),
        emitted_by="worker",
    ),
    MetricSpec(
        name=QUEUE_DEPTH,
        prometheus_name="threshold_job_queue_depth",
        labels=frozenset({"state"}),
        dashboards=("Queue depth",),
        alerts=("QueueBacklogGrowing", "QueueObserverStopped"),
        emitted_by="worker",
    ),
    MetricSpec(
        name=PAYMENTS_IN_FLIGHT,
        prometheus_name="threshold_payment_in_flight",
        labels=frozenset({"state"}),
        dashboards=("Payments in flight",),
        alerts=("PaymentObserverStopped",),
        emitted_by="worker",
    ),
    MetricSpec(
        name=PAYMENT_OLDEST_SUBMITTING,
        prometheus_name="threshold_payment_oldest_submitting_age_seconds",
        labels=frozenset(),
        dashboards=("Oldest payment in flight",),
        alerts=("PaymentStuckInFlight",),
        emitted_by="worker",
    ),
    MetricSpec(
        name=QUEUE_OLDEST_AGE,
        prometheus_name="threshold_job_queue_oldest_age_seconds",
        labels=frozenset(),
        dashboards=("Oldest claimable job",),
        alerts=("OldestJobStuck",),
        emitted_by="worker",
    ),
    MetricSpec(
        name=DEAD_LETTERS,
        prometheus_name="threshold_job_dead_letters",
        labels=frozenset(),
        dashboards=("Dead letters",),
        alerts=("DeadLettersUnreplayed",),
        emitted_by="worker",
    ),
    # -- emitted by instrumentation, declared here because it still ships ------
    MetricSpec(
        name=HTTP_DURATION,
        # Milliseconds, not seconds: the instrumentation emits the older
        # semantic convention by default, and Prometheus appends the unit it is
        # actually given. Renaming it would mean opting into the stable semconv
        # and changing every attribute key at the same time.
        prometheus_name="http_server_duration_milliseconds",
        # Trimmed from the eight attributes the instrumentor offers. The five
        # dropped ones -- scheme, host, port, flavor, server_name -- are
        # constant for a deployment, so they multiply the exposition without
        # ever varying. `http.target` is the route TEMPLATE here, not the path.
        labels=frozenset({"http.method", "http.status_code", "http.target"}),
        dashboards=("API request rate", "API latency", "API errors"),
        alerts=("ApiErrorRate", "ApiLatencyHigh"),
        emitted_by="api",
    ),
    MetricSpec(
        name=HTTP_ACTIVE_REQUESTS,
        prometheus_name="http_server_active_requests",
        labels=frozenset({"http.method"}),
        dashboards=("API concurrency",),
        alerts=(),
        emitted_by="api",
    ),
    MetricSpec(
        name=DB_CONNECTIONS,
        # No unit suffix: Prometheus omits it when the unit's word is already a
        # token in the name, and "connections" is. Guessing `_connections` here
        # would have produced a dashboard panel that renders empty and reports
        # no error.
        prometheus_name="db_client_connections_usage",
        # `pool.name` is the DSN with the credentials stripped by SQLAlchemy.
        # One value per engine, so it is bounded -- but the stripping is what
        # keeps a password out of a metrics backend, and test_metric_labels.py
        # pins it rather than trusting it to survive an upgrade.
        labels=frozenset({"pool.name", "state"}),
        dashboards=("Connection pool",),
        alerts=(),
        emitted_by="api, worker",
    ),
)


#: Attribute keys become Prometheus label names with dots replaced. Written as
#: a function rather than a lookup because it is the same rule Prometheus
#: applies, and a dashboard querying `http.target` instead of `http_target`
#: renders an empty panel rather than an error.
def prometheus_label(attribute: str) -> str:
    return attribute.replace(".", "_")


BY_NAME: Final[Mapping[str, MetricSpec]] = {spec.name: spec for spec in REGISTRY}


# ------------------------------------------------------------------- buckets
#
# Every boundary set below is derived from a measurement taken against the
# running stack, not from a default. The measurements are quoted where they are
# used so a later reader can tell whether they still hold.

#: Job execution, in seconds. Measured over 317 completed jobs: extract p50
#: 7 ms / max 41 ms, match p50 3 ms / max 52 ms. The first four boundaries
#: resolve that range; the tail is kept because M7 adds a model tier beside the
#: deterministic one, and a model call is seconds rather than milliseconds,
#: and a histogram that has to be re-bucketed to see the change is a histogram
#: that hides it.
JOB_DURATION_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

#: Queue wait, in seconds: from a job becoming available to a worker claiming
#: it. Measured p50 0.503 s and max 1.084 s for `extract_document`, which is the
#: one-second poll sampled uniformly, and p50 0.029 s for `match_invoice`,
#: which the worker claims on its next loop without sleeping. The boundaries
#: therefore resolve below the poll interval, mark the poll interval itself, and
#: then step to the two backoff delays (5 s, 10 s) and the lease (120 s) —
#: because "waited longer than one poll" and "waited longer than a lease" are
#: different incidents with different runbooks.
QUEUE_WAIT_BUCKETS: Final = (0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

#: HTTP server duration arrives from the instrumentor in MILLISECONDS, so the
#: SDK defaults are correctly scaled for once and would work. They are replaced
#: anyway because they were not chosen for this service: measured, the API
#: serves /healthz in 3 ms, /readyz in 6 ms and the match endpoint in 7 ms, so
#: the default first bucket at 5 ms splits the healthy range almost arbitrarily
#: and the next is 10 ms. These resolve where the traffic actually is.
HTTP_DURATION_BUCKETS: Final = (
    1.0, 2.5, 5.0, 7.5, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0,
)  # fmt: skip


# ------------------------------------------------------------- configuration

#: Kept well below the SDK's default of 10 s. `shutdown()` runs a final export
#: on the exiting thread, so a worker stopping while the collector is down
#: blocks for roughly the exporter timeout before the process can die —
#: measured at 11.3 s with the default. This stack runs on a laptop where the
#: collector is routinely not running and `make down` has a grace period.
EXPORT_TIMEOUT: Final = timedelta(seconds=3)

#: Faster than the SDK's 60 s default. A minute is a long time to wait to see
#: whether a fix worked while developing, and the volume here is a few hundred
#: series.
EXPORT_INTERVAL: Final = timedelta(seconds=15)

_provider_configured = False


def _views() -> list[View]:
    """The registry, compiled into SDK enforcement.

    ``attribute_keys`` is an allowlist: the SDK removes every attribute not
    named, so an undeclared label never reaches an exporter no matter what a
    call site passes. ``instrument_name`` matching is fnmatch, and every name
    here is a literal, so each view matches exactly one instrument.
    """
    views = [View(instrument_name=spec.name, attribute_keys=set(spec.labels)) for spec in REGISTRY]
    views += [View(instrument_name=name, aggregation=DropAggregation()) for name in DROPPED]
    return views


def configure_metrics(settings: Settings) -> None:
    """Install the meter provider. Idempotent, for the reason tracing is.

    A second `set_meter_provider` does not replace the first — it logs
    "Overriding of current MeterProvider is not allowed" and keeps the original,
    silently sending every later measurement to the first provider's readers.
    `create_app()` runs at import, so unit tests importing the API would hit
    that on every import.

    No endpoint is a supported configuration, exactly as it is for tracing: the
    provider is installed with no readers, the instruments stay real SDK
    instruments rather than no-ops, and every measurement is fanned out to zero
    storages and dropped. Measured: 300 000 recordings against a reader-less
    provider grow the heap by under a kilobyte, where the same loop with a
    reader attached grows it by 51 MB.
    """
    global _provider_configured
    if _provider_configured:
        return

    readers: list[PeriodicExportingMetricReader] = []
    if settings.otel_metrics_endpoint:
        endpoint = f"{settings.otel_metrics_endpoint.rstrip('/')}/v1/metrics"
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, timeout=EXPORT_TIMEOUT.total_seconds()),
                export_interval_millis=EXPORT_INTERVAL.total_seconds() * 1000,
            )
        )
        log.info("metrics.exporting", service=settings.service_name, endpoint=endpoint)
    else:
        log.info("metrics.local_only", service=settings.service_name)

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource_for(settings),
            metric_readers=readers,
            views=_views(),
        )
    )
    _provider_configured = True


def install_test_provider(reader: MetricReader) -> None:
    """Install a provider a test can read from. Never called in production.

    Tracing's tests can sidestep the global entirely, because every consumer
    there takes a ``tracer_provider=`` argument. Metrics cannot: the instruments
    below are module-level, they resolve against the first global provider
    installed in the process, and once resolved they are pinned to it — a second
    ``set_meter_provider`` is refused with a warning and every later measurement
    keeps going to the first. So the choice is to install a readable provider
    before anything else does, or to test copies of the instruments rather than
    the ones the system actually records into.

    Marking the module configured is the other half: `create_app()` runs
    `configure_metrics` at import time, and without this it would install a
    reader-less provider first and win.

    Raises rather than returning quietly if a provider is already installed,
    because the alternative is the worst outcome available: the global refuses
    the second provider, the reader passed here is attached to nothing, and
    every assertion made through it reads an empty exposition and passes. A test
    suite that cannot see its own metrics but reports success is worse than no
    metrics test at all.
    """
    global _provider_configured
    if _provider_configured:
        raise RuntimeError(
            "a meter provider is already installed in this process; a second reader "
            "would be attached to nothing and every metric assertion made through it "
            "would silently pass on an empty exposition"
        )
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader], views=_views()))
    _provider_configured = True


def reset_for_testing() -> None:
    """Forget the published queue snapshot. Never called in production."""
    _queue.clear()


# ----------------------------------------------------------------- instruments
#
# Created at import, before any provider exists. That is safe: the API hands
# back proxy instruments that resolve against whatever provider is installed
# later, and measurements recorded afterwards arrive correctly. What is NOT
# safe is recording *before* a provider is installed — those are dropped with
# no exception and no log line — which is why `configure_metrics` is called at
# the top of `create_app()` and of the worker's `_main()`, ahead of any work.

_meter = metrics.get_meter(__name__)

_match_decisions = _meter.create_counter(
    MATCH_DECISIONS,
    unit="{decision}",
    description="Match results by disposition.",
)
_match_exceptions = _meter.create_counter(
    MATCH_EXCEPTIONS,
    unit="{exception}",
    description="Exceptions raised by the matcher, by code.",
)
_approval_decisions = _meter.create_counter(
    APPROVAL_DECISIONS,
    unit="{decision}",
    description="Terminal and interim approval decisions, by who made them.",
)
_job_duration = _meter.create_histogram(
    JOB_DURATION,
    unit="s",
    description="Time a worker spent executing a job, excluding the wait.",
    explicit_bucket_boundaries_advisory=JOB_DURATION_BUCKETS,
)
_queue_wait = _meter.create_histogram(
    QUEUE_WAIT,
    unit="s",
    description="Time from a job becoming claimable to a worker claiming it.",
    explicit_bucket_boundaries_advisory=QUEUE_WAIT_BUCKETS,
)


# ------------------------------------------------------- recording, by hand
#
# One function per thing worth counting, rather than the instruments being
# public. The call sites then pass domain values and cannot invent a label.


def record_match_decision(*, disposition: str, policy_version: str) -> None:
    _match_decisions.add(1, {"disposition": disposition, "policy_version": policy_version})


def record_match_exceptions(exceptions: Iterable[tuple[str, str, str]]) -> None:
    """One observation per exception raised, as ``(code, severity, owner_role)``."""
    for code, severity, owner_role in exceptions:
        _match_exceptions.add(1, {"code": code, "severity": severity, "owner_role": owner_role})


def record_approval_decision(*, decision: str, actor_type: str) -> None:
    _approval_decisions.add(1, {"decision": decision, "actor_type": actor_type})


class JobOutcome(StrEnum):
    """How a job ended. An enum so `mypy --strict` catches a typo in a label.

    A misspelt outcome would not fail anything at runtime — it would quietly
    start a new time series that no alert rule matches, which is the metrics
    equivalent of a log line nobody greps for.
    """

    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    DEAD_LETTERED = "dead_lettered"
    #: The worker is running older code than whatever enqueued the job.
    NO_HANDLER = "no_handler"


def _kind_label(kind: str) -> str:
    """Clamp a job kind read from the database to the kinds this code knows.

    `Job.kind` is a string column, and the worker's whole reason for having an
    unknown-kind path is that the value may not be a `JobKind` this build has
    heard of. Passing it through would make a row in a table into a label, which
    is the unbounded-cardinality defect arriving by the back door — and it would
    arrive on precisely the path nobody tests.
    """
    try:
        return JobKind(kind).value
    except ValueError:
        return "unknown"


def record_job_execution(*, kind: str, outcome: JobOutcome, seconds: float) -> None:
    _job_duration.record(seconds, {"kind": _kind_label(kind), "outcome": outcome.value})


def record_queue_wait(*, kind: str, seconds: float) -> None:
    _queue_wait.record(seconds, {"kind": _kind_label(kind)})


# ------------------------------------------------------------ the queue gauges
#
# Queue depth is a property of the QUEUE, not of a process, and that distinction
# decides the whole shape of this section.
#
# If every worker reported it, a backend would receive N identical series for
# one number and `sum()` would report N times the true depth. So exactly one
# process publishes: the worker that holds the reaper's advisory lock, which is
# an election the system already runs for the same reason -- no point in eight
# workers doing one scan. A worker that is not elected publishes nothing, and
# the gauge is simply absent from its exposition rather than stale.
#
# The reading is taken on the worker's own event loop and cached here, because
# an observable callback runs on the exporter's thread where there is no loop to
# await a database on.


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """What one worker last saw when it looked at the queue."""

    depth_by_state: Mapping[str, int]
    oldest_pending_age_seconds: float | None
    dead_letters_unreplayed: int


#: How long a snapshot stays publishable. Three times the worker's 30 s
#: observation interval, so a single slow or failed poll does not blink the
#: series out, but a worker that has stopped looking stops claiming to know.
#:
#: Expiring rather than freezing is the point. A frozen gauge reports a healthy
#: depth for ever after the observer dies, which is the failure mode that makes
#: people stop trusting dashboards. An absent series is a question Prometheus
#: can ask with `absent()`.
SNAPSHOT_TTL: Final = timedelta(seconds=90)


@dataclass(frozen=True, slots=True)
class PaymentSnapshot:
    """What one worker last saw when it looked at the payments in flight.

    Separate from `QueueSnapshot` rather than folded into it, because they
    answer different questions and would otherwise expire together: a queue
    observer that stops must blink out the queue series, and it must not also
    blink out the money.
    """

    count_by_state: Mapping[str, int]
    oldest_submitting_age_seconds: float | None


class _SnapshotHolder[Snapshot]:
    """Written by the worker's loop, read by the exporter's thread.

    A lock rather than relying on the GIL: the callback reads several fields and
    must not see a half-updated pair. Held for a reference swap only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._taken_at = 0.0

    def publish(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot
            # Monotonic, so a host clock correction cannot make a fresh
            # snapshot look older than the TTL -- or, worse, keep a dead one
            # alive.
            self._taken_at = time.monotonic()

    def current(self) -> Snapshot | None:
        with self._lock:
            if self._snapshot is None:
                return None
            if time.monotonic() - self._taken_at > SNAPSHOT_TTL.total_seconds():
                return None
            return self._snapshot

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None
            self._taken_at = 0.0


_queue: _SnapshotHolder[QueueSnapshot] = _SnapshotHolder()
_payments: _SnapshotHolder[PaymentSnapshot] = _SnapshotHolder()


def publish_queue_snapshot(
    *,
    depth_by_state: Mapping[str, int],
    oldest_pending_age_seconds: float | None,
    dead_letters_unreplayed: int,
) -> None:
    """Called by the elected worker after it reads the queue.

    Depth is zero-filled across every ``JobState`` here rather than at the call
    site, because the caller's ``GROUP BY`` only returns states that currently
    have rows. Without the fill, a state that empties keeps its last non-zero
    value forever: the dead-letter count would drop to zero in the database and
    stay at five on the dashboard, which is precisely the direction that
    matters.
    """
    filled = {state.value: 0 for state in JobState}
    filled.update({str(k): int(v) for k, v in depth_by_state.items() if str(k) in filled})
    _queue.publish(
        QueueSnapshot(
            depth_by_state=filled,
            oldest_pending_age_seconds=oldest_pending_age_seconds,
            dead_letters_unreplayed=dead_letters_unreplayed,
        )
    )


def publish_payment_snapshot(
    *,
    count_by_state: Mapping[str, int],
    oldest_submitting_age_seconds: float | None,
) -> None:
    """Called by the elected worker after it reads the payment table.

    Zero-filled across every `PaymentState` for the same reason the queue depth
    is: a `GROUP BY` returns only the states that currently have rows, so a
    state that empties would keep its last non-zero value for ever. The
    direction that matters here is the same one -- `submitting` draining to zero
    must show as zero rather than as the last number anybody saw.
    """
    filled = {state.value: 0 for state in PaymentState}
    filled.update({str(k): int(v) for k, v in count_by_state.items() if str(k) in filled})
    _payments.publish(
        PaymentSnapshot(
            count_by_state=filled,
            oldest_submitting_age_seconds=oldest_submitting_age_seconds,
        )
    )


def _observe_payments(_options: CallbackOptions) -> Sequence[Observation]:
    snapshot = _payments.current()
    if snapshot is None:
        return ()
    return [
        Observation(count, {"state": state}) for state, count in snapshot.count_by_state.items()
    ]


def _observe_oldest_submitting(_options: CallbackOptions) -> Sequence[Observation]:
    snapshot = _payments.current()
    if snapshot is None:
        return ()
    # Zero when nothing is in flight, rather than nothing at all, so that an
    # absent series means "nobody is watching the money" and not "no payment is
    # stuck". Those must not look the same.
    return [Observation(snapshot.oldest_submitting_age_seconds or 0.0)]


def _observe_depth(_options: CallbackOptions) -> Sequence[Observation]:
    snapshot = _queue.current()
    if snapshot is None:
        return ()
    return [
        Observation(count, {"state": state}) for state, count in snapshot.depth_by_state.items()
    ]


def _observe_oldest_age(_options: CallbackOptions) -> Sequence[Observation]:
    snapshot = _queue.current()
    if snapshot is None:
        return ()
    # An empty queue reports zero rather than reporting nothing, so that an
    # absent series means exactly one thing: nobody is watching the queue.
    #
    # The first version omitted it, on the argument that zero is
    # indistinguishable from "the oldest job arrived this instant". That is
    # true and it does not matter -- both say nothing is behind. What does
    # matter is the other reading: if "empty" and "no observer" are both
    # absence, then the alert for a dead observer cannot be written, and that
    # is the failure worth catching. Nothing waiting is a measurement; nothing
    # reporting is an outage.
    return [Observation(snapshot.oldest_pending_age_seconds or 0.0)]


def _observe_dead_letters(_options: CallbackOptions) -> Sequence[Observation]:
    snapshot = _queue.current()
    if snapshot is None:
        return ()
    return [Observation(snapshot.dead_letters_unreplayed)]


_meter.create_observable_gauge(
    QUEUE_DEPTH,
    callbacks=[_observe_depth],
    unit="{job}",
    description="Jobs in the queue by state, as last seen by the elected worker.",
)
_meter.create_observable_gauge(
    QUEUE_OLDEST_AGE,
    callbacks=[_observe_oldest_age],
    unit="s",
    description="Age of the oldest claimable job.",
)
_meter.create_observable_gauge(
    DEAD_LETTERS,
    callbacks=[_observe_dead_letters],
    unit="{job}",
    description="Dead letters nobody has replayed.",
)
_meter.create_observable_gauge(
    PAYMENTS_IN_FLIGHT,
    callbacks=[_observe_payments],
    unit="{payment}",
    description="Payments by state, as last seen by the elected worker.",
)
_meter.create_observable_gauge(
    PAYMENT_OLDEST_SUBMITTING,
    callbacks=[_observe_oldest_submitting],
    unit="s",
    description="How long the oldest payment has been awaiting an answer from the rail.",
)
