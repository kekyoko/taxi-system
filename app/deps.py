"""Request dependencies: authentication and role checks."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, Request

from .config import settings
from .db import connection
from .errors import Problem
from .security import token_hash


class User(dict):
    """Lightweight authenticated principal."""


async def current_user(
    request: Request, authorization: str | None = Header(default=None)
) -> User:
    # X-User-Id is deliberately ignored since lab 3.
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Problem(401, "Unauthorized", "Bearer token is required", code="unauthorized")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise Problem(401, "Unauthorized", "Bearer token is required", code="unauthorized")
    with connection() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.login, u.role, u.district
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > now()
            """,
            (token_hash(token),),
        ).fetchone()
    if row is None:
        raise Problem(401, "Unauthorized", "Invalid or expired token", code="unauthorized")
    request.state.token_hash = token_hash(token)
    return User(row)


async def staff_user(user: User = Depends(current_user)) -> User:
    if user["role"] != "staff":
        raise Problem(403, "Forbidden", "Staff role is required", code="forbidden")
    return user


def require_idempotency_key(value: str | None) -> str:
    if not value or not value.strip():
        raise Problem(
            400,
            "Bad Request",
            "Idempotency-Key header is required",
            code="missing_idempotency_key",
        )
    return value.strip()


def feature_tariffs_enabled(conn: Any) -> bool:
    row = conn.execute(
        "SELECT enabled FROM feature_flags WHERE name = 'tariffs_v2'"
    ).fetchone()
    master = bool(row and row["enabled"])
    # Per-instance flag allows a canary: app-a only, app-b keeps old behaviour.
    canary = settings.feature_tariffs_v2 or settings.instance == "app-a"
    return master and canary
