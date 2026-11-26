"""Taxi API: two identical instances behind nginx, all state in PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .db import connection
from .deps import cleanup_expired_keys
from .errors import (
    Problem,
    bad_json_handler,
    http_exception_handler,
    problem_handler,
    problem_response,
    validation_handler,
)
from .routes import admin, auth, rides, service

logger = logging.getLogger("taxi.app")
logger.setLevel(logging.INFO)
if not logger.handlers:  # uvicorn configures only the "uvicorn*" loggers
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
logger.propagate = False


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            removed = await asyncio.to_thread(cleanup_expired_keys)
            if removed:
                logger.info(json.dumps({"event": "idempotency.cleanup", "removed": removed}))
        except Exception as error:  # noqa: BLE001 - housekeeping must not kill the app
            logger.warning(json.dumps({"event": "idempotency.cleanup_error", "error": str(error)}))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.shutting_down = False
    previous: dict[int, object] = {}

    def _drain(signum: int, frame: object) -> None:
        # Flip readiness at signal time, before uvicorn stops accepting work.
        app.state.shutting_down = True
        handler = previous.get(signum)
        if callable(handler):
            handler(signum, frame)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, _drain)
    cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info(json.dumps({"event": "startup", "instance": settings.instance}))
    try:
        yield
    finally:
        app.state.shutting_down = True
        cleanup_task.cancel()
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]
        logger.info(json.dumps({"event": "shutdown", "instance": settings.instance}))


app = FastAPI(
    title="C14 Taxi API",
    version="1.1.0",
    description="Passenger rides, atomic driver assignment, idempotency, outbox and notifications.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.client_origin, settings.extra_client_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match", "X-Request-ID"],
    expose_headers=["Location", "ETag", "Retry-After", "X-Request-ID", "X-Instance"],
)

app.add_exception_handler(Problem, problem_handler)
app.add_exception_handler(RequestValidationError, validation_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(json.JSONDecodeError, bad_json_handler)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    status = 500
    response = None
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            json.dumps(
                {
                    "event": "request",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "request_id": request_id,
                    "instance": settings.instance,
                    "route": template,
                    "method": request.method,
                    "status": status,
                    "duration_ms": duration_ms,
                }
            )
        )
        if response is not None:
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Instance"] = settings.instance


@app.get("/health", tags=["service"], summary="Liveness: no database call")
def health():
    return {"status": "ok", "instance": settings.instance}


@app.get("/ready", tags=["service"], summary="Readiness: database reachable and not draining")
def ready(request: Request):
    if getattr(request.app.state, "shutting_down", False):
        return problem_response(
            503, "Service Unavailable", "Draining", code="shutting_down",
            headers={"Retry-After": "2"},
        )
    try:
        with connection() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001 - readiness reports any DB failure
        return problem_response(503, "Service Unavailable", "Database unavailable", code="db_unavailable")
    return {"status": "ready", "instance": settings.instance}


app.include_router(auth.router)
app.include_router(rides.router)
app.include_router(admin.router)
app.include_router(service.router)

# Contract-facing alias: clients and openapi.yaml use /v1/me.
app.add_api_route(
    "/v1/me",
    auth.me,
    methods=["GET"],
    tags=["auth"],
    summary="Current principal and role",
)
