"""Liveness and readiness.

These are different questions and conflating them causes real outages: a
liveness probe that checks the database will restart every healthy pod during a
database failover, turning a recoverable dependency blip into a full outage.

  /healthz   is this process alive?            no dependencies, never fails slow
  /readyz    should traffic be routed here?    checks what this instance needs

**What readiness checks, and what it deliberately does not.**

It checks Postgres and the object store, because the API cannot do its job
without either: an invoice arrives as bytes that must be stored before the row
that describes them exists, and everything else is a query.

It does *not* check Keycloak, and that is the decision this endpoint exists to
record. Until M5 it checked Redis — which nothing in this application uses — and
skipped MinIO and Keycloak, which it does. The obvious fix was to check all
three. That would have been wrong for the identity provider, for two reasons.

The first is what readiness means. It is not "is everything healthy"; it is
"should a load balancer send this instance a request". An identity provider is
shared, so an IdP blip fails readiness on every instance of every service at the
same instant and there is nowhere left to route: a degraded system becomes a
total outage, caused by the probe rather than by the fault.

The second is that it would be false. Signing keys are cached, so an instance
that has fetched them keeps validating tokens perfectly well while Keycloak is
unreachable. Reporting such an instance as unready would take a working server
out of rotation.

**Reaching Postgres is not the same as being allowed to use it.**

Until M9 the database check was `SELECT 1`, and until M9 that was sufficient:
the application connected as a superuser, so a connection that opened could do
anything. Since migration 0010 it connects as `threshold_app`, which holds only
what it was granted, and SQLSTATE 42501 became possible for the first time. The
review of that migration restored a schema without its ACLs and watched this
endpoint report ready, for ever, above a worker that could not claim a job --
`SELECT 1` needs no grant.

So the check asks for the grants this process cannot work without, one privilege
at a time: `has_table_privilege(role, table, 'SELECT,INSERT')` is true when
*either* is held, which is the wrong question and an easy one to ask by mistake.

It also reports what kind of role it is. A superuser or BYPASSRLS role outranks
every grant and every policy in this schema, so an instance holding one is
reported on the wire. That gates routing in staging and production and not on a
laptop or in CI, where tests run in-process as the schema owner on purpose.

So the identity provider is reported and never gates routing: how many keys this
instance holds and how long ago it learned them. That is the number an operator
actually wants during an IdP incident — it says which instances can still
authenticate and which will start returning 503 on their next unknown key. The
probe reads cached state and never calls Keycloak; compose polls this every five
seconds for ever, and a readiness endpoint that hammers the IdP is its own
outage.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.object_store import ObjectStore
from threshold.api.auth import jwks_cache
from threshold.api.dependencies import SessionDep, get_object_store
from threshold.config.settings import Settings, get_settings

router = APIRouter(tags=["health"])

_DEPENDENCY_TIMEOUT_SECONDS = 2.0


class Liveness(BaseModel):
    status: Literal["ok"]
    service: str


class DependencyStatus(BaseModel):
    name: str
    ok: bool
    #: Whether ``ok`` being false should stop traffic being routed here.
    #:
    #: On the wire rather than implied, so the answer to "why is this instance
    #: ready when something in the list is not ok" is in the response instead of
    #: in someone's memory of this module.
    required: bool
    detail: str | None = None


class Readiness(BaseModel):
    ready: bool
    dependencies: list[DependencyStatus]


@router.get("/healthz", response_model=Liveness)
async def liveness(settings: Settings = Depends(get_settings)) -> Liveness:
    return Liveness(status="ok", service=settings.service_name)


#: What the API cannot do its job without, as (table, privilege). Not the whole
#: policy -- `test_least_privilege.py` owns that -- but enough that a schema
#: restored without its grants, or a role that was never granted anything, is
#: caught here rather than by the first user to upload an invoice.
_NEEDS: tuple[tuple[str, str], ...] = (
    ("document", "INSERT"),
    ("idempotency_key", "INSERT"),
    ("job", "INSERT"),
    ("audit_event", "INSERT"),
    ("approval_decision", "INSERT"),
    ("match_result", "SELECT"),
    ("vendor", "SELECT"),
)

_ROLE_AND_GRANTS = text(
    """
    SELECT r.rolname AS role,
           (r.rolsuper OR r.rolbypassrls) AS outranks_grants,
           ARRAY(
               SELECT need
                 FROM unnest(CAST(:needs AS text[])) AS need
                WHERE CASE
                        WHEN to_regclass('public.' || split_part(need, ':', 1)) IS NULL THEN true
                        ELSE NOT has_table_privilege(
                            current_user,
                            'public.' || split_part(need, ':', 1),
                            split_part(need, ':', 2)
                        )
                      END
           ) AS missing
      FROM pg_roles r
     WHERE r.rolname = current_user
    """
)


async def _check_database(
    session: AsyncSession, settings: Settings
) -> tuple[DependencyStatus, DependencyStatus]:
    """Whether Postgres answers, whether this role may do its work, and what it is."""
    try:
        async with asyncio.timeout(_DEPENDENCY_TIMEOUT_SECONDS):
            row = (
                await session.execute(
                    _ROLE_AND_GRANTS, {"needs": [f"{table}:{priv}" for table, priv in _NEEDS]}
                )
            ).one()
    except Exception as exc:
        unknown = DependencyStatus(
            name="database_role", ok=False, required=False, detail="postgres did not answer"
        )
        return (
            DependencyStatus(name="postgres", ok=False, required=True, detail=type(exc).__name__),
            unknown,
        )

    missing = sorted(row.missing)
    postgres = DependencyStatus(
        name="postgres",
        ok=not missing,
        required=True,
        detail=(
            f"role {row.role} lacks {', '.join(missing)}; migrations grant these"
            if missing
            else None
        ),
    )
    role = DependencyStatus(
        name="database_role",
        ok=not row.outranks_grants,
        # On a laptop and in CI the in-process tests connect as the schema owner
        # deliberately. Anywhere else, a runtime that outranks its own grants is
        # a misconfiguration worth taking the instance out of rotation for.
        required=not settings.is_local,
        detail=(
            f"{row.role} is a superuser or bypasses row-level security: no grant and no "
            "policy in this schema applies to it"
            if row.outranks_grants
            else row.role
        ),
    )
    return postgres, role


async def _check_object_store(store: ObjectStore) -> DependencyStatus:
    """A HEAD against the bucket, which is what ingest needs to work.

    Required, because D-006 stores the object before writing the row: with the
    bucket unreachable every upload fails at the first step, and an instance
    that cannot accept a document is not ready to be sent one.
    """
    try:
        async with asyncio.timeout(_DEPENDENCY_TIMEOUT_SECONDS):
            await store.reachable()
        return DependencyStatus(name="object_store", ok=True, required=True)
    except Exception as exc:
        return DependencyStatus(
            name="object_store", ok=False, required=True, detail=type(exc).__name__
        )


def _report_identity(settings: Settings) -> DependencyStatus:
    """What this instance knows about token signing, without asking anyone.

    Never required. See the module docstring: an instance holding cached keys
    authenticates fine with the IdP down, and failing readiness across a fleet
    on a shared dependency turns a blip into an outage.
    """
    if settings.oidc_issuer is None or settings.oidc_jwks_url is None:
        return DependencyStatus(
            name="identity", ok=True, required=False, detail="no issuer configured"
        )

    cache = jwks_cache()
    keys = cache.key_count
    if keys == 0:
        # Accurate rather than alarming: no token has been validated yet. The
        # keys are fetched lazily on the first authenticated request, so a
        # freshly started instance always reports this.
        return DependencyStatus(
            name="identity", ok=False, required=False, detail="no signing keys cached yet"
        )

    age = cache.seconds_since_refresh
    return DependencyStatus(
        name="identity",
        ok=True,
        required=False,
        detail=f"{keys} signing key{'s' if keys != 1 else ''}, fetched {age:.0f}s ago",
    )


@router.get("/readyz", response_model=Readiness)
async def readiness(
    response: Response,
    session: SessionDep,
    settings: Settings = Depends(get_settings),
    store: ObjectStore = Depends(get_object_store),
) -> Readiness:
    (postgres, role), object_store = await asyncio.gather(
        _check_database(session, settings), _check_object_store(store)
    )
    checks = [postgres, role, object_store, _report_identity(settings)]

    ready = all(check.ok for check in checks if check.required)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(ready=ready, dependencies=checks)


__all__ = ["DependencyStatus", "Liveness", "Readiness", "router"]
