"""Event handler: external payment effect, one local commit, then broker ACK."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from .broker import DOMAIN_STREAM, client, ensure_group
from .config import settings
from .db import connection, insert_notification, transaction
from .faults import inject_fault

GROUP = "taxi-worker"


class ProviderUnavailable(RuntimeError):
    """Transient provider failure: the message must stay unacknowledged for retry."""


def _already_processed(event_id: str) -> bool:
    with connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM processed_messages WHERE handler=%s AND event_id=%s",
                ("worker", event_id),
            ).fetchone()
            is not None
        )


def _call_provider(provider_key: str, amount_minor: int, currency: str) -> tuple[str, dict]:
    """Return succeeded / declined (business rule) / unavailable (technical)."""
    url = f"{settings.sim_url}/charge"
    payload = {"key": provider_key, "amount_minor": amount_minor, "currency": currency}
    for attempt in range(settings.worker_retries + 1):
        try:
            response = httpx.post(url, json=payload, timeout=15.0)
        except httpx.HTTPError as error:
            print(json.dumps({"event": "provider.network_error", "error": str(error)}), flush=True)
        else:
            if response.status_code == 200:
                return "succeeded", response.json()
            if response.status_code == 422:
                return "declined", response.json()
            if response.status_code != 503:
                return "declined", {
                    "reason": "unexpected_provider_response",
                    "status": response.status_code,
                }
            print(json.dumps({"event": "provider.unavailable", "status": 503}), flush=True)
        if attempt < settings.worker_retries:
            time.sleep(2**attempt)
    return "unavailable", {"reason": "provider_unavailable"}


def claimed_items(reclaimed: object) -> list:
    """XAUTOCLAIM result: redis-py 5 gives a list, older releases a tuple."""
    if isinstance(reclaimed, (list, tuple)) and len(reclaimed) >= 2:
        return list(reclaimed[1] or [])
    return []


def handle(fields: dict[str, str]) -> None:
    event_id = fields["event_id"]
    event_type = fields["event_type"]
    if _already_processed(event_id):
        return
    payload: dict[str, Any] = json.loads(fields.get("payload", "{}"))
    if event_type != "payment.requested":
        with transaction() as conn:
            conn.execute(
                "INSERT INTO processed_messages(handler, event_id) VALUES ('worker', %s) ON CONFLICT DO NOTHING",
                (event_id,),
            )
        return

    operation_id = str(payload["operation_id"])
    with connection() as conn:
        operation = conn.execute(
            "SELECT * FROM operations WHERE id=%s", (operation_id,)
        ).fetchone()
    if operation is None:
        return
    if operation["status"] in ("succeeded", "failed"):
        # A redelivery after a crash: the effect is already final.
        with transaction() as conn:
            conn.execute(
                "INSERT INTO processed_messages(handler, event_id) VALUES ('worker', %s) ON CONFLICT DO NOTHING",
                (event_id,),
            )
        return

    # Published before the external call so GET /v1/operations can observe
    # pending -> running (O4.1). A retry after a crash sees the same state.
    with transaction() as conn:
        conn.execute(
            """
            UPDATE operations SET status='running', updated_at=now()
            WHERE id=%s AND status='pending'
            """,
            (operation_id,),
        )

    request = operation["request"] or {}
    status, provider_result = _call_provider(
        operation["provider_key"],
        int(request.get("amount_minor", payload.get("amount_minor", 0))),
        str(request.get("currency", "UAH")),
    )
    inject_fault("after_external_before_commit")

    if status == "unavailable":
        # No local commit and no ACK: XAUTOCLAIM reclaims it and the simulator
        # is called again with the same operation key (503 is not remembered).
        raise ProviderUnavailable(provider_result.get("reason", "provider_unavailable"))

    with transaction() as conn:
        if _already_processed_in(conn, event_id):
            return
        conn.execute(
            "INSERT INTO processed_messages(handler, event_id) VALUES ('worker', %s) ON CONFLICT DO NOTHING",
            (event_id,),
        )
        if status == "succeeded":
            conn.execute(
                """
                UPDATE operations
                SET status='succeeded', result=%s, version=version+1, updated_at=now()
                WHERE id=%s AND status IN ('pending','running')
                """,
                (Jsonb(provider_result), operation_id),
            )
            conn.execute(
                "UPDATE rides SET payment_status='paid' WHERE id=%s AND payment_status='pending'",
                (operation["ride_id"],),
            )
            notification_type = "payment.succeeded"
            notification_body = {"ride_id": operation["ride_id"], "message": "Оплату підтверджено"}
        else:
            conn.execute(
                """
                UPDATE operations
                SET status='failed', error=%s, version=version+1, updated_at=now()
                WHERE id=%s AND status IN ('pending','running')
                """,
                (Jsonb(provider_result), operation_id),
            )
            conn.execute(
                "UPDATE rides SET payment_status='failed' WHERE id=%s AND payment_status='pending'",
                (operation["ride_id"],),
            )
            notification_type = "payment.failed"
            notification_body = {
                "ride_id": operation["ride_id"],
                "message": "Оплату відхилено",
                "reason": provider_result.get("reason", "provider_declined"),
            }
        insert_notification(
            conn,
            event_id=f"payment-notify-{event_id}",
            user_id=operation["user_id"],
            event_type=notification_type,
            payload=notification_body,
        )


def _already_processed_in(conn, event_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM processed_messages WHERE handler='worker' AND event_id=%s",
            (event_id,),
        ).fetchone()
        is not None
    )


def main() -> None:
    ensure_group(DOMAIN_STREAM, GROUP)
    broker = client()
    consumer = f"{settings.instance}-{uuid.uuid4().hex[:8]}"
    print(json.dumps({"event": "worker.start", "group": GROUP, "consumer": consumer}), flush=True)
    while True:
        try:
            messages = broker.xreadgroup(
                GROUP, consumer, {DOMAIN_STREAM: ">"}, count=10, block=2000
            )
            # Messages left pending by a crashed consumer are reclaimed, not lost.
            reclaimed = broker.xautoclaim(
                DOMAIN_STREAM, GROUP, consumer, min_idle_time=10_000,
                start_id="0-0", count=10,
            )
            items_by_source = [items for _, items in messages]
            claimed = claimed_items(reclaimed)
            if claimed:
                items_by_source.append(claimed)
            for items in items_by_source:
                for message_id, fields in items:
                    try:
                        handle(fields)
                        broker.xack(DOMAIN_STREAM, GROUP, message_id)
                    except Exception as error:  # noqa: BLE001 - keep the stream moving
                        print(
                            json.dumps({"event": "worker.error", "message_id": message_id, "error": str(error)}),
                            flush=True,
                        )
        except Exception as error:  # noqa: BLE001
            print(json.dumps({"event": "worker.loop_error", "error": str(error)}), flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
