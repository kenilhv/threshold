"""Document storage, behind a port.

Deliberately written against the S3 API using boto3 rather than a MinIO client,
because that makes the local-to-cloud difference exactly one setting — the
endpoint URL — instead of a rewrite. The interface exists so that claim is
testable: ``InMemoryObjectStore`` runs the whole ingest path in unit tests with
no container, and the integration suite runs the same assertions against MinIO.

Keys are derived from the content hash, so an object's location is a pure
function of its bytes. Re-uploading the same document cannot fork into two
objects, and a key can be recomputed from a document row without storing a
mapping.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from threshold.config.settings import Settings

log = structlog.get_logger(__name__)


class ObjectStoreError(RuntimeError):
    """Storage failed in a way the caller cannot fix by retrying differently."""


@runtime_checkable
class ObjectStore(Protocol):
    """The only storage surface the application depends on."""

    async def put(self, key: str, body: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def reachable(self) -> None:
        """Raise ``ObjectStoreError`` if documents could not be stored right now.

        Separate from ``exists`` because ``exists`` cannot answer this. A
        ``head_object`` against a bucket that does not exist returns the same
        404 as a key that does not exist, so "the object is missing" and "the
        bucket is missing" are one answer -- fine for the question ``exists``
        asks, useless for readiness, where the second means every upload will
        fail and the first means nothing at all.
        """
        ...


def storage_key_for(content_sha256: str, extension: str) -> str:
    """Content-addressed, fanned out by prefix.

    The two-level prefix keeps any single listing small and spreads writes across
    partitions — on S3 a flat namespace with a shared prefix is a throttling
    story, and it costs nothing to avoid here.
    """
    if len(content_sha256) != 64:
        raise ValueError("content_sha256 must be a 64-character hex digest")
    safe_ext = extension.lstrip(".").lower() or "bin"
    return f"documents/{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.{safe_ext}"


class S3ObjectStore:
    """boto3 against S3 or anything that speaks its API.

    boto3 is synchronous, so every call is pushed to a thread rather than
    blocking the event loop. That is the honest cost of using the SDK the cloud
    version needs; the alternative is a second client library whose behaviour
    diverges from production.
    """

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket_documents
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(
                signature_version="s3v4",
                # MinIO requires path style; S3 accepts it. One less difference.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        def _put() -> None:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
            )

        try:
            await asyncio.to_thread(_put)
        except ClientError as exc:
            raise ObjectStoreError(f"failed to store {key}") from exc
        log.debug("object_store.put", key=key, bytes=len(body))

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data = response["Body"].read()
            if not isinstance(data, bytes):  # pragma: no cover - defensive
                raise ObjectStoreError(f"unexpected body type for {key}")
            return data

        try:
            return await asyncio.to_thread(_get)
        except ClientError as exc:
            raise ObjectStoreError(f"failed to read {key}") from exc

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise
            return True

        try:
            return await asyncio.to_thread(_head)
        except ClientError as exc:
            raise ObjectStoreError(f"failed to stat {key}") from exc

    async def reachable(self) -> None:
        """One HEAD against the bucket itself. No object is read or written.

        `head_bucket` rather than listing: a list is O(objects) at the server
        and this runs on every readiness probe, which compose issues every five
        seconds for as long as the stack is up.
        """

        def _head() -> None:
            self._client.head_bucket(Bucket=self._bucket)

        try:
            await asyncio.to_thread(_head)
        except (ClientError, EndpointConnectionError, BotoCoreError) as exc:
            raise ObjectStoreError(f"bucket {self._bucket} is not usable") from exc


class InMemoryObjectStore:
    """For unit tests. Same contract, no container.

    Its existence is what keeps ``ObjectStore`` an honest boundary: if the
    application ever reaches for an S3-only feature, this stops compiling and
    the leak is caught at the seam rather than discovered in a cloud migration.
    """

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        self._objects[key] = (body, content_type)

    async def get(self, key: str) -> bytes:
        if key not in self._objects:
            raise ObjectStoreError(f"no such object: {key}")
        return self._objects[key][0]

    async def exists(self, key: str) -> bool:
        return key in self._objects

    @property
    async def reachable(self) -> None:
        """Always. A dict cannot be unreachable."""
        return None

    def keys(self) -> list[str]:
        return sorted(self._objects)
