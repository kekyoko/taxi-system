"""RFC 9457 Problem Details for every error response."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_TYPE = "https://example.edu/taxi/problems"


class Problem(Exception):
    def __init__(
        self,
        status: int,
        title: str,
        detail: str = "",
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code
        self.headers = headers or {}
        self.extra = extra or {}


def problem_response(
    status: int,
    title: str,
    detail: str = "",
    *,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE}/{code or status}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if code:
        body["code"] = code
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status,
        content=body,
        headers={"Content-Type": "application/problem+json", **(headers or {})},
    )


async def problem_handler(_: Request, exc: Problem) -> JSONResponse:
    return problem_response(
        exc.status,
        exc.title,
        exc.detail,
        code=exc.code,
        headers=exc.headers,
        extra=exc.extra,
    )


async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if any(error.get("type") == "json_invalid" for error in errors):
        return problem_response(
            400, "Bad Request", "Malformed JSON", code="malformed_json"
        )
    return problem_response(
        422,
        "Unprocessable Entity",
        "Request body or parameters are invalid",
        code="validation_error",
        extra={"errors": [str(error.get("msg", "")) for error in errors]},
    )


async def http_exception_handler(
    _: Request, exc: StarletteHTTPException
) -> JSONResponse:
    return problem_response(
        exc.status_code,
        str(exc.detail),
        code="http_error",
        headers=dict(exc.headers or {}),
    )


async def bad_json_handler(_: Request, __: Exception) -> JSONResponse:
    return problem_response(
        400, "Bad Request", "Malformed JSON", code="malformed_json"
    )
