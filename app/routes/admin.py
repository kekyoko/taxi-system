"""Dispatcher-only staff functions and the lab-6 feature flag."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import connection, transaction
from ..deps import staff_user
from ..errors import Problem

router = APIRouter(prefix="/v1", tags=["staff"])


class FeatureFlag(BaseModel):
    enabled: bool


@router.get("/drivers", summary="Staff: current driver pool (invariant inspection)")
def list_drivers(user=Depends(staff_user)):
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, district, status, version, lat, lon FROM drivers ORDER BY id"
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/drivers/{driver_id}/offline", summary="Staff: take a driver off the line")
def driver_offline(driver_id: str, user=Depends(staff_user)):
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM drivers WHERE id = %s FOR UPDATE", (driver_id,)
        ).fetchone()
        if row is None:
            raise Problem(404, "Not Found", "Driver not found", code="driver_not_found")
        if row["status"] == "busy":
            raise Problem(409, "Conflict", "Busy driver cannot go offline", code="driver_busy")
        conn.execute(
            "UPDATE drivers SET status='offline', version=version+1 WHERE id=%s", (driver_id,)
        )
    return {"id": driver_id, "status": "offline", "version": row["version"] + 1}


@router.post("/drivers/{driver_id}/online", summary="Staff: return a driver to the line")
def driver_online(driver_id: str, user=Depends(staff_user)):
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM drivers WHERE id = %s FOR UPDATE", (driver_id,)
        ).fetchone()
        if row is None:
            raise Problem(404, "Not Found", "Driver not found", code="driver_not_found")
        conn.execute(
            "UPDATE drivers SET status='online', version=version+1 WHERE id=%s", (driver_id,)
        )
    return {"id": driver_id, "status": "online", "version": row["version"] + 1}


@router.get("/admin/features", summary="Current feature flags")
def get_features(user=Depends(staff_user)):
    with connection() as conn:
        rows = conn.execute("SELECT name, enabled FROM feature_flags ORDER BY name").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/admin/features/{name}", summary="Staff: flip a feature flag, rollback point")
def set_feature(name: str, body: FeatureFlag, user=Depends(staff_user)):
    with transaction() as conn:
        updated = conn.execute(
            "UPDATE feature_flags SET enabled=%s, updated_at=now() WHERE name=%s",
            (body.enabled, name),
        )
        if updated.rowcount != 1:
            raise Problem(404, "Not Found", "Unknown feature", code="feature_not_found")
        row = conn.execute(
            "SELECT name, enabled FROM feature_flags WHERE name=%s", (name,)
        ).fetchone()
    return dict(row)
