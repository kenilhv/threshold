"""Liveness and readiness.

These are different questions and conflating them causes real outages: a
liveness probe that checks the database will restart every healthy pod during a
database failover, turning a recoverable dependency blip into a full outage.

  /healthz   is this process alive?            no dependencies, never fails slow
  /readyz    should traffic be routed here?    checks every hard dependency
"""

from __future__ import annotations

import asyncio
from typing import Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.api.dependencies import SessionDep
from threshold.config.settings import Settings, get_settings

router = APIRouter(tags=["health"])

_DEPENDENCY_TIMEOUT_SECONDS = 2.0


class Liveness(BaseModel):
    status: Literal["ok"]
    service: str


class DependencyStatus(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class Readiness(BaseModel):
    ready: bool
    dependencies: list[DependencyStatus]


@router.get("/healthz", response_model=Liveness)
async def liveness(settings: Settings = Depends(get_settings)) -> Liveness:
    return Liveness(status="ok", service=settings.service_name)


async def _check_database(session: AsyncSession) -> DependencyStatus:
    try:
        async with asyncio.timeout(_DEPENDENCY_TIMEOUT_SECONDS):
            await session.execute(text("SELECT 1"))
        return DependencyStatus(name="postgres", ok=True)
    except Exception as exc:
        return DependencyStatus(name="postgres", ok=False, detail=type(exc).__name__)


async def _check_redis(settings: Settings) -> DependencyStatus:
    client = aioredis.from_url(str(settings.redis_url))
    try:
        async with asyncio.timeout(_DEPENDENCY_TIMEOUT_SECONDS):
            await client.ping()
        return DependencyStatus(name="redis", ok=True)
    except Exception as exc:
        return DependencyStatus(name="redis", ok=False, detail=type(exc).__name__)
    finally:
        await client.aclose()


@router.get("/readyz", response_model=Readiness)
async def readiness(
    response: Response,
    session: SessionDep,
    settings: Settings = Depends(get_settings),
) -> Readiness:
    checks = await asyncio.gather(_check_database(session), _check_redis(settings))
    ready = all(c.ok for c in checks)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(ready=ready, dependencies=list(checks))
