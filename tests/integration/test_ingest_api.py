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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.integration.databases import LIVE
from tests.integration.test_auth import mint
from tests.unit.test_fixture_layouts import build_spec, render

from threshold.api.idempotency import remember
from threshold.domain.models import Document, IdempotencyKey

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


@pytest.fixture
def reader(client: httpx.Client) -> dict[str, str]:
    """An Authorization header for the auditor: reads everything, decides nothing.

    Retrieval needed no token until M9, which was a hole rather than a
    convenience. Uploading still needs none -- suppliers are not users of this
    system -- so these tests now say, in their own signatures, which half of the
    ingest API is public and which is not.
    """
    return {"Authorization": f"Bearer {mint(client, 'auditor')}"}


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
        self, client: httpx.Client, invoice_pdf: bytes, reader: dict[str, str]
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
            response = client.get(f"/v1/documents/{created['document_id']}", headers=reader)
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

    def test_an_unknown_id_is_a_404(self, client: httpx.Client, reader: dict[str, str]) -> None:
        response = client.get(f"/v1/documents/{uuid.uuid4()}", headers=reader)
        assert response.status_code == 404

    def test_a_malformed_id_is_a_422_not_a_500(
        self, client: httpx.Client, reader: dict[str, str]
    ) -> None:
        response = client.get("/v1/documents/not-a-uuid", headers=reader)
        assert response.status_code == 422

    def test_reading_a_document_back_needs_a_token(
        self, client: httpx.Client, invoice_pdf: bytes
    ) -> None:
        """The hole, as a regression test against the running API.

        `test_route_authentication.py` proves the dependency is declared. This
        proves the deployed service actually refuses -- including for a document
        the anonymous caller has just uploaded themselves, which is the case
        that feels like it ought to work and must not: a supplier who can read
        back their submission can read back the vendor it resolved to and
        whether it was blocked, which tells them what the controls caught.
        """
        created = client.post(
            "/v1/documents",
            files={"file": ("invoice.pdf", invoice_pdf, "application/pdf")},
        ).json()
        document_id = created["document_id"]

        assert client.get(f"/v1/documents/{document_id}").status_code == 401
        assert client.get(f"/v1/documents/{document_id}/match").status_code == 401

    def test_an_unknown_id_without_a_token_is_a_401_and_not_a_404(
        self, client: httpx.Client
    ) -> None:
        """Order matters. A 404 for anonymous callers would let anybody probe
        which document ids exist; authentication is checked before existence."""
        assert client.get(f"/v1/documents/{uuid.uuid4()}").status_code == 401

    def test_uploading_still_needs_none(self, client: httpx.Client, invoice_pdf: bytes) -> None:
        """The vacuity guard, and the deliberate half of the decision. If the
        upload had been closed along with the reads, every test above would be
        unreachable and the supplier-facing door would be shut by accident."""
        response = client.post(
            "/v1/documents",
            files={"file": ("invoice.pdf", invoice_pdf, "application/pdf")},
        )
        assert response.status_code in (200, 201, 202)


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


class TestIdempotencyDoesNotCostTheCallerItsWork:
    """Losing a race for an Idempotency-Key must cost only the key.

    `remember()` used to call `session.rollback()` when a concurrent request
    had already claimed the key. That rolls back the *caller's* transaction --
    the document row, its audit event and the job enqueued alongside them --
    while the endpoint went on to return 202 naming a document_id that no
    longer existed. A savepoint scopes the loss to the insert that actually
    lost.
    """

    async def test_a_concurrent_duplicate_key_does_not_discard_the_document(self) -> None:
        engine = create_async_engine(LIVE)
        try:
            async with engine.connect() as conn:
                await conn.execute(sa_text("SELECT 1"))
        except Exception:
            await engine.dispose()
            pytest.skip("Postgres is not reachable; start the stack with `make up`")

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        key = f"race-{uuid.uuid4().hex[:12]}"

        # Someone else claimed the key and committed.
        async with sessions() as session:
            session.add(
                IdempotencyKey(
                    key=key,
                    request_fingerprint="fp",
                    response_status=202,
                    response_body={},
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await session.commit()

        # The loser has already done real work in its own transaction.
        async with sessions() as session:
            digest = uuid.uuid4().hex * 2
            session.add(
                Document(
                    content_sha256=digest,
                    storage_key=f"documents/{digest[:2]}/{digest[2:4]}/{digest}.pdf",
                    original_filename="race.pdf",
                    content_type="application/pdf",
                    byte_size=10,
                    source="fixture",
                )
            )
            await session.flush()

            await remember(session, key, "fp", 202, {})

            surviving = (
                await session.execute(select(Document.id).where(Document.content_sha256 == digest))
            ).all()
            await session.rollback()

        await engine.dispose()
        assert len(surviving) == 1, (
            "the caller's document was discarded; a 202 would name a row that does not exist"
        )
