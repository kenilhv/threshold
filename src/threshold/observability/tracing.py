"""Tracing.

`logging.py` opens by saying logs are one of three signals and on their own are
not observability. This is the second signal.

Three things here are less obvious than they look, and each was measured rather
than assumed:

**FastAPI must be instrumented inside ``create_app``, not in the lifespan.**
Instrumenting from the lifespan does not raise — it silently produces no spans
at all, because Starlette has already built its middleware stack by the time the
lifespan runs. A visible error would have been kinder. There is a test for this
specifically, because "we added tracing and got no traces" is a failure that
looks like a configuration problem for a long time.

**No exporter is a supported configuration, not a broken one.** With no
endpoint configured the provider still creates real, valid, sampled spans; they
simply go nowhere. That is what makes `trace.get_current_span()` usable for
`audit_event.trace_id` in a unit test with no collector running, and it is why
the test suite must never set ``OTEL_SDK_DISABLED``: that variable returns a
no-op tracer whatever provider you install by hand, so every span context comes
back invalid and every assertion about trace ids quietly passes on ``None``.

**Export failures must never become request failures.** The SDK logs and drops.
An observability stack that can take the service down with it is a liability
rather than an instrument, and this one runs on a laptop where the collector is
routinely not running.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from threshold.config.settings import Settings
from threshold.observability.resource import resource_for

log = structlog.get_logger(__name__)

#: Liveness and readiness are polled every five seconds by compose, for ever.
#: Tracing them would bury every real request in probe noise, and a probe's
#: latency is a question for a metric rather than a trace.
EXCLUDED_URLS = "healthz,readyz,metrics"

_provider_configured = False


def configure_tracing(settings: Settings, *, app: FastAPI | None = None) -> None:
    """Install the tracer provider, and instrument what this process talks to.

    Idempotent. `create_app()` runs at import time, so unit tests that import
    the API call this repeatedly; installing a second provider would log
    "Overriding of current TracerProvider is not allowed" and keep the first.
    """
    global _provider_configured

    if not _provider_configured:
        # The same resource the metrics provider uses, from one function, so a
        # trace and a metric describing the same incident agree about which
        # service produced them.
        provider = TracerProvider(resource=resource_for(settings))
        if settings.otel_exporter_otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces"
                    )
                )
            )
            log.info(
                "tracing.exporting",
                service=settings.service_name,
                endpoint=settings.otel_exporter_otlp_endpoint,
            )
        else:
            # Spans are still created and still carry a valid trace id, which is
            # what audit rows and log correlation need. Only export is off.
            log.info("tracing.local_only", service=settings.service_name)

        trace.set_tracer_provider(provider)

        # SQLAlchemy is NOT instrumented here. It is instrumented on the engine
        # in `adapters/db.py`, because the global form patches
        # `create_async_engine` on its module and `db.py` binds that name at
        # import -- long before this function runs. See the note there; the
        # failure was silent, and cost every SQL span in the service.
        #
        # MinIO is reached through boto3, so object storage shows up as spans
        # without a line of storage code changing. Botocore is safe to
        # instrument globally because it wraps a method on
        # `botocore.client.BaseClient`, which every holder of the class sees,
        # rather than replacing an importable name.
        # The botocore instrumentor is untyped upstream; the call is correct.
        BotocoreInstrumentor().instrument()  # type: ignore[no-untyped-call]
        _provider_configured = True

    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)


def current_trace_id() -> str | None:
    """The active trace id as 32 lowercase hex, or None outside a trace.

    Returns `None` rather than a zero id when there is no recording span, so a
    row without a trace says so instead of claiming to belong to trace
    00000000000000000000000000000000 — which is what a naive format of an
    invalid span context produces, and which looks like a real id in a column.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return trace.format_trace_id(context.trace_id)


def reset_for_testing() -> None:
    """Allow a test to install its own provider. Never called in production."""
    global _provider_configured
    _provider_configured = False
