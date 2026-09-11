"""The request id, and the trace id it is no longer mistaken for.

An inbound header is echoed back to the caller and bound to every log line for
the request. Accepting it verbatim meant a caller controlled both, and an
over-long value surfaced as a database error rather than on the request that
caused it -- so it is bounded and replaced rather than rejected.

It used to be written to `audit_event.trace_id` as well, because before M5 it
was the only correlation id that existed. It is not written there any more: a
caller-chosen string cannot be a W3C trace id, and the column has always
promised to be one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace

from threshold.api.main import REQUEST_ID_HEADER, _accept_request_id
from threshold.services.audit import current_trace_id


class TestAcceptance:
    def test_a_conforming_id_is_kept(self) -> None:
        assert _accept_request_id("smoke-test-id") == "smoke-test-id"
        assert _accept_request_id("integration-test-id") == "integration-test-id"

    def test_a_generated_id_conforms_to_its_own_rule(self) -> None:
        """Otherwise the service produces ids it would itself reject."""
        generated = _accept_request_id(None)
        assert _accept_request_id(generated) == generated
        assert len(generated) == 32

    @pytest.mark.parametrize(
        ("supplied", "why"),
        [
            ("x" * 33, "longer than the trace_id column"),
            ("has spaces", "would break a log line"),
            ("new\nline", "log injection"),
            ("../../etc/passwd", "path characters"),
            ("", "empty"),
            ("a;b", "separator"),
        ],
    )
    def test_a_hostile_id_is_replaced_not_echoed(self, supplied: str, why: str) -> None:
        accepted = _accept_request_id(supplied)
        assert accepted != supplied, why
        assert len(accepted) == 32

    def test_replacement_rather_than_rejection(self) -> None:
        """The request is fine; only the header is not. Refusing it is a worse trade."""
        assert _accept_request_id("!!!") is not None


@pytest.fixture
def app_with_a_failing_route() -> Iterator[TestClient]:
    from threshold.api.main import create_app

    app: FastAPI = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("deliberate")

    # raise_server_exceptions=False so the client returns the 500 the way a real
    # caller would see it, rather than re-raising into the test.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


class TestTheHeaderSurvivesFailure:
    def test_a_500_still_carries_a_request_id(self, app_with_a_failing_route: TestClient) -> None:
        """The response most in need of correlation is the one that had none.

        Assigning the header after `call_next` cannot run when `call_next`
        raises, and Starlette builds its own 500 outside this middleware.
        """
        response = app_with_a_failing_route.get(
            "/boom", headers={REQUEST_ID_HEADER: "boom-probe-id"}
        )
        assert response.status_code == 500
        assert response.headers.get(REQUEST_ID_HEADER) == "boom-probe-id"
        assert response.json()["request_id"] == "boom-probe-id"

    def test_a_successful_request_echoes_it(self, app_with_a_failing_route: TestClient) -> None:
        response = app_with_a_failing_route.get(
            "/healthz", headers={REQUEST_ID_HEADER: "healthy-probe"}
        )
        assert response.headers.get(REQUEST_ID_HEADER) == "healthy-probe"

    def test_context_is_cleared_between_requests(
        self, app_with_a_failing_route: TestClient
    ) -> None:
        """A leaked contextvar would stamp one request's id onto another's rows."""
        app_with_a_failing_route.get("/boom", headers={REQUEST_ID_HEADER: "first"})
        assert current_trace_id() is None


class TestTraceId:
    """The audit column now holds a trace id, not the request id.

    These two used to be the same value, because before M5 the request id was
    the only correlation identifier that existed. They are different things: a
    caller chooses the request id, so it can never be 32 lowercase hex, and
    ARCHITECTURE.md has promised since M0 that audit_event.trace_id is "the same
    id as the distributed trace".
    """

    def test_it_comes_from_the_span(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
        with provider.get_tracer("probe").start_as_current_span("s") as span:
            trace_id = current_trace_id()
        assert trace_id == trace.format_trace_id(span.get_span_context().trace_id)
        assert len(trace_id or "") == 32

    def test_a_bound_request_id_does_not_become_the_trace_id(self) -> None:
        """The regression this replaces: the column used to hold whatever the
        caller sent, so an audit row could claim a trace named "smoke-test-id".
        """
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="smoke-test-id")
        try:
            assert current_trace_id() is None
        finally:
            structlog.contextvars.clear_contextvars()

    def test_it_is_none_outside_a_span(self) -> None:
        """The worker, until the next increment carries a trace across the queue."""
        structlog.contextvars.clear_contextvars()
        assert current_trace_id() is None
