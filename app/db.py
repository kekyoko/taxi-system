"""Small synchronous PostgreSQL helper. All state lives in the database only."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row)


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """One atomic unit of work: commit on success, rollback on any exception."""
    conn = connect()
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def insert_outbox(
    conn: psycopg.Connection,
    *,
    event_id: str,
    channel: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    version: int,
    payload: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO outbox_events
            (id, channel, event_type, aggregate_type, aggregate_id, version,
             payload, schema_version, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 1, now())
        """,
        (
            event_id,
            channel,
            event_type,
            aggregate_type,
            aggregate_id,
            version,
            Jsonb(payload),
        ),
    )


def insert_notification(
    conn: psycopg.Connection,
    *,
    event_id: str,
    user_id: str,
    event_type: str,
    payload: dict,
) -> None:
    """History row and WebSocket outbox row are written in the same transaction."""
    row = conn.execute(
        """
        INSERT INTO notifications (id, user_id, event_type, payload)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        RETURNING seq
        """,
        (event_id, user_id, event_type, Jsonb(payload)),
    ).fetchone()
    if row is None:  # replay of the same notification id after a crash
        row = conn.execute("SELECT seq FROM notifications WHERE id=%s", (event_id,)).fetchone()
    seq = int(row["seq"]) if row else 0
    insert_outbox(
        conn,
        event_id=f"notify-{event_id}",
        channel="notifications",
        event_type=event_type,
        aggregate_type="user",
        aggregate_id=user_id,
        version=1,
        payload={
            "user_id": user_id,
            "notification_id": event_id,
            "seq": seq,
            "body": payload,
        },
    )
