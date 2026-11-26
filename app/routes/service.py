"""Lab 4 async operations and lab 5 notification history / WebSocket tickets."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from ..db import connection, insert_outbox, transaction
from ..deps import User, current_user, require_idempotency_key
from ..domain import uuid_str
from ..errors import Problem
from .rides import _claim_idempotency

router = APIRouter(prefix="/v1", tags=["service"])


class PaymentRequest(BaseModel):
    amount_minor: int | None = Field(default=None, ge=1)
    currency: str = Field(default="UAH", min_length=3, max_length=3)

    model_config = {"extra": "ignore"}


@router.post("/rides/{ride_id}/payments", status_code=202, summary="Start an async payment operation")
def start_payment(
    ride_id: str,
    body: PaymentRequest,
    response: Response,
    user: User = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = require_idempotency_key(idempotency_key)
    body_hash = fingerprint_payment(ride_id, body)
    operation_id = uuid_str()
    with transaction() as conn:
        saved = _claim_idempotency(conn, user["id"], "create_payment", key, body_hash)
        if saved is not None:
            response.headers["Location"] = f"/v1/operations/{saved['ride_id']}"
            response.headers["Retry-After"] = "2"
            return saved["response_body"]

        ride = conn.execute(
            "SELECT * FROM rides WHERE id = %s AND user_id = %s FOR UPDATE",
            (ride_id, user["id"]),
        ).fetchone()
        if ride is None:
            raise Problem(404, "Not Found", "Ride not found", code="ride_not_found")
        if ride["status"] != "finished":
            raise Problem(409, "Conflict", "Only a finished ride can be paid", code="bad_state")
        if ride["payment_status"] in ("pending", "paid"):
            raise Problem(409, "Conflict", "Payment already started", code="payment_started")

        amount = body.amount_minor or ride["fare_minor"]
        conn.execute(
            """
            INSERT INTO operations(id, user_id, ride_id, type, status, provider_key, request)
            VALUES (%s, %s, %s, 'payment', 'pending', %s, %s)
            """,
            (
                operation_id, user["id"], ride_id, operation_id,
                Jsonb({"ride_id": ride_id, "amount_minor": amount, "currency": body.currency}),
            ),
        )
        conn.execute(
            "UPDATE rides SET payment_status='pending' WHERE id=%s", (ride_id,)
        )
        insert_outbox(
            conn, event_id=uuid_str(), channel="domain-events",
            event_type="payment.requested", aggregate_type="operation",
            aggregate_id=operation_id, version=1,
            payload={
                "operation_id": operation_id, "ride_id": ride_id,
                "provider_key": operation_id, "amount_minor": amount,
                "currency": body.currency, "schema_version": 1,
            },
        )
        result = {
            "operation_id": operation_id,
            "ride_id": ride_id,
            "status": "pending",
            "version": 1,
        }
        conn.execute(
            """
            UPDATE idempotency_records
            SET status='done', response_code=202, response_body=%s, ride_id=%s
            WHERE user_id=%s AND operation='create_payment' AND key=%s
            """,
            (Jsonb(result), operation_id, user["id"], key),
        )

    response.headers["Location"] = f"/v1/operations/{operation_id}"
    response.headers["Retry-After"] = "2"
    return result


def fingerprint_payment(ride_id: str, body: PaymentRequest) -> str:
    from ..domain import fingerprint

    return fingerprint({"ride_id": ride_id, **body.model_dump()})


@router.get("/operations/{operation_id}", summary="Poll my long-running operation")
def get_operation(operation_id: str, user: User = Depends(current_user)):
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM operations WHERE id=%s AND user_id=%s",
            (operation_id, user["id"]),
        ).fetchone()
    if row is None:
        raise Problem(404, "Not Found", "Operation not found", code="operation_not_found")
    return {
        "operation_id": row["id"],
        "ride_id": row["ride_id"],
        "status": row["status"],
        "version": row["version"],
        "result": row["result"],
        "error": row["error"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("/me/notifications/head", summary="Upper bound of my notification feed")
def notifications_head(user: User = Depends(current_user)):
    with connection() as conn:
        row = conn.execute(
            "SELECT coalesce(max(seq), 0) AS head FROM notifications WHERE user_id=%s",
            (user["id"],),
        ).fetchone()
    return {"head": int(row["head"])}


@router.get("/me/notifications", summary="My notification feed, monotonic positions")
def notifications(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=50),
    user: User = Depends(current_user),
):
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT seq, id, event_type, payload, created_at
            FROM notifications WHERE user_id=%s AND seq > %s
            ORDER BY seq ASC LIMIT %s
            """,
            (user["id"], after, limit),
        ).fetchall()
    return {
        "items": [
            {
                "seq": row["seq"],
                "id": row["id"],
                "type": row["event_type"],
                "payload": row["payload"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
        "next_cursor": rows[-1]["seq"] if len(rows) == limit else None,
    }


@router.post("/ws-tickets", status_code=201, summary="Single-use WebSocket ticket (30 s)")
def ws_ticket(request: Request, user: User = Depends(current_user)):
    ticket = uuid_str()
    hashed = getattr(request.state, "token_hash", "")
    with transaction() as conn:
        conn.execute(
            "INSERT INTO ws_tickets(ticket, user_id, token_hash, expires_at) VALUES (%s,%s,%s, now()+interval '30 seconds')",
            (ticket, user["id"], hashed),
        )
    return {"ticket": ticket, "url": f"/v1/ws?ticket={ticket}", "expires_in_s": 30}
