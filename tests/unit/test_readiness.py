"""What readiness is allowed to say, and what it must never do.

Until M5 this endpoint checked Postgres and Redis. Nothing in the application
has ever used Redis; MinIO and Keycloak are used and were not checked. So with
only Keycloak down the API reported itself ready while every authenticated call
returned 503 — and it had no tests, which is why that survived four milestones.

The interesting assertions here are the ones about what readiness *refuses* to
do. A probe is not a status page. Every dependency it checks is a dependency
that can take the instance out of rotation, and for shared infrastructure that
is usually worse than the fault it is reporting.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from threshold.adapters.object_store import ObjectStoreError
from threshold.api.auth import jwks_cache
from threshold.api.dependencies import get_object_store, get_session
from threshold.api.main import create_app


class _Answer:
    def __init__(self, **row: Any) -> None:
        self._row = SimpleNamespace(**row)

    def one(self) -> SimpleNamespace:
        return self._row


class _HealthySession:
    """Enough of an AsyncSession for the one query readiness makes: who am I,
    do I outrank my grants, and which of the grants I need am I missing."""

    def __init__(
        self,
        *,
        role: str = "threshold_app",
        outranks_grants: bool = False,
        missing: tuple[str, ...] = (),
    ) -> None:
        self._answer = _Answer(role=role, outranks_grants=outranks_grants, missing=list(missing))

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Answer:
        return self._answer


class _BrokenSession:
    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionRefusedError("postgres is not accepting connections")


class _HealthyStore:
    async def reachable(self) -> None:
        return None


class _BrokenStore:
    async def reachable(self) -> None:
        raise ObjectStoreError("bucket threshold-documents is not usable")


def _client(*, session: object, store: object) -> TestClient:
    app = create_app()

    async def _session() -> Any:
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_object_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _forget_signing_keys() -> Iterator[None]:
    """The cache is a process global; a test that fills it would leak."""
    jwks_cache().clear()
    yield
    jwks_cache().clear()


def _named(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {dependency["name"]: dependency for dependency in body["dependencies"]}


class TestReadinessChecksWhatTheServiceUses:
    def test_healthy_dependencies_are_ready(self) -> None:
        response = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz")
        assert response.status_code == 200
        assert response.json()["ready"] is True

    def test_postgres_is_required(self) -> None:
        response = _client(session=_BrokenSession(), store=_HealthyStore()).get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        assert _named(body)["postgres"]["ok"] is False

    def test_the_object_store_is_required(self) -> None:
        """New in M5, and the reason is D-006.

        Ingest stores the object before writing the row that describes it, so an
        instance whose bucket is unreachable cannot accept a document at all. It
        was not checked for four milestones.
        """
        response = _client(session=_HealthySession(), store=_BrokenStore()).get("/readyz")
        assert response.status_code == 503
        assert _named(response.json())["object_store"]["ok"] is False

    def test_the_failing_dependency_is_named(self) -> None:
        """A 503 that does not say which dependency is a 503 someone has to
        reproduce before they can act on it."""
        body = _client(session=_HealthySession(), store=_BrokenStore()).get("/readyz").json()
        assert _named(body)["object_store"]["detail"] == "ObjectStoreError"

    def test_redis_is_gone(self) -> None:
        """It was checked for four milestones and never used once."""
        body = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz").json()
        assert "redis" not in _named(body)


class TestReachingPostgresIsNotBeingAllowedToUseIt:
    """The review of migration 0010 restored a schema without its grants and
    watched `/readyz` report ready above a worker that could claim nothing."""

    def test_a_role_missing_a_grant_is_not_ready(self) -> None:
        session = _HealthySession(missing=("job:INSERT", "document:INSERT"))
        response = _client(session=session, store=_HealthyStore()).get("/readyz")
        assert response.status_code == 503
        postgres = _named(response.json())["postgres"]
        assert postgres["ok"] is False
        assert "document:INSERT" in postgres["detail"]
        assert "job:INSERT" in postgres["detail"]
        assert "threshold_app" in postgres["detail"]

    def test_each_privilege_is_asked_for_separately(self) -> None:
        """`has_table_privilege(role, t, 'SELECT,INSERT')` is true when *either*
        is held. A probe written that way reports green for a table that kept
        SELECT and lost INSERT -- verified against Postgres 16 on the read-only
        `vendor` table -- so the needs are one privilege each, by construction."""
        from threshold.api.routes.health import _NEEDS

        assert _NEEDS, "nothing is asked for"
        for table, privilege in _NEEDS:
            assert "," not in privilege, f"{table}: '{privilege}' is an any-of test"
            assert privilege in {"SELECT", "INSERT", "UPDATE", "DELETE"}

    def test_a_superuser_is_reported_and_does_not_gate_a_laptop(self) -> None:
        session = _HealthySession(role="threshold", outranks_grants=True)
        response = _client(session=session, store=_HealthyStore()).get("/readyz")
        role = _named(response.json())["database_role"]
        assert role["ok"] is False
        assert role["required"] is False, "tests connect in-process as the owner on purpose"
        assert "superuser" in role["detail"]
        assert response.status_code == 200

    def test_a_superuser_gates_routing_everywhere_else(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from threshold.config.settings import get_settings

        monkeypatch.setenv("THRESHOLD_ENVIRONMENT", "production")
        get_settings.cache_clear()
        try:
            session = _HealthySession(role="threshold", outranks_grants=True)
            response = _client(session=session, store=_HealthyStore()).get("/readyz")
            role = _named(response.json())["database_role"]
        finally:
            monkeypatch.delenv("THRESHOLD_ENVIRONMENT")
            get_settings.cache_clear()
        assert role["required"] is True
        assert response.status_code == 503

    def test_the_restricted_role_is_ready_and_named(self) -> None:
        response = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz")
        assert response.status_code == 200
        role = _named(response.json())["database_role"]
        assert role == {
            "name": "database_role",
            "ok": True,
            "required": False,
            "detail": "threshold_app",
        }

    def test_an_unreachable_postgres_says_so_once(self) -> None:
        body = _client(session=_BrokenSession(), store=_HealthyStore()).get("/readyz").json()
        assert _named(body)["postgres"]["ok"] is False
        assert _named(body)["database_role"]["required"] is False


class TestTheIdentityProviderNeverGatesRouting:
    """The decision this endpoint exists to record.

    Checking Keycloak would be the obvious completion of "check every
    dependency", and it would be wrong twice over: an IdP blip would fail
    readiness on every instance of every service simultaneously, leaving a load
    balancer nowhere to route; and it would be false, because an instance
    holding cached signing keys keeps validating tokens perfectly well while the
    IdP is unreachable.
    """

    def test_an_instance_with_no_signing_keys_is_still_ready(self) -> None:
        response = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz")
        body = response.json()

        assert _named(body)["identity"]["ok"] is False, (
            "with no keys cached this instance genuinely cannot validate a new token"
        )
        assert response.status_code == 200
        assert body["ready"] is True, (
            "identity must never gate routing; see the module docstring in health.py"
        )

    def test_identity_says_it_is_not_required(self) -> None:
        """On the wire, so the answer to "why is this ready" is in the response."""
        body = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz").json()
        assert _named(body)["identity"]["required"] is False

    def test_the_required_flag_is_set_on_the_ones_that_are(self) -> None:
        """The other half. A `required` that is False everywhere would satisfy
        the test above and make readiness incapable of ever failing."""
        body = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz").json()
        named = _named(body)
        assert named["postgres"]["required"] is True
        assert named["object_store"]["required"] is True

    def test_it_reports_the_keys_it_holds(self) -> None:
        """The number an operator wants during an IdP incident: which instances
        can still authenticate, and which will 503 on their next unknown key."""
        cache = jwks_cache()
        cache._keys = {"kid-1": object(), "kid-2": object()}  # type: ignore[dict-item]
        cache._last_fetch = 1.0

        body = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz").json()
        identity = _named(body)["identity"]
        assert identity["ok"] is True
        assert identity["detail"] is not None
        assert "2 signing keys" in identity["detail"]

    def test_the_probe_never_calls_the_identity_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Compose polls this every five seconds for ever.

        A readiness endpoint that fetches JWKS is a denial of service against
        the IdP, aimed by the thing that is supposed to be watching it. The
        cache is read, never refreshed.
        """
        import threshold.api.auth as auth

        def _explode(url: str) -> dict[str, Any]:
            raise AssertionError(f"readiness fetched {url}")

        monkeypatch.setattr(auth, "_fetch", _explode)
        response = _client(session=_HealthySession(), store=_HealthyStore()).get("/readyz")
        assert response.status_code == 200


class TestLivenessTouchesNothing:
    """A liveness probe that checks a dependency restarts every healthy
    instance during that dependency's outage, which is how a recoverable blip
    becomes a full outage."""

    def test_it_is_ok_with_every_dependency_down(self) -> None:
        response = _client(session=_BrokenSession(), store=_BrokenStore()).get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_it_names_the_service(self) -> None:
        """So a probe against the wrong container says so."""
        body = _client(session=_HealthySession(), store=_HealthyStore()).get("/healthz").json()
        assert body["service"] == "threshold-api"
