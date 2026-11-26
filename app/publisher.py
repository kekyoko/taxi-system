"""Transactional-outbox publisher: FOR UPDATE SKIP LOCKED + broker acknowledgement."""

from __future__ import annotations

import json
import time

from .broker import DOMAIN_STREAM, NOTIFICATION_STREAM, client, event_fields
from .config import settings
from .db import transaction
from .faults import inject_fault


def publish_batch() -> int:
    published = 0
    broker = client()
    try:
        with transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outbox_events
                WHERE published_at IS NULL ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED LIMIT 50
                """
            ).fetchall()
            if not rows:
                return 0
            inject_fault("publisher_before_send")
            pipe = broker.pipeline(transaction=True)
            for row in rows:
                stream = NOTIFICATION_STREAM if row["channel"] == "notifications" else DOMAIN_STREAM
                pipe.xadd(stream, event_fields(row))
            pipe.execute()
            ids = [row["id"] for row in rows]
            conn.execute(
                "UPDATE outbox_events SET published_at=now() WHERE id = ANY(%s)",
                (ids,),
            )
            published = len(ids)
    finally:
        broker.close()
    return published


def main() -> None:
    print(json.dumps({"event": "publisher.start", "instance": settings.instance}), flush=True)
    while True:
        try:
            count = publish_batch()
            if count:
                print(json.dumps({"event": "outbox.published", "count": count}), flush=True)
        except Exception as error:  # noqa: BLE001 - transient broker/DB failures are retried
            print(json.dumps({"event": "publisher.error", "error": str(error)}), flush=True)
        time.sleep(settings.poll_interval_ms / 1000)


if __name__ == "__main__":
    main()
