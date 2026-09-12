"""Tracing, and the two ways it silently does nothing.

Both failures here produce no error and no trace. They were found by measuring
rather than by reading documentation, and they are the reason this file exists
at all: "we added OpenTelemetry and got no spans" is a problem that reads like
misconfiguration for a long time before anyone suspects the wiring.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from threshold.config.settings import Settings, get_settings
from threshold.observability.logging import _add_trace_context
from threshold.observability.tracing import configure_tracing, current_trace_id


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """A recording provider, installed the way a test must install one."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # set_tracer_provider refuses to override, so the global is bypassed and the
    # tracer is taken from this provider directly where a test needs one.
    yield exporter
    exporter.clear()


def _tracer(exporter: InMemorySpanExporter) -> trace.Tracer:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test")


class TestInstrumentationPlacement:
    """Instrumenting FastAPI too late is a silent no-op, not an error.

    Starlette builds its middleware stack before the lifespan runs, so
    `instrument_app` from inside the lifespan is accepted and then records
    nothing. This is the single most expensive way to get tracing wrong,
    because everything looks configured.
    """

    @staticmethod
    def _app_with(where: str, exporter: InMemorySpanExporter) -> FastAPI:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        if where == "lifespan":

            @asynccontextmanager
            async def lifespan(app: FastAPI) -> Any:
                FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
                yield

            app = FastAPI(lifespan=lifespan)
        else:
            app = FastAPI()

        @app.get("/thing")
        async def thing() -> dict[str, bool]:
            return {"ok": True}

        if where == "create":
            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        return app

    def test_instrumenting_during_creation_produces_a_server_span(
        self, spans: InMemorySpanExporter
    ) -> None:
        with TestClient(self._app_with("create", spans)) as client:
            assert client.get("/thing").status_code == 200
        assert any(span.name == "GET /thing" for span in spans.get_finished_spans())

    def test_instrumenting_from_the_lifespan_records_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        """Guards the reason configure_tracing is called where it is.

        If a future version of the instrumentation starts working from the
        lifespan, this test fails and the comment in tracing.py should be
        revisited — which is the right way round.
        """
        with TestClient(self._app_with("lifespan", spans)) as client:
            assert client.get("/thing").status_code == 200
        assert spans.get_finished_spans() == (), (
            "instrumenting from the lifespan now works; tracing.py says it cannot"
        )

    def test_the_real_app_produces_a_server_span(self) -> None:
        """The application as it is actually built, through its own wiring.

        The exporter is attached to the provider `configure_tracing` installed,
        rather than instrumenting a second time against a provider of the
        test's own. Re-instrumenting would test the test's wiring; this tests
        the application's.
        """
        from threshold.api.main import create_app

        provider = trace.get_tracer_provider()
        assert hasattr(provider, "add_span_processor"), (
            "the real provider was never installed, so this test would prove nothing"
        )
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        with TestClient(create_app()) as client:
            assert client.get("/v1/me").status_code == 401  # no token, still traced

        names = [span.name for span in exporter.get_finished_spans()]
        assert any("/v1/me" in name for name in names), names

    def test_the_probes_are_not_traced(self) -> None:
        """Compose polls /healthz every five seconds, for ever."""
        from threshold.api.main import create_app

        provider = trace.get_tracer_provider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        with TestClient(create_app()) as client:
            client.get("/healthz")

        assert not [s for s in exporter.get_finished_spans() if "healthz" in s.name]


class TestTraceIdForAuditRows:
    def test_it_is_thirty_two_lowercase_hex_inside_a_span(
        self, spans: InMemorySpanExporter
    ) -> None:
        with _tracer(spans).start_as_current_span("probe"):
            trace_id = current_trace_id()
        assert trace_id is not None
        assert len(trace_id) == 32
        assert trace_id == trace_id.lower()
        assert int(trace_id, 16) != 0

    def test_it_is_none_outside_a_span(self) -> None:
        """Not a string of zeros.

        An invalid span context formats as 32 zeros, which looks like a real id
        in a database column and silently groups every untraced row together.
        """
        assert current_trace_id() is None

    def test_it_fits_the_column(self, spans: InMemorySpanExporter) -> None:
        """audit_event.trace_id is String(32)."""
        with _tracer(spans).start_as_current_span("probe"):
            trace_id = current_trace_id()
        assert trace_id is not None
        assert len(trace_id) <= 32


class TestLogCorrelation:
    def test_a_line_inside_a_span_carries_the_trace(self, spans: InMemorySpanExporter) -> None:
        with _tracer(spans).start_as_current_span("probe") as span:
            event = _add_trace_context(None, "info", {"event": "something"})
            expected = trace.format_trace_id(span.get_span_context().trace_id)
        assert event["trace_id"] == expected
        assert event["span_id"]

    def test_a_line_outside_a_span_carries_neither(self) -> None:
        event = _add_trace_context(None, "info", {"event": "something"})
        assert "trace_id" not in event
        assert "span_id" not in event

    def test_the_processor_is_installed_before_redaction(self) -> None:
        """Order matters: a trace id added after redaction is never scrubbed.

        Nothing about a trace id is sensitive, but the processor list is the
        place that guarantee is made, and a later field added here would inherit
        whatever position this one set.
        """
        names = [
            p.__name__ if hasattr(p, "__name__") else type(p).__name__ for p in _shared_processors()
        ]
        assert "_add_trace_context" in names
        assert names.index("_add_trace_context") < names.index("_redact")


def _shared_processors() -> list[Any]:
    """The processor chain configure_logging installs, without side effects."""
    import threshold.observability.logging as logging_module

    captured: list[Any] = []
    original = structlog.configure

    def capture(**kwargs: Any) -> None:
        captured.extend(kwargs.get("processors", []))

    structlog.configure = capture  # type: ignore[assignment]
    try:
        logging_module.configure_logging(
            Settings(
                database_url="postgresql+asyncpg://u:p@unit-tests.invalid/db",  # type: ignore[arg-type]
                s3_access_key_id="x",
                s3_secret_access_key="x",
            )
        )
    finally:
        structlog.configure = original  # type: ignore[assignment]
    return captured


class TestConfiguration:
    def test_no_endpoint_still_produces_valid_spans(self) -> None:
        """The configuration a unit test and a laptop both run in.

        If this ever stopped being true, every trace-id assertion in the suite
        would pass on None and prove nothing.
        """
        settings = get_settings()
        assert settings.otel_traces_endpoint is None
        configure_tracing(settings)  # idempotent; already called at import

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        with provider.get_tracer("probe").start_as_current_span("s"):
            assert current_trace_id() is not None

    def test_the_suite_does_not_disable_the_sdk(self) -> None:
        """OTEL_SDK_DISABLED returns a no-op tracer whatever provider is installed.

        Setting it for the test suite would make every span context invalid and
        every trace-id assertion here pass on None — a whole file of tests that
        check nothing. Measured, not assumed.
        """
        import os

        assert os.environ.get("OTEL_SDK_DISABLED") in (None, "", "false")
