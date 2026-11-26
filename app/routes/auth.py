"""Lab 3: login, logout, current principal, revocation signal for gateways."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..db import connection, insert_outbox, transaction
from ..deps import User, current_user
from ..domain import uuid_str
from ..errors import Problem
from ..security import new_token, token_hash, verify_password

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/login", summary="Create an opaque server-side session")
def login(body: LoginRequest):
    with connection() as conn:
        row = conn.execute(
            "SELECT id, login, role, district, password_hash FROM users WHERE login = %s",
            (body.login,),
        ).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise Problem(401, "Unauthorized", "Invalid login or password", code="bad_credentials")
    token = new_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (token_hash(token), row["id"], expires),
        )
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_at": expires.isoformat(),
        "user": {
            "id": row["id"],
            "login": row["login"],
            "role": row["role"],
            "district": row["district"],
        },
    }


@router.post("/logout", status_code=204, summary="Destroy session and close WebSockets")
def logout(request: Request, user: User = Depends(current_user)):
    hashed = getattr(request.state, "token_hash", "")
    with transaction() as conn:
        deleted = conn.execute(
            "DELETE FROM sessions WHERE token_hash = %s", (hashed,)
        ).rowcount
        if deleted:
            insert_outbox(
                conn,
                event_id=uuid_str(),
                channel="notifications",
                event_type="session.logout",
                aggregate_type="session",
                aggregate_id=user["id"],
                version=1,
                payload={"token_hash": hashed, "user_id": user["id"]},
            )
    return None


@router.get("/me", summary="Current principal and role")
def me(user: User = Depends(current_user)):
    return dict(user)
