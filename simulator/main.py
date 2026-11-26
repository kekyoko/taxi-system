"""Slow external payment gateway simulator with idempotent provider keys."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connection, transaction  # noqa: E402

app = FastAPI(title="C14 payment gateway simulator", version="1.0.0")

MIN_DELAY_MS = int(os.environ.get("SIM_MIN_DELAY_MS", "200"))
MAX_DELAY_MS = int(os.environ.get("SIM_MAX_DELAY_MS", "800"))
FAIL_RATE = float(os.environ.get("SIM_FAIL_RATE", "0.10"))
DECLINE_RATE = float(os.environ.get("SIM_DECLINE_RATE", "0.10"))
SEED = os.environ.get("SIM_SEED", "taxi-seed")
UNAVAILABLE_UNTIL = 0.0  # controlled "provider is down for N seconds" mode


class Charge(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    amount_minor: int = Field(ge=1)
    currency: str = Field(default="UAH", min_length=3, max_length=3)


def _rng(key: str) -> random.Random:
    digest = hashlib.sha256(f"{SEED}:{key}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


@app.get("/health")
def health():
    return {"status": "ok", "service": "external-sim"}


@app.post("/charge")
def charge(body: Charge):
    with connection() as conn:
        saved = conn.execute(
            "SELECT status, response FROM sim.provider_effects WHERE provider_key=%s",
            (body.key,),
        ).fetchone()
    if saved:
        status_code = 200 if saved["status"] == "succeeded" else 422
        return JSONResponse(status_code=status_code, content=saved["response"])

    if time.monotonic() < UNAVAILABLE_UNTIL:
        # 503 is never remembered for a key: a later retry can succeed.
        return JSONResponse(
            status_code=503,
            content={
                "type": "https://example.edu/taxi/problems/provider_unavailable",
                "title": "Service Unavailable", "status": 503,
                "detail": "Payment gateway temporarily unavailable",
            },
            headers={"Retry-After": "2"},
        )

    rng = _rng(body.key)
    delay = rng.uniform(MIN_DELAY_MS, MAX_DELAY_MS) / 1000
    time.sleep(delay)
    roll = rng.random()
    if roll < FAIL_RATE:
        # 503 is not remembered: a retry with the same key may succeed.
        return JSONResponse(
            status_code=503,
            content={
                "type": "https://example.edu/taxi/problems/provider_unavailable",
                "title": "Service Unavailable", "status": 503,
                "detail": "Payment gateway temporarily unavailable",
            },
            headers={"Retry-After": "2"},
        )
    if roll < FAIL_RATE + DECLINE_RATE:
        status, status_code = "declined", 422
        response = {
            "type": "https://example.edu/taxi/problems/provider_declined",
            "title": "Payment declined", "status": 422,
            "detail": "Card was declined by the provider", "reason": "card_declined",
        }
    else:
        status, status_code = "succeeded", 200
        response = {
            "status": "succeeded",
            "charge_id": hashlib.sha256(body.key.encode()).hexdigest()[:16],
            "amount_minor": body.amount_minor,
            "currency": body.currency,
        }
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO sim.provider_effects(provider_key, status, response)
            VALUES (%s, %s, %s)
            ON CONFLICT (provider_key) DO NOTHING
            """,
            (body.key, status, Jsonb(response)),
        )
    return JSONResponse(status_code=status_code, content=response)


@app.get("/effects")
def effects(key: str | None = None):
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT provider_key, status, response, created_at
            FROM sim.provider_effects
            WHERE %s::text IS NULL OR provider_key = %s::text
            ORDER BY created_at, provider_key
            """,
            (key, key),
        ).fetchall()
    return {
        "count": len(rows),
        "items": [
            {
                "key": row["provider_key"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
        "by_key": {row["provider_key"]: row["status"] for row in rows},
    }


@app.post("/admin/unavailable")
def set_unavailable(seconds: int = Query(default=5, ge=1, le=120)):
    """Force technical failures for a bounded period (O4.3 experiment)."""
    global UNAVAILABLE_UNTIL
    UNAVAILABLE_UNTIL = time.monotonic() + seconds
    return {"unavailable_for_s": seconds}


@app.get("/config")
def config():
    return {
        "min_delay_ms": MIN_DELAY_MS, "max_delay_ms": MAX_DELAY_MS,
        "fail_rate": FAIL_RATE, "decline_rate": DECLINE_RATE, "seed": SEED,
    }
