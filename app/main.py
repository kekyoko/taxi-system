"""Taxi API: two identical instances behind nginx, all state in PostgreSQL."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .db import connection
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.shutting_down = False
    logger.info(json.dumps({"event": "startup", "instance": settings.instance}))
    yield
    app.state.shutting_down = True
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
