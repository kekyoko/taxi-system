"""Redis Streams broker (Kafka/RabbitMQ alternative allowed by the guide)."""

from __future__ import annotations

import json

import redis

from .config import settings

DOMAIN_STREAM = "domain-events"
NOTIFICATION_STREAM = "notifications"


def client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def ensure_group(stream: str, group: str) -> None:
    connection = client()
    try:
        connection.xgroup_create(stream, group, id="0-0", mkstream=True)
    except redis.ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise
    finally:
        connection.close()


def event_fields(row: dict) -> dict[str, str]:
    return {
        "event_id": row["id"],
        "event_type": row["event_type"],
        "aggregate_type": row["aggregate_type"],
        "aggregate_id": row["aggregate_id"],
        "version": str(row["version"]),
        "schema_version": str(row["schema_version"]),
        "created_at": row["created_at"].isoformat(),
        "payload": json.dumps(row["payload"], ensure_ascii=False, sort_keys=True),
    }
