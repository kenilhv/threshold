"""FastAPI application.

Request IDs are generated here and bound into the structured log context, so
every log line for a request shares one id. In M5 this same id becomes the
OpenTelemetry trace id and lands in ``audit_event.trace_id``, which is what
makes a compliance question and a latency question answerable from one row.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from threshold.adapters.db import dispose_engine, init_engine
from threshold.api.routes import documents, health, identity
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


#: An inbound request id is echoed back and written to `audit_event.trace_id`,
#: which is String(32). Accepting the header verbatim means a caller controls
#: the contents of a log field and an indexed column, and an over-long value is
#: a database error on the audit write rather than on the request that caused
#: it. Bounded and restricted to characters that cannot break a log line; a
#: header that does not conform is replaced rather than rejected, because the
#: request itself is fine and refusing it would be a worse trade.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _accept_request_id(supplied: str | None) -> str:
    if supplied and _SAFE_REQUEST_ID.match(supplied):
        return supplied
    return uuid.uuid4().hex


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
        request_id = _accept_request_id(request.headers.get(REQUEST_ID_HEADER))
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
        try:
            response = await call_next(request)
        except Exception:
            # The 500 is the response most in need of a correlation id, and it
            # is the one that would not have had one: assigning the header after
            # the call cannot run when the call raises, and Starlette's
            # ServerErrorMiddleware sits outside this and builds its own reply.
            # So this builds the reply, with the id on it.
            log.exception("request.unhandled_error")
            response = JSONResponse(
                status_code=500,
                content={"detail": "internal server error", "request_id": request_id},
            )
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    app.include_router(health.router)
    app.include_router(identity.router, prefix="/v1")
    app.include_router(documents.router)
    return app


app = create_app()
