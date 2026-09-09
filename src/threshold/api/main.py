"""FastAPI application.

Request IDs are generated here and bound into the structured log context, so
every log line for a request shares one id. In M5 this same id becomes the
OpenTelemetry trace id and lands in ``audit_event.trace_id``, which is what
makes a compliance question and a latency question answerable from one row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response

from threshold.adapters.db import dispose_engine, init_engine
from threshold.api.routes import documents, health
from threshold.config.settings import get_settings
from threshold.observability.logging import configure_logging

log = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    init_engine(settings)
    log.info(
        "service.start",
        service=settings.service_name,
        environment=settings.environment,
    )
    try:
        yield
    finally:
        await dispose_engine()
        log.info("service.stop", service=settings.service_name)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Threshold",
        version="0.1.0",
        summary=(
            "An accounts-payable control plane. The default action is to escalate; "
            "automation is opt-in per rule."
        ),
        lifespan=lifespan,
        # The generated OpenAPI document is the API contract and the frontend's
        # types are generated from it, so a renamed field fails the web build.
        openapi_url="/openapi.json",
        docs_url="/docs" if settings.is_local else None,
    )

    @app.middleware("http")
    async def bind_request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    app.include_router(health.router)
    app.include_router(documents.router)
    return app


app = create_app()
