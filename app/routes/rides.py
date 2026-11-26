"""Labs 1, 2 and 6: contract, main operation, cursor lists, If-Match and tariffs."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import psycopg.errors
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from ..db import connection, insert_notification, insert_outbox, transaction
from ..deps import User, current_user, feature_tariffs_enabled, require_idempotency_key, staff_user
from ..domain import (
    accept_deadline,
    can_transition,
    decode_cursor,
    encode_cursor,
    fare_minor,
    fingerprint,
    night_overlap,
    uuid_str,
)
from ..errors import Problem
from ..faults import inject_fault

router = APIRouter(prefix="/v1/rides", tags=["rides"])


class RideCreate(BaseModel):
    district: str = Field(min_length=1, max_length=64)
    pickup: str = Field(min_length=1, max_length=200)
    tariff: Literal["standard", "economy", "comfort"] = "standard"
    distance_km: Decimal = Field(default=Decimal("2.5"), ge=0, le=100)
    duration_min: int = Field(default=10, ge=1, le=240)

    model_config = {"extra": "ignore"}


class RidePatch(BaseModel):
    pickup: str = Field(min_length=1, max_length=200)

    model_config = {"extra": "ignore"}


def _claim_idempotency(conn, user_id: str, operation: str, key: str, body_hash: str):
    """Return None for a new key, or the saved record for a replay."""
    try:
        with conn.transaction():  # savepoint: a conflicting insert must not abort the tx
            conn.execute(
                """
                INSERT INTO idempotency_records
                    (user_id, operation, key, request_hash, status, expires_at)
                VALUES (%s, %s, %s, %s, 'processing', now() + interval '24 hours')
                """,
                (user_id, operation, key, body_hash),
            )
        return None
    except psycopg.errors.UniqueViolation:
        saved = conn.execute(
            """
            SELECT * FROM idempotency_records
            WHERE user_id = %s AND operation = %s AND key = %s FOR UPDATE
            """,
            (user_id, operation, key),
        ).fetchone()
        if saved is None:
            raise Problem(409, "Conflict", "Idempotency key is being processed", code="key_in_use")
        if saved["status"] == "processing":
            raise Problem(409, "Conflict", "Idempotency key is being processed", code="key_in_use")
        if saved["request_hash"] != body_hash:
            raise Problem(422, "Unprocessable Entity", "Idempotency key was used with another body", code="key_body_mismatch")
        return saved


def _ride_response(row: dict) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "district": row["district"],
        "pickup": row["pickup"],
        "driver_id": row["driver_id"],
        "status": row["status"],
        "payment_status": row["payment_status"],
        "fare_minor": row["fare_minor"],
        "currency": row["currency"],
        "version": row["version"],
        "tariff": row["tariff"],
        "distance_km": str(row["distance_km"]),
        "created_at": row["created_at"].isoformat(),
        "accept_deadline": row["accept_deadline"].isoformat() if row["accept_deadline"] else None,
    }


@router.post("", status_code=201, summary="Order a ride and atomically assign a free driver")
def create_ride(
    body: RideCreate,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = require_idempotency_key(idempotency_key)
    body_hash = fingerprint(body.model_dump())
    now = datetime.now(timezone.utc)
    ride_id = uuid_str()

    with transaction() as conn:
        saved = _claim_idempotency(conn, user["id"], "create_ride", key, body_hash)
        if saved is not None:
            return JSONResponse(
                status_code=saved["response_code"],
                content=saved["response_body"],
                headers={
                    "Location": f"/v1/rides/{saved['ride_id']}",
                    "ETag": f'"{saved["response_body"]["version"]}"',
                },
            )

        if body.tariff != "standard" and not feature_tariffs_enabled(conn):
            raise Problem(
                409,
                "Conflict",
                "Tariff feature is not enabled yet",
                code="feature_disabled",
                headers={"Retry-After": "5"},
            )

        # The invariant is protected by a row lock taken inside the same transaction.
        driver = conn.execute(
            """
            SELECT id, version FROM drivers
            WHERE district = %s AND status = 'online'
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
            """,
            (body.district,),
        ).fetchone()
        if driver is None:
            raise Problem(409, "Conflict", "No free driver in this district", code="no_driver")

        deadline = accept_deadline(now)
        fare = fare_minor(
            distance_km=body.distance_km,
            duration_min=body.duration_min,
            tariff=body.tariff,
            started_at=now,
        )
        conn.execute(
            """
            INSERT INTO rides
                (id, user_id, district, pickup, driver_id, status, fare_minor,
                 currency, version, created_at, accept_deadline, tariff, distance_km)
            VALUES (%s, %s, %s, %s, %s, 'assigned', %s, 'UAH', 1, %s, %s, %s, %s)
            """,
            (
                ride_id, user["id"], body.district, body.pickup, driver["id"],
                fare, now, deadline, body.tariff, body.distance_km,
            ),
        )
        updated = conn.execute(
            """
            UPDATE drivers SET status = 'busy', version = version + 1, updated_at = now()
            WHERE id = %s AND version = %s
            """,
            (driver["id"], driver["version"]),
        )
        if updated.rowcount != 1:
            raise Problem(409, "Conflict", "Driver was changed concurrently", code="driver_conflict")

        event_id = uuid_str()
        insert_outbox(
            conn, event_id=event_id, channel="domain-events",
            event_type="ride.requested", aggregate_type="ride", aggregate_id=ride_id,
            version=1, payload={"ride_id": ride_id, "district": body.district, "schema_version": 1},
        )
        insert_outbox(
            conn, event_id=uuid_str(), channel="domain-events",
            event_type="driver.assigned", aggregate_type="ride", aggregate_id=ride_id,
            version=1,
            payload={
                "ride_id": ride_id, "driver_id": driver["id"], "version": 1,
                "accept_deadline": deadline.isoformat(), "schema_version": 1,
            },
        )
        insert_notification(
            conn,
            event_id=uuid_str(),
            user_id=user["id"],
            event_type="driver.assigned",
            payload={
                "ride_id": ride_id,
                "driver_id": driver["id"],
                "pickup": body.pickup,
                "message": "Водія призначено",
            },
        )

        result = {
            "id": ride_id,
            "user_id": user["id"],
            "district": body.district,
            "pickup": body.pickup,
            "driver_id": driver["id"],
            "status": "assigned",
            "payment_status": "unpaid",
            "fare_minor": fare,
            "currency": "UAH",
            "version": 1,
            "tariff": body.tariff,
            "distance_km": str(body.distance_km),
            "created_at": now.isoformat(),
            "accept_deadline": deadline.isoformat(),
        }
        conn.execute(
            """
            UPDATE idempotency_records
            SET status = 'done', response_code = 201, response_body = %s, ride_id = %s
            WHERE user_id = %s AND operation = 'create_ride' AND key = %s
            """,
            (Jsonb(result), ride_id, user["id"], key),
        )

    inject_fault("after_commit_before_response")
    response.headers["Location"] = f"/v1/rides/{ride_id}"
    response.headers["ETag"] = '"1"'
    return result


@router.get("", summary="My rides, newest first, with an opaque cursor")
def list_rides(
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None),
    user: User = Depends(current_user),
):
    if cursor:
        try:
            created_at, last_id = decode_cursor(cursor)
        except ValueError as error:
            raise Problem(400, "Bad Request", str(error), code="bad_cursor") from error
    else:
        created_at, last_id = datetime.max.replace(tzinfo=timezone.utc), "\uffff"

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM rides
            WHERE user_id = %s AND (created_at, id) < (%s, %s)
            ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (user["id"], created_at, last_id, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "items": [_ride_response(row) for row in page],
        "next_cursor": encode_cursor(page[-1]["created_at"], page[-1]["id"]) if has_more and page else None,
        "limit": limit,
    }


@router.get("/{ride_id}", summary="My ride only; foreign or missing ride is 404")
def get_ride(ride_id: str, user: User = Depends(current_user)):
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM rides WHERE id = %s AND user_id = %s", (ride_id, user["id"])
        ).fetchone()
    if row is None:
        raise Problem(404, "Not Found", "Ride not found", code="ride_not_found")
    return JSONResponse(
        content=_ride_response(row), headers={"ETag": f'"{row["version"]}"'}
    )


@router.patch("/{ride_id}", summary="Change pickup with If-Match (one conditional UPDATE)")
def patch_ride(
    ride_id: str,
    body: RidePatch,
    request: Request,
    user: User = Depends(current_user),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    if not if_match:
        raise Problem(428, "Precondition Required", "If-Match is required", code="missing_if_match")
    try:
        version = int(if_match.strip().strip('"'))
    except ValueError as error:
        raise Problem(400, "Bad Request", "Invalid If-Match", code="bad_if_match") from error
    with transaction() as conn:
        row = conn.execute(
            """
            UPDATE rides SET pickup = %s, version = version + 1
            WHERE id = %s AND user_id = %s AND version = %s
              AND status IN ('requested', 'assigned')
            RETURNING *
            """,
            (body.pickup, ride_id, user["id"], version),
        ).fetchone()
        if row is None:
            exists = conn.execute(
                "SELECT 1 FROM rides WHERE id = %s AND user_id = %s", (ride_id, user["id"])
            ).fetchone()
            if exists is None:
                raise Problem(404, "Not Found", "Ride not found", code="ride_not_found")
            raise Problem(412, "Precondition Failed", "Version is stale", code="stale_version")
    return JSONResponse(
        content=_ride_response(row), headers={"ETag": f'"{row["version"]}"'}
    )


@router.post("/{ride_id}/cancel", summary="Cancel once; a replay never frees the driver twice")
def cancel_ride(ride_id: str, user: User = Depends(current_user)):
    with transaction() as conn:
        ride = conn.execute(
            "SELECT * FROM rides WHERE id = %s AND user_id = %s FOR UPDATE",
            (ride_id, user["id"]),
        ).fetchone()
        if ride is None:
            raise Problem(404, "Not Found", "Ride not found", code="ride_not_found")
        if ride["status"] == "cancelled":
            return {"id": ride_id, "status": "cancelled", "version": ride["version"], "replayed": True}
        if ride["status"] not in ("requested", "assigned"):
            raise Problem(409, "Conflict", "Ride cannot be cancelled", code="bad_state")

        if ride["driver_id"]:
            conn.execute(
                """
                UPDATE drivers SET status = 'online', version = version + 1, updated_at = now()
                WHERE id = %s AND status = 'busy'
                """,
                (ride["driver_id"],),
            )
        conn.execute(
            """
            UPDATE rides SET status = 'cancelled', version = version + 1
            WHERE id = %s
            """,
            (ride_id,),
        )
        insert_notification(
            conn, event_id=uuid_str(), user_id=user["id"],
            event_type="ride.cancelled", payload={"ride_id": ride_id, "message": "Поїздку скасовано"},
        )
    return {"id": ride_id, "status": "cancelled", "version": ride["version"] + 1, "replayed": False}


@router.post("/{ride_id}/accept", summary="Dispatcher: accept, or reassign when the deadline passed")
def accept_ride(
    ride_id: str,
    user: User = Depends(staff_user),
):
    now = datetime.now(timezone.utc)
    no_replacement = False
    with transaction() as conn:
        ride = conn.execute(
            "SELECT * FROM rides WHERE id = %s FOR UPDATE", (ride_id,)
        ).fetchone()
        if ride is None:
            raise Problem(404, "Not Found", "Ride not found", code="ride_not_found")
        if ride["status"] != "assigned":
            raise Problem(409, "Conflict", "Ride is not assigned", code="bad_state")

        old_driver = conn.execute(
            "SELECT * FROM drivers WHERE id = %s FOR UPDATE", (ride["driver_id"],)
        ).fetchone()
        if ride["accept_deadline"] and now >= ride["accept_deadline"]:
            next_driver = conn.execute(
                """
                SELECT id, version FROM drivers
                WHERE district = %s AND status = 'online' AND id <> %s
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
                """,
                (ride["district"], ride["driver_id"]),
            ).fetchone()
            if old_driver:
                conn.execute(
                    "UPDATE drivers SET status='online', version=version+1 WHERE id=%s",
                    (old_driver["id"],),
                )
            if next_driver is None:
                # The expired driver is released; the error is reported after commit.
                conn.execute(
                    """
                    UPDATE rides SET status='requested', driver_id=NULL, version=version+1
                    WHERE id=%s
                    """,
                    (ride_id,),
                )
                no_replacement = True
            else:
                conn.execute(
                    "UPDATE drivers SET status='busy', version=version+1 WHERE id=%s",
                    (next_driver["id"],),
                )
                new_deadline = accept_deadline(now)
                conn.execute(
                    """
                    UPDATE rides SET driver_id=%s, accept_deadline=%s, version=version+1
                    WHERE id=%s
                    """,
                    (next_driver["id"], new_deadline, ride_id),
                )
                insert_outbox(
                    conn, event_id=uuid_str(), channel="domain-events",
                    event_type="driver.assigned", aggregate_type="ride", aggregate_id=ride_id,
                    version=ride["version"] + 1,
                    payload={"ride_id": ride_id, "driver_id": next_driver["id"], "reassigned": True, "schema_version": 1},
                )
                insert_notification(
                    conn, event_id=uuid_str(), user_id=ride["user_id"],
                    event_type="driver.reassigned",
                    payload={"ride_id": ride_id, "driver_id": next_driver["id"], "message": "Призначено іншого водія"},
                )
                return {
                    "id": ride_id, "status": "assigned", "driver_id": next_driver["id"],
                    "version": ride["version"] + 1, "reassigned": True,
                }

        if not no_replacement:
            if not can_transition(ride["status"], "riding"):
                raise Problem(409, "Conflict", "Illegal transition", code="bad_state")
            conn.execute(
                """
                UPDATE rides SET status='riding', started_at=%s, version=version+1 WHERE id=%s
                """,
                (now, ride_id),
            )
            insert_outbox(
                conn, event_id=uuid_str(), channel="domain-events",
                event_type="car.position", aggregate_type="ride", aggregate_id=ride_id,
                version=ride["version"] + 1,
                payload={
                    "ride_id": ride_id, "driver_id": ride["driver_id"],
                    "lat": float(old_driver["lat"]) if old_driver else None,
                    "lon": float(old_driver["lon"]) if old_driver else None,
                    "schema_version": 1,
                },
            )
            insert_notification(
                conn, event_id=uuid_str(), user_id=ride["user_id"],
                event_type="ride.riding", payload={"ride_id": ride_id, "message": "Поїздку почато"},
            )
            return {
                "id": ride_id, "status": "riding", "driver_id": ride["driver_id"],
                "version": ride["version"] + 1, "reassigned": False,
            }

    raise Problem(409, "Conflict", "Deadline passed, no replacement driver", code="no_driver")


@router.post("/{ride_id}/finish", summary="Dispatcher: finish a ride with the night tariff rule")
def finish_ride(ride_id: str, user: User = Depends(staff_user)):
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        ride = conn.execute("SELECT * FROM rides WHERE id = %s FOR UPDATE", (ride_id,)).fetchone()
        if ride is None:
            raise Problem(404, "Not Found", "Ride not found", code="ride_not_found")
        if not can_transition(ride["status"], "finished"):
            raise Problem(409, "Conflict", "Ride is not riding", code="bad_state")
        started = ride["started_at"] or ride["created_at"]
        fare = fare_minor(
            distance_km=ride["distance_km"],
            duration_min=max(1, int((now - started).total_seconds() // 60)),
            tariff=ride["tariff"],
            started_at=started,
            finished_at=now,
        )
        night = night_overlap(started, now)
        conn.execute(
            """
            UPDATE rides SET status='finished', finished_at=%s, fare_minor=%s, version=version+1
            WHERE id=%s
            """,
            (now, fare, ride_id),
        )
        if ride["driver_id"]:
            conn.execute(
                "UPDATE drivers SET status='online', version=version+1 WHERE id=%s AND status='busy'",
                (ride["driver_id"],),
            )
        insert_outbox(
            conn, event_id=uuid_str(), channel="domain-events",
            event_type="ride.finished", aggregate_type="ride", aggregate_id=ride_id,
            version=ride["version"] + 1,
            payload={"ride_id": ride_id, "fare_minor": fare, "night_tariff": night, "schema_version": 1},
        )
        insert_notification(
            conn, event_id=uuid_str(), user_id=ride["user_id"],
            event_type="ride.finished",
            payload={"ride_id": ride_id, "fare_minor": fare, "night_tariff": night, "message": "Поїздку завершено"},
        )
    return {"id": ride_id, "status": "finished", "fare_minor": fare, "night_tariff": night, "version": ride["version"] + 1}
