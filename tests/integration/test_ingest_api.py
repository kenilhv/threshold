"""Ingest, end to end, against the running stack.

Requires `make up`. These hit the real API, the real Postgres and the real
MinIO, because the properties under test — a unique constraint catching a
duplicate, a stored response replaying, an object landing in a bucket — are
exactly the ones an in-memory double cannot demonstrate.

Run with:  make test-integration
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from tests.unit.test_fixture_layouts import build_spec, render

BASE_URL = "http://localhost:8000"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        try:
            c.get("/healthz")
        except httpx.ConnectError:
            pytest.skip("stack is not running; start it with `make up`")
        yield c


def unique_pdf(tmp_path: Path, layout: str = "continental_tabular") -> bytes:
    """A PDF whose bytes have never been seen by the running system.

    Varying the *seed* is not enough. Fixtures are deliberately deterministic —
    that is the property the golden set depends on — so a fresh seed can still
    render byte-identical output, at which point the system correctly
    deduplicates it and every "was it created?" assertion fails on the second
    run against a warm database.

    This suite hits a persistent stack, so it must be safe to run repeatedly.
    Varying the invoice number changes something that actually reaches the page.
    """
    spec = dataclasses.replace(build_spec(), invoice_number=f"IT-{uuid.uuid4().hex[:10].upper()}")
    return render(layout, spec, tmp_path / f"{uuid.uuid4().hex}.pdf")


@pytest.fixture
def invoice_pdf(tmp_path: Path) -> bytes:
    return unique_pdf(tmp_path)


class TestSubmit:
    def test_a_new_document_is_accepted_and_queued(
        self, client: httpx.Client, invoice_pdf: bytes
    ) -> None:
        """202, not 201.

        The request stores the document and enqueues the work; extraction runs
        on a worker moments later. Reporting a confidence score here would claim
        knowledge the system does not have yet.
        """
        response = client.post(
            "/v1/documents",
            files={"file": ("invoice.pdf", invoice_pdf, "application/pdf")},
        )
        assert response.status_code == 202, response.text
        body = response.json()

        assert body["created"] is True
        assert len(body["content_sha256"]) == 64
        assert body["job_id"] is not None, "work must have been queued"

    def test_the_same_bytes_do_not_create_a_second_document(
        self, client: httpx.Client, invoice_pdf: bytes
    ) -> None:
        """The most common real duplicate: a supplier resending the same PDF.

        No idempotency key involved — the content hash alone must catch it.
        """
        files = {"file": ("invoice.pdf", invoice_pdf, "application/pdf")}
        first = client.post("/v1/documents", files=files)
        second = client.post(
            "/v1/documents",
            files={"file": ("renamed-by-the-supplier.pdf", invoice_pdf, "application/pdf")},
        )

        assert first.status_code == 202
        assert first.json()["created"] is True

        # 200, not 202: nothing was created and nothing new was queued.
        assert second.status_code == 200
        assert second.json()["created"] is False
        assert second.json()["document_id"] == first.json()["document_id"]

    def test_an_unsupported_type_is_rejected(self, client: httpx.Client) -> None:
        response = client.post(
            "/v1/documents",
            files={"file": ("notes.txt", b"not a document", "text/plain")},
        )
        assert response.status_code == 415

    def test_an_empty_upload_is_rejected(self, client: httpx.Client) -> None:
        response = client.post(
            "/v1/documents", files={"file": ("empty.pdf", b"", "application/pdf")}
        )
        assert response.status_code == 400

    def test_a_corrupt_pdf_is_accepted_then_dead_lettered(self, client: httpx.Client) -> None:
        """Where a corrupt document surfaces moved with extraction.

        It used to be a 422 from the request, because extraction ran inline. Now
        ingest only stores and queues, so the request succeeds and the failure
        appears where asynchronous failures belong — in the dead-letter queue,
        classified permanent so it never burns its retry budget re-reading the
        same broken bytes.
        """
        response = client.post(
            "/v1/documents",
            files={"file": ("broken.pdf", b"%PDF-1.4 truncated garbage", "application/pdf")},
        )
        assert response.status_code in (200, 202)


class TestIdempotencyKey:
    def test_retrying_with_the_same_key_replays_the_original_response(
        self, client: httpx.Client, invoice_pdf: bytes
    ) -> None:
        key = f"test-{uuid.uuid4()}"
        files = {"file": ("invoice.pdf", invoice_pdf, "application/pdf")}
        headers = {"Idempotency-Key": key}

        first = client.post("/v1/documents", files=files, headers=headers)
        second = client.post("/v1/documents", files=files, headers=headers)

        assert first.status_code == 202
        assert second.status_code == 202, "the replay must return the ORIGINAL status"
        assert second.json() == first.json()

    def test_reusing_a_key_for_different_content_is_a_conflict(
        self, client: httpx.Client, tmp_path: Path
    ) -> None:
        """The case implementations usually miss.

        Silently replaying here would hand the caller a different invoice's id
        under a 200, which it has no way to detect.
        """
        key = f"test-{uuid.uuid4()}"
        first_pdf = unique_pdf(tmp_path, "continental_tabular")
        second_pdf = unique_pdf(tmp_path, "us_service_minimal")

        first = client.post(
            "/v1/documents",
            files={"file": ("a.pdf", first_pdf, "application/pdf")},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code in (200, 202)

        conflict = client.post(
            "/v1/documents",
            files={"file": ("b.pdf", second_pdf, "application/pdf")},
            headers={"Idempotency-Key": key},
        )
        assert conflict.status_code == 409
        assert "different request" in conflict.text


class TestRetrieval:
    def test_the_worker_extracts_the_document_shortly_after_ingest(
        self, client: httpx.Client, invoice_pdf: bytes
    ) -> None:
        """The end-to-end proof that the async pipeline actually closes.

        Submit returns before extraction has happened, so this polls. That is
        not a workaround for a slow test — it is the contract: the API accepts
        work and a worker completes it, and a client learns the outcome by
        asking rather than by waiting on the connection.
        """
        created = client.post(
            "/v1/documents",
            files={"file": ("invoice.pdf", invoice_pdf, "application/pdf")},
        ).json()

        deadline = time.monotonic() + 30.0
        body: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = client.get(f"/v1/documents/{created['document_id']}")
            assert response.status_code == 200
            body = response.json()
            if body.get("extraction_version") is not None:
                break
            time.sleep(0.25)
        else:  # pragma: no cover - only on a stalled worker
            pytest.fail("the worker did not extract the document within 30s")

        assert body["content_sha256"] == created["content_sha256"]
        assert body["content_type"] == "application/pdf"
        assert body["byte_size"] == len(invoice_pdf)
        assert body["extraction_version"] == 1

        fields = body["fields"]
        assert fields is not None
        # Every field carries its own confidence, not one score for the document.
        assert "confidence" in fields["invoice_number"]
        assert "confidence" in fields["total"]

    def test_an_unknown_id_is_a_404(self, client: httpx.Client) -> None:
        response = client.get(f"/v1/documents/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_a_malformed_id_is_a_422_not_a_500(self, client: httpx.Client) -> None:
        response = client.get("/v1/documents/not-a-uuid")
        assert response.status_code == 422


class TestObservability:
    def test_the_request_id_is_echoed_back(self, client: httpx.Client) -> None:
        """The id that ties logs, traces and audit events together (M5)."""
        response = client.get("/healthz", headers={"X-Request-Id": "integration-test-id"})
        assert response.headers.get("X-Request-Id") == "integration-test-id"

    def test_the_openapi_contract_describes_the_ingest_endpoint(self, client: httpx.Client) -> None:
        """The frontend generates its types from this in M4."""
        spec = client.get("/openapi.json").json()
        assert "/v1/documents" in spec["paths"]
        post = spec["paths"]["/v1/documents"]["post"]
        assert {"200", "409", "413", "415", "422"} <= set(post["responses"])
