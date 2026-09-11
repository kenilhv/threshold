"""Where a job's span belongs in the trace graph.

The automatic chain continues the producer's trace so that one invoice's audit
rows share one id. A replayed job does not: a human chose to run it again,
possibly days later, and inheriting the dead trace would make today's work
appear to have taken a week.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from threshold.adapters.queue import (
    LINKED_TRACEPARENT_KEY,
    TRACEPARENT_KEY,
    _with_trace_context,
)
from threshold.workers.runtime import _job_context


def _recording_tracer() -> trace.Tracer:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    return provider.get_tracer("test")


class TestProducer:
    def test_a_payload_gains_the_current_trace(self) -> None:
        with _recording_tracer().start_as_current_span("produce") as span:
            payload = _with_trace_context({"document_id": "abc"})
        expected = trace.format_trace_id(span.get_span_context().trace_id)

        assert payload["document_id"] == "abc", "business data survives"
        assert expected in payload[TRACEPARENT_KEY]

    def test_the_callers_dict_is_not_mutated(self) -> None:
        """ingest logs the payload it passed; it must not change underneath."""
        original = {"document_id": "abc"}
        with _recording_tracer().start_as_current_span("produce"):
            _with_trace_context(original)
        assert original == {"document_id": "abc"}

    def test_no_trace_means_no_key(self) -> None:
        """A seed script or a CLI enqueueing outside a request adds nothing."""
        assert _with_trace_context({"document_id": "abc"}) == {"document_id": "abc"}


class TestConsumer:
    def test_a_traceparent_continues_the_producers_trace(self) -> None:
        with _recording_tracer().start_as_current_span("produce") as span:
            payload = _with_trace_context({"document_id": "abc"})
        produced_in = span.get_span_context().trace_id

        context, links = _job_context(payload)
        assert links == []
        assert context is not None
        assert trace.get_current_span(context).get_span_context().trace_id == produced_in

    def test_a_replayed_job_starts_its_own_trace_and_links_back(self) -> None:
        with _recording_tracer().start_as_current_span("produce") as span:
            died_in = _with_trace_context({})[TRACEPARENT_KEY]
        original = span.get_span_context().trace_id

        context, links = _job_context({LINKED_TRACEPARENT_KEY: died_in})
        assert context is None, "a replay must not inherit the dead trace as its parent"
        assert len(links) == 1
        assert links[0].context.trace_id == original

    def test_a_payload_with_neither_is_a_new_trace(self) -> None:
        """Jobs enqueued before this existed still run."""
        assert _job_context({"document_id": "abc"}) == (None, [])

    def test_an_unparseable_traceparent_does_not_raise(self) -> None:
        """A corrupt carrier must not take the worker down.

        W3C extraction returns an empty context rather than failing, so the job
        runs in a new trace. Losing a trace is a reporting problem; refusing to
        process the invoice is a business one.
        """
        context, links = _job_context({LINKED_TRACEPARENT_KEY: "not-a-traceparent"})
        assert (context, links) == (None, [])

        context, links = _job_context({TRACEPARENT_KEY: "not-a-traceparent"})
        assert links == []
        # The context exists but carries no valid span, so the span starts a
        # fresh trace rather than attaching to a fabricated parent.
        assert context is not None
        assert not trace.get_current_span(context).get_span_context().is_valid


class TestKeysAreTransportMetadata:
    def test_they_are_underscore_prefixed(self) -> None:
        """So a handler reading business fields never collides with them."""
        assert TRACEPARENT_KEY.startswith("_")
        assert LINKED_TRACEPARENT_KEY.startswith("_")

    def test_handlers_ignore_them(self) -> None:
        """The extract handler reads document_id and nothing else."""
        from threshold.workers.handlers import _document_id_from

        payload: dict[str, Any] = {
            "document_id": "1e8f2a3d-0000-4000-8000-000000000000",
            TRACEPARENT_KEY: "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        }
        assert str(_document_id_from(payload)) == payload["document_id"]
